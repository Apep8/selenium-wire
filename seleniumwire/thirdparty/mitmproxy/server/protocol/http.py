import base64
import enum
import logging
import textwrap
import time

import h2.exceptions

from seleniumwire.thirdparty.mitmproxy import connections  # noqa
from seleniumwire.thirdparty.mitmproxy import exceptions, flow, http
from seleniumwire.thirdparty.mitmproxy.net import websockets
from seleniumwire.thirdparty.mitmproxy.server.protocol import base
from seleniumwire.thirdparty.mitmproxy.server.protocol.websocket import WebSocketLayer
from seleniumwire.thirdparty.mitmproxy.utils import strutils

log = logging.getLogger(__name__)


class _HttpTransmissionLayer(base.Layer):
    def read_request_headers(self, flow):
        raise NotImplementedError()

    def read_request_body(self, request):
        raise NotImplementedError()

    def read_request_trailers(self, request):
        raise NotImplementedError()

    def send_request(self, request):
        raise NotImplementedError()

    def read_response_headers(self):
        raise NotImplementedError()

    def read_response_body(self, request, response):
        raise NotImplementedError()
        yield "this is a generator"  # pragma: no cover

    def read_response_trailers(self, request, response):
        raise NotImplementedError()

    def read_response(self, request):
        response = self.read_response_headers()
        response.data.content = b"".join(
            self.read_response_body(request, response)
        )
        response.data.trailers = self.read_response_trailers(request, response)
        return response

    def send_response(self, response):
        if response.data.content is None:
            raise exceptions.HttpException("Cannot assemble flow with missing content")
        self.send_response_headers(response)
        self.send_response_body(response, [response.data.content])
        self.send_response_trailers(response)

    def send_response_headers(self, response):
        raise NotImplementedError()

    def send_response_body(self, response, chunks):
        raise NotImplementedError()

    def send_response_trailers(self, response, chunks):
        raise NotImplementedError()

    def check_close_connection(self, f):
        raise NotImplementedError()


class ConnectServerConnection:

    """
    "Fake" ServerConnection to represent state after a CONNECT request to an upstream mitmproxy.
    """

    def __init__(self, address, ctx):
        self.address = address
        self._ctx = ctx

    @property
    def via(self):
        return self._ctx.server_conn

    def __getattr__(self, item):
        return getattr(self.via, item)

    def connected(self):
        return self.via.connected()


class UpstreamConnectLayer(base.Layer):

    def __init__(self, ctx, connect_request):
        super().__init__(ctx)
        self.connect_request = connect_request
        self.server_conn = ConnectServerConnection(
            (connect_request.host, connect_request.port),
            self.ctx
        )

    def __call__(self):
        layer = self.ctx.next_layer(self)
        layer()

    def _send_connect_request(self):
        self.log("Sending CONNECT request", "debug", [
            "Proxy Server: {}".format(self.ctx.server_conn.address),
            "Connect to: {}:{}".format(self.connect_request.host, self.connect_request.port)
        ])
        self.send_request(self.connect_request)
        resp = self.read_response(self.connect_request)
        if resp.status_code != 200:
            raise exceptions.ProtocolException("Reconnect: Upstream server refuses CONNECT request")

    def connect(self):
        if not self.server_conn.connected():
            self.ctx.connect()
            self._send_connect_request()
        else:
            pass  # swallow the message

    def change_upstream_proxy_server(self, address):
        self.log("Changing upstream mitmproxy to {} (CONNECTed)".format(repr(address)), "debug")
        if address != self.server_conn.via.address:
            self.ctx.set_server(address)

    def set_server(self, address):
        if self.ctx.server_conn.connected():
            self.ctx.disconnect()
        self.connect_request.host = address[0]
        self.connect_request.port = address[1]
        self.server_conn.address = address


def is_ok(status):
    return 200 <= status < 300


class HTTPMode(enum.Enum):
    regular = 1
    transparent = 2
    upstream = 3


# At this point, we see only a subset of the mitmproxy modes
MODE_REQUEST_FORMS = {
    HTTPMode.regular: ("authority", "absolute"),
    HTTPMode.transparent: ("relative",),
    HTTPMode.upstream: ("authority", "absolute"),
}


def validate_request_form(mode, request):
    if request.first_line_format == "absolute" and request.scheme not in ("http", "https"):
        raise exceptions.HttpException(
            "Invalid request scheme: %s" % request.scheme
        )
    allowed_request_forms = MODE_REQUEST_FORMS[mode]
    if request.first_line_format not in allowed_request_forms:
        if request.is_http2 and mode is HTTPMode.transparent and request.first_line_format == "absolute":
            return  # dirty hack: h2 may have authority info. will be fixed properly with sans-io.
        if mode == HTTPMode.transparent:
            err_message = textwrap.dedent((
                """
                Mitmproxy received an {} request even though it is not running
                in regular mode. This usually indicates a misconfiguration,
                please see the mitmproxy mode documentation for details.
                """
            )).strip().format("HTTP CONNECT" if request.first_line_format == "authority" else "absolute-form")
        else:
            err_message = "Invalid HTTP request form (expected: %s, got: %s)" % (
                " or ".join(allowed_request_forms), request.first_line_format
            )
        raise exceptions.HttpException(err_message)


class HttpLayer(base.Layer):

    if False:
        # mypy type hints
        server_conn: connections.ServerConnection = None

    def __init__(self, ctx, mode):
        super().__init__(ctx)
        self.mode = mode
        self.__initial_server_address: tuple = None
        "Contains the original destination in transparent mode, which needs to be restored"
        "if an inline script modified the target server for a single http request"
        # We cannot rely on server_conn.tls_established,
        # see https://github.com/mitmproxy/mitmproxy/issues/925
        self.__initial_server_tls = None
        # Requests happening after CONNECT do not need Proxy-Authorization headers.
        self.connect_request = False

    def __call__(self):
        if self.mode == HTTPMode.transparent:
            self.__initial_server_tls = self.server_tls
            self.__initial_server_address = self.server_conn.address
        while True:
            flow = http.HTTPFlow(
                self.client_conn,
                self.server_conn,
                live=self,
                mode=self.mode.name
            )
            if not self._process_flow(flow):
                return

    def handle_regular_connect(self, f):
        self.connect_request = True

        try:
            self.set_server((f.request.host, f.request.port))

            if f.response:
                resp = f.response
            else:
                resp = http.make_connect_response(f.request.data.http_version)

            self.send_response(resp)

            if is_ok(resp.status_code):
                layer = self.ctx.next_layer(self)
                layer()
        except (
            exceptions.ProtocolException, exceptions.NetlibException
        ) as e:
            # HTTPS tasting means that ordinary errors like resolution
            # and connection errors can happen here.
            self.send_error_response(502, repr(e))
            f.error = flow.Error(str(e))
            self.channel.ask("error", f)
            return False

        return False

    def handle_upstream_connect(self, f):
        # if the user specifies a response in the http_connect hook, we do not connect upstream here.
        # https://github.com/mitmproxy/mitmproxy/pull/2473
        if not f.response:
            self.establish_server_connection(
                f.request.host,
                f.request.port,
                f.request.scheme
            )
            self.send_request(f.request)
            f.response = self.read_response_headers()
            f.response.data.content = b"".join(
                self.read_response_body(f.request, f.response)
            )
        self.send_response(f.response)
        if is_ok(f.response.status_code):
            layer = UpstreamConnectLayer(self, f.request)
            return layer()
        elif f.response.status_code == 407:
            _, proxy_authority = self.config.options.mode.split('upstream:')
            username, _ = self.config.options.upstream_auth.split(':')
            log.error("Couldn't connect to proxy server %s with username %s", proxy_authority, username)
            return False

        return False

    def _process_flow(self, f):
        try:
            try:
                request: http.HTTPRequest = self.read_request_headers(f)
            except exceptions.HttpReadDisconnect:
                # don't throw an error for disconnects that happen
                # before/between requests.
                return False

            f.request = request

            if (self.mode is HTTPMode.upstream or "socks" in self.config.options.mode) and \
                    self.matches_no_proxy(f.request):
                self.set_server((f.request.host, f.request.port))
                self.mode = HTTPMode.regular
                self.server_conn.use_socks = False

            if request.first_line_format == "authority":
                # The standards are silent on what we should do with a CONNECT
                # request body, so although it's not common, it's allowed.
                f.request.data.content = b"".join(
                    self.read_request_body(f.request)
                )
                f.request.data.trailers = self.read_request_trailers(f.request)
                f.request.timestamp_end = time.time()
                self.channel.ask("http_connect", f)

                if self.mode is HTTPMode.regular:
                    return self.handle_regular_connect(f)
                elif self.mode is HTTPMode.upstream:
                    self.apply_proxy_auth(f)
                    return self.handle_upstream_connect(f)
                else:
                    msg = "Unexpected CONNECT request."
                    self.send_error_response(400, msg)
                    return False

            if not self.config.options.relax_http_form_validation:
                validate_request_form(self.mode, request)
            self.channel.ask("requestheaders", f)

            if self.mode is HTTPMode.upstream and not f.server_conn.via:
                self.apply_proxy_auth(f)

            # Re-validate request form in case the user has changed something.
            if not self.config.options.relax_http_form_validation:
                validate_request_form(self.mode, request)

            if request.headers.get("expect", "").lower() == "100-continue":
                # TODO: We may have to use send_response_headers for HTTP2
                # here.
                self.send_response(http.make_expect_continue_response())
                request.headers.pop("expect")

            if f.request.stream:
                f.request.data.content = None
            else:
                f.request.data.content = b"".join(self.read_request_body(request))

            f.request.data.trailers = self.read_request_trailers(f.request)

            request.timestamp_end = time.time()
        except exceptions.HttpException as e:
            # We optimistically guess there might be an HTTP client on the
            # other end
            self.send_error_response(400, repr(e))
            # Request may be malformed at this point, so we unset it.
            f.request = None
            f.error = flow.Error(str(e))
            self.channel.ask("error", f)
            msg = "HTTP protocol error in client request: {}".format(e)
            if self.config.options.suppress_connection_errors:
                self.log("request", "debug", [msg])
            else:
                self.log("request", "warn", [msg])
            return False

        self.log("request", "debug", [repr(request)])

        # set first line format to relative in regular mode,
        # see https://github.com/mitmproxy/mitmproxy/issues/1759
        if self.mode is HTTPMode.regular and request.first_line_format == "absolute":
            request.authority = ""

        # update host header in reverse mitmproxy mode
        if self.config.options.mode.startswith("reverse:") and not self.config.options.keep_host_header:
            f.request.host_header = self.config.upstream_server.address[0]

        # Determine .scheme, .host and .port attributes for inline scripts. For
        # absolute-form requests, they are directly given in the request. For
        # authority-form requests, we only need to determine the request
        # scheme. For relative-form requests, we need to determine host and
        # port as well.
        if self.mode is HTTPMode.transparent:
            # Setting request.host also updates the host header, which we want
            # to preserve
            f.request.data.host = self.__initial_server_address[0]
            f.request.data.port = self.__initial_server_address[1]
            f.request.data.scheme = b"https" if self.__initial_server_tls else b"http"
        self.channel.ask("request", f)

        try:
            if websockets.check_handshake(request.headers) and websockets.check_client_version(request.headers):
                f.metadata['websocket'] = True
                # We only support RFC6455 with WebSocket version 13
                # allow inline scripts to manipulate the client handshake
                self.channel.ask("websocket_handshake", f)

            if not f.response:
                self.establish_server_connection(
                    f.request.host,
                    f.request.port,
                    f.request.scheme
                )

                def get_response():
                    self.send_request_headers(f.request)

                    if f.request.stream:
                        chunks = self.read_request_body(f.request)
                        if callable(f.request.stream):
                            chunks = f.request.stream(chunks)
                        self.send_request_body(f.request, chunks)
                    else:
                        self.send_request_body(f.request, [f.request.data.content])

                    self.send_request_trailers(f.request)

                    f.response = self.read_response_headers()

                try:
                    get_response()
                except exceptions.NetlibException as e:
                    self.log(
                        "server communication error: %s" % repr(e),
                        level="debug"
                    )
                    # In any case, we try to reconnect at least once. This is
                    # necessary because it might be possible that we already
                    # initiated an upstream connection after clientconnect that
                    # has already been expired, e.g consider the following event
                    # log:
                    # > clientconnect (transparent mode destination known)
                    # > serverconnect (required for client tls handshake)
                    # > read n% of large request
                    # > server detects timeout, disconnects
                    # > read (100-n)% of large request
                    # > send large request upstream

                    if isinstance(e, exceptions.Http2ProtocolException):
                        # do not try to reconnect for HTTP2
                        raise exceptions.ProtocolException(
                            "First and only attempt to get response via HTTP2 failed."
                        )
                    elif f.request.stream:
                        # We may have already consumed some request chunks already,
                        # so all we can do is signal downstream that upstream closed the connection.
                        self.send_error_response(408, "Request Timeout")
                        f.error = flow.Error(repr(e))
                        self.channel.ask("error", f)
                        return False

                    self.disconnect()
                    self.connect()
                    get_response()

                # call the appropriate script hook - this is an opportunity for
                # an inline script to set f.stream = True
                self.channel.ask("responseheaders", f)

                if f.response.stream:
                    f.response.data.content = None
                else:
                    f.response.data.content = b"".join(
                        self.read_response_body(f.request, f.response)
                    )
                f.response.timestamp_end = time.time()

                # no further manipulation of self.server_conn beyond this point
                # we can safely set it as the final attribute value here.
                f.server_conn = self.server_conn
            else:
                # response was set by an inline script.
                # we now need to emulate the responseheaders hook.
                self.channel.ask("responseheaders", f)

            f.response.data.trailers = self.read_response_trailers(f.request, f.response)

            self.log("response", "debug", [repr(f.response)])
            self.channel.ask("response", f)

            if not f.response.stream:
                # no streaming:
                # we already received the full response from the server and can
                # send it to the client straight away.
                self.send_response(f.response)
            else:
                # streaming:
                # First send the headers and then transfer the response incrementally
                self.send_response_headers(f.response)
                chunks = self.read_response_body(
                    f.request,
                    f.response
                )
                if callable(f.response.stream):
                    chunks = f.response.stream(chunks)
                self.send_response_body(f.response, chunks)
                f.response.timestamp_end = time.time()

            if self.check_close_connection(f):
                return False

            # Handle 101 Switching Protocols
            if f.response.status_code == 101:
                # Handle a successful HTTP 101 Switching Protocols Response,
                # received after e.g. a WebSocket upgrade request.
                # Check for WebSocket handshake
                is_websocket = (
                    websockets.check_handshake(f.request.headers) and
                    websockets.check_handshake(f.response.headers)
                )
                if is_websocket and not self.config.options.websocket:
                    self.log(
                        "Client requested WebSocket connection, but the protocol is disabled.",
                        "info"
                    )

                if is_websocket and self.config.options.websocket:
                    layer = WebSocketLayer(self, f)
                else:
                    layer = self.ctx.next_layer(self)
                layer()
                return False  # should never be reached

        except (exceptions.ProtocolException, exceptions.NetlibException) as e:
            if not f.response:
                self.send_error_response(502, repr(e))
                f.error = flow.Error(str(e))
                self.channel.ask("error", f)
                return False
            else:
                raise exceptions.ProtocolException(
                    "Error in HTTP connection: %s" % repr(e)
                )
        finally:
            if f:
                f.live = False

        return True

    def send_error_response(self, code, message, headers=None) -> None:
        try:
            response = http.make_error_response(code, message, headers)
            self.send_response(response)
        except (exceptions.NetlibException, h2.exceptions.H2Error, exceptions.Http2ProtocolException):
            self.log("Failed to send error response to client: {}".format(message), "debug")

    def change_upstream_proxy_server(self, address):
        # Make set_upstream_proxy_server always available,
        # even if there's no UpstreamConnectLayer
        if hasattr(self.ctx, "change_upstream_proxy_server"):
            self.ctx.change_upstream_proxy_server(address)
        elif address != self.server_conn.address:
            self.log("Changing upstream mitmproxy to {} (not CONNECTed)".format(repr(address)), "debug")
            self.set_server(address)

    def establish_server_connection(self, host: str, port: int, scheme: str):
        tls = (scheme == "https")

        if self.mode is HTTPMode.regular or self.mode is HTTPMode.transparent:
            # If there's an existing connection that doesn't match our expectations, kill it.
            address = (host, port)
            if address != self.server_conn.address or tls != self.server_tls:
                self.set_server(address)
                self.set_server_tls(tls, address[0])
            # Establish connection is necessary.
            if not self.server_conn.connected():
                self.connect()
        else:
            if not self.server_conn.connected():
                self.connect()
            if tls:
                raise exceptions.HttpProtocolException("Cannot change scheme in upstream mitmproxy mode.")

    def matches_no_proxy(self, request):
        """Whether we should bypass any upstream proxy.

        This checks whether the request address is in the no_proxy list.
        """
        no_proxy = False

        for addr in self.config.options.no_proxy:
            host, *port = addr.split(':')

            if request.host.endswith(host):
                no_proxy = True

                if port:
                    # The user has specified a port, so this also needs to match
                    no_proxy = int(port[0]) == request.port

            if no_proxy:
                break

        return no_proxy

    def apply_proxy_auth(self, f):
        """Apply proxy authorization to the request if configured."""
        auth = None

        try:
            auth = self.config.options.upstream_custom_auth
        except AttributeError:
            pass

        if not auth:
            try:
                auth = self.config.options.upstream_auth

                if auth:
                    auth = b"Basic" + b" " + base64.b64encode(strutils.always_bytes(auth))
            except AttributeError:
                pass

        if auth:
            f.request.headers["Proxy-Authorization"] = auth
