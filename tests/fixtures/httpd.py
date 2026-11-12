"""A small HTTP(S) server for tests, in-process, on 127.0.0.1 with a random port (never reachable from outside).

Routes are plain paths (query string carries the knobs). Every request is recorded in `server.requests`
(path, lowercase header dict, client port); `server.connections` counts accepted TCP connections and
`server.sent` counts body bytes the server managed to write (a client that stops reading early makes it
small; that is how tests measure "read no more than the first chunk").

Not production code: no policy, no secrets. Errors raised by a client that hangs up early are swallowed."""

import socket
import ssl
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CHUNK = 65536
HTML_HEAD = b"<!DOCTYPE html><html><head><title>x</title></head><body>"


def _int(query, name, default):
    return int(query.get(name, [default])[0])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.server.note_connection()

    def handle(self):
        if self.server.tls is not None:
            self.request.do_handshake()  # runs in the handler thread; a failed handshake ends the thread quietly
        super().handle()

    def log_message(self, *args):
        pass

    # --- helpers
    def head(self, status, headers=(), length=None, close=False):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if length is not None:
            self.send_header("Content-Length", str(length))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

    def push(self, data):
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except OSError:
            return False
        self.server.add_sent(len(data))
        return True

    def stream(self, size, fill=b"x", first=b""):
        """Write `size` bytes (first `first`, then `fill` repeated) in 64 KiB pieces; stop when the client leaves."""
        left = size
        if first:
            piece = first[:left]
            if not self.push(piece):
                return
            left -= len(piece)
        while left > 0:
            piece = (fill * CHUNK)[: min(CHUNK, left)]
            if not self.push(piece):
                return
            left -= len(piece)

    # --- routes
    def do_GET(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        path = parts.path
        self.server.record(self.path, {k.lower(): v for k, v in self.headers.items()}, self.client_address[1])
        try:
            self.route(path, query)
        except (OSError, ssl.SSLError):
            self.close_connection = True

    def route(self, path, q):
        if path == "/ok":
            self.head(200, [("Set-Cookie", "sid=abc; Path=/"), ("Content-Type", "text/plain")], 5)
            self.push(b"hello")
        elif path == "/echo":
            self.head(200, [("Content-Type", "text/plain")], 4)
            self.push(b"echo")
        elif path == "/file":  # /file?size=N
            size = _int(q, "size", 100)
            self.head(200, [("Content-Type", "application/octet-stream")], size)
            self.stream(size)
        elif path.startswith("/status/"):
            code = int(path.rsplit("/", 1)[1])
            extra = [("Retry-After", "30")] if code == 429 else []
            self.head(code, extra, 0)
        elif path.startswith("/enc/"):  # /enc/<coding>?size=N : a body that claims to be encoded (bytes are plain)
            size = _int(q, "size", 100)
            self.head(200, [("Content-Encoding", path.rsplit("/", 1)[1].replace("+", ", ")),
                            ("Content-Type", "application/octet-stream")], size)
            self.stream(size)
        elif path == "/nocl":  # no Content-Length, connection close
            size = _int(q, "size", 5000)
            self.head(200, [("Content-Type", "application/octet-stream")], close=True)
            self.stream(size)
        elif path == "/bigcl":  # /bigcl?cl=N&size=M  : declared N, really sends M
            cl, size = _int(q, "cl", 100), _int(q, "size", 100)
            self.head(200, [("Content-Type", "application/octet-stream")], cl, close=True)
            self.stream(size)
        elif path == "/html":  # an HTML page; /html?size=N, Content-Type from ?ctype= (default text/html)
            size = _int(q, "size", 2000)
            ctype = q.get("ctype", ["text/html"])[0]
            self.head(200, [("Content-Type", ctype)], size, close=True)
            self.stream(size, fill=b" ", first=HTML_HEAD)
        elif path.endswith(".pdf") or path.endswith(".png"):  # /<x>.pdf?kind=html|real&size=N
            size = _int(q, "size", 2000)
            if q.get("kind", ["real"])[0] == "html":
                self.head(200, [("Content-Type", q.get("ctype", ["text/html"])[0])], size, close=True)
                self.stream(size, fill=b" ", first=HTML_HEAD)
            else:
                self.head(200, [("Content-Type", "application/pdf")], size, close=True)
                self.stream(size, fill=b"0", first=b"%PDF-1.7\n")
        elif path == "/short":  # /short?cl=1000&send=10
            self.head(200, [], _int(q, "cl", 1000), close=True)
            self.push(b"x" * _int(q, "send", 10))
        elif path == "/chunked":  # /chunked?size=N&ext=M : one chunk of N bytes, M bytes of chunk extension
            size, ext = _int(q, "size", 100), _int(q, "ext", 0)
            self.head(200, [("Transfer-Encoding", "chunked"), ("Content-Type", "application/octet-stream")], close=True)
            line = b"%x" % size + ((b";e=" + b"x" * ext) if ext else b"") + b"\r\n"
            if self.push(line):
                self.stream(size)
                self.push(b"\r\n0\r\n\r\n")
        elif path == "/bighdr":  # a response header of ?size bytes
            self.head(200, [("X-Big", "h" * _int(q, "size", 100000))], 0)
        elif path == "/reset":
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.connection.close()
            self.close_connection = True
        elif path == "/slow":  # nothing at all for ?delay seconds (header stall)
            time.sleep(float(q.get("delay", ["3"])[0]))
            self.head(200, [], 0)
        elif path == "/stall":  # header and ?send bytes, then silence for ?delay seconds
            self.head(200, [("Content-Type", "application/octet-stream")], 1000, close=True)
            self.push(b"x" * _int(q, "send", 5))
            time.sleep(float(q.get("delay", ["5"])[0]))
        elif path == "/drip":  # one byte every ?every seconds, ?n bytes
            n = _int(q, "n", 100)
            self.head(200, [("Content-Type", "application/octet-stream")], n, close=True)
            for _ in range(n):
                if not self.push(b"d"):
                    return
                time.sleep(float(q.get("every", ["0.05"])[0]))
        elif path == "/redir":  # /redir?to=<url>&code=302&cookie=1
            headers = [("Location", q["to"][0])]
            if q.get("cookie"):
                headers.append(("Set-Cookie", "hop=1; Path=/"))
            self.head(_int(q, "code", 302), headers, 0)
        elif path == "/hdr":  # raw header bytes: control and bidi characters, very long values
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Last-Modified", "Wed, 21 Oct 2015 07:28:00 GMT")
            self.send_header("Content-Disposition",
                             ('attachment; filename="a‮b\u0085c' + "z" * 400 + '.pdf"').encode("utf-8"))
            self.end_headers()
            self.wfile.flush()
            self.push(b"hi")
        else:
            self.head(404, [], 0)

    def send_header(self, name, value):
        # allow the /hdr route to send non-latin-1 text: values given as bytes are written as they are
        if isinstance(value, bytes):
            if self.request_version != "HTTP/0.9":
                self._headers_buffer.append(name.encode("latin-1") + b": " + value + b"\r\n")
            return
        super().send_header(name, value)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, tls=None):
        """`tls` is (certfile, keyfile) for an HTTPS server, or None."""
        super().__init__(("127.0.0.1", 0), Handler)
        self.tls = None
        self.sni = []
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*tls)
            ctx.sni_callback = self._sni
            self.tls = ctx
        self._lock = threading.Lock()
        self.requests = []
        self.connections = 0
        self.sent = 0
        self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    # --- recording
    def _sni(self, sslobj, name, ctx):
        self.sni.append(name)

    def get_request(self):
        sock, addr = super().get_request()
        if self.tls is not None:
            sock = self.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, addr

    def handle_error(self, request, client_address):
        pass  # a client that hangs up early is part of the tests, not an error

    def note_connection(self):
        with self._lock:
            self.connections += 1

    def add_sent(self, count):
        with self._lock:
            self.sent += count

    def record(self, path, headers, port):
        with self._lock:
            self.requests.append({"path": path, "headers": headers, "client_port": port})

    # --- use
    @property
    def port(self):
        return self.server_address[1]

    def url(self, path="/", host="pinned.example", scheme=None):
        scheme = scheme or ("https" if self.tls is not None else "http")
        return "%s://%s:%d%s" % (scheme, host, self.port, path)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()
        self._thread.join(2)
