"""Per-item egress proxy for yt-dlp (SPEC FR13.1-FR13.5, FR13.8; ADR-005).

A tiny HTTP CONNECT proxy on 127.0.0.1, one per media item. It is a cooperative control, not a sandbox: it
stops redirects and DNS tricks of a public source from reaching an internal address, it does not stop a child
process that chooses to go around it (node, ADR-005). What it enforces:

* a token (`Proxy-Authorization: Basic`) on every request, compared in constant time;
* CONNECT only, to a NAME (never an IP literal) on port 443 that passes the URL policy;
* the name is resolved once per CONNECT by `netfetch.PinnedBackend`; only a public address is connected to, by
  its literal IP (the check and the connection are one step);
* a byte counter per item (bytes that came back from the remote side, under a lock) with a cap;
* a bound on tunnels (taken only after the token was right), on connections that have not proved themselves
  yet, on the request size and on the time to send the headers.

Every refusal is answered with a status (403 policy, 405 method, 502 could not connect, 503 busy), never by a
silent close, and every connection leaves exactly one log line, whatever way it ends.
Nothing here reads the environment; the policy and the backend are parameters of the code."""

import base64
import binascii
import hmac
import ipaddress
import re
import secrets
import selectors
import socket
import threading
import time
from urllib.parse import urlsplit

import httpcore

from . import netfetch, urlpolicy

LISTEN_HOST = "127.0.0.1"
CONNECT_PORT = 443
MAX_HEAD_BYTES = 8192
TICK = 0.25  # how often a blocked wait looks at the stop flag
IO_TIMEOUT = 30.0  # one send on an open tunnel
HALF_CLOSE_GRACE = 5.0  # after one side has finished, the other has this long
STATUS_TEXT = {400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed", 407: "Proxy Authentication Required",
               502: "Bad Gateway", 503: "Service Unavailable"}
DENIED_REASONS = frozenset({"ip_literal", "port", "host", "ip_not_public", "http_downgrade"})  # refusals about WHERE the client went
MAX_RECORDS = 100  # `denied` and `failures` never hold more entries than this
MAX_ACCEPT_ERRORS = 100  # consecutive accept() failures before the acceptor gives up (each one is logged)
_NAME = re.compile(r"[A-Za-z0-9._-]{1,253}", re.ASCII)


def _is_ip(text):
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _close(sock):
    for step in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
        try:
            step()
        except OSError:
            pass


class _Conn:
    """What one connection holds and has said: its places (pre-auth, tunnel), whether the client was answered,
    whether a log line was written, and how many bytes came back so far."""

    def __init__(self, proxy):
        self.proxy = proxy
        self.pending = True
        self.slot = False
        self.answered = False
        self.logged = False
        self.count = 0

    def leave_pending(self):
        if self.pending:
            self.pending = False
            self.proxy._pending.release()

    def release(self):
        if self.slot:
            self.slot = False
            self.proxy._slots.release()
        self.leave_pending()


class EgressProxy:
    """One proxy for one item. `start()` returns the proxy URL (token inside); `stop()` closes everything."""

    def __init__(self, policy, *, byte_cap, connect_timeout, header_timeout=10.0, max_tunnels=16, max_pending=64,
                 log, token_of=secrets.token_hex, backend_of=netfetch.PinnedBackend):
        self._policy = policy
        self._byte_cap = byte_cap
        self._connect_timeout = connect_timeout
        self._header_timeout = header_timeout
        self._log = log
        self._token = token_of(32)
        self._token_bytes = self._token.encode("ascii")
        self._backend = backend_of(policy)
        self._slots = threading.BoundedSemaphore(max_tunnels)  # tunnels: taken only after the token was right
        self._pending = threading.BoundedSemaphore(max_pending)  # connections that have not proved themselves yet
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._stopping = threading.Event()
        self._bytes_in = 0
        self._sockets = set()
        self._threads = set()
        self._listener = None
        self._acceptor = None
        self._url = None
        self.over_cap = threading.Event()
        self.denied = []  # (host, reason) of refusals about where the client went, at most MAX_RECORDS
        self.failures = []  # (host, reason) of allowed CONNECTs that did not connect, at most MAX_RECORDS

    # --- public surface
    @property
    def bytes_in(self):
        with self._lock:
            return self._bytes_in

    @property
    def url(self):
        return self._url

    def start(self):
        if self._listener is not None:
            raise RuntimeError("already started")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            sock.bind((LISTEN_HOST, 0))
            sock.listen(32)
            sock.settimeout(TICK)
        except BaseException:
            sock.close()
            raise
        self._listener = sock
        self._url = "http://%s:x@%s:%d" % (self._token, LISTEN_HOST, sock.getsockname()[1])
        self._acceptor = threading.Thread(target=self._accept_loop, name="egress-accept", daemon=True)
        self._acceptor.start()
        return self._url

    def stop(self, timeout=2.0):
        """Closes the port and every socket; the whole call takes at most `timeout` (plus the close calls)."""
        end = time.monotonic() + timeout
        self._stopping.set()
        if self._listener is not None:
            _close(self._listener)
        if self._acceptor is not None:  # first: once it ended nothing new is added, so the lists below are complete
            self._acceptor.join(max(0.0, end - time.monotonic()))
        with self._lock:
            sockets = list(self._sockets)
            threads = list(self._threads)
        for sock in sockets:
            _close(sock)
        for thread in threads:
            thread.join(max(0.0, end - time.monotonic()))

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    # --- bookkeeping
    def _emit(self, host, port, decision, count):
        try:
            with self._log_lock:
                self._log("%s %s %s %d" % (host, port, decision, count))
        except Exception:  # noqa: BLE001 - a full or broken log must not take the tunnel down
            pass

    def _say(self, ctx, host, port, decision):
        """The one log line of a connection."""
        ctx.logged = True
        self._emit(host, port, decision, ctx.count)

    def _answer(self, ctx, conn, status, extra=""):
        ctx.answered = True
        self._reply(conn, status, extra)

    def _deny(self, reason, host):
        if reason in DENIED_REASONS:
            with self._lock:
                if len(self.denied) < MAX_RECORDS:
                    self.denied.append((host, reason))

    def _fail(self, reason, host):
        with self._lock:
            if len(self.failures) < MAX_RECORDS:
                self.failures.append((host, reason))

    def _track(self, sock):
        """False (nothing kept) once the proxy is stopping: the caller closes the socket."""
        with self._lock:
            if self._stopping.is_set():
                return False
            self._sockets.add(sock)
            return True

    def _untrack(self, sock):
        with self._lock:
            self._sockets.discard(sock)

    @staticmethod
    def _reply(conn, status, extra=""):
        text = "HTTP/1.1 %d %s\r\n%sContent-Length: 0\r\nConnection: close\r\n\r\n" % (status, STATUS_TEXT[status], extra)
        try:
            conn.settimeout(1.0)
            conn.sendall(text.encode("ascii"))
            conn.shutdown(socket.SHUT_WR)
            conn.settimeout(0.1)
            conn.recv(65536)  # unread request bytes would turn the close into a reset that eats the reply
        except OSError:
            pass

    # --- accepting
    def _accept_loop(self):
        errors = 0
        while not self._stopping.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                errors += 1  # a reset in the queue, no buffers: not the end of the proxy; say so and go on
                self._emit("-", "-", "deny:accept", 0)
                if errors >= MAX_ACCEPT_ERRORS:
                    return
                self._stopping.wait(0.05)
                continue
            errors = 0
            ctx = _Conn(self)
            if not self._pending.acquire(blocking=False):
                ctx.pending = False
                self._reply(conn, 503)
                _close(conn)
                self._emit("-", "-", "deny:busy", 0)
                continue
            thread = threading.Thread(target=self._serve, args=(conn, ctx), name="egress-conn", daemon=True)
            with self._lock:
                stopped = self._stopping.is_set()
                if not stopped:
                    self._sockets.add(conn)
                    self._threads.add(thread)
            if stopped:
                ctx.release()
                _close(conn)
                return
            try:
                thread.start()
            except BaseException:
                self._finish(conn, thread, ctx)
                raise

    def _finish(self, conn, thread, ctx):
        _close(conn)
        with self._lock:
            self._sockets.discard(conn)
            self._threads.discard(thread)
        ctx.release()

    def _serve(self, conn, ctx):
        try:
            self._handle(conn, ctx)
        except Exception:  # noqa: BLE001 - one broken connection never ends the proxy, and never goes unsaid
            if not ctx.answered:
                self._answer(ctx, conn, 502)
        finally:
            if not ctx.logged:  # whatever way out this was, the connection leaves one line
                self._say(ctx, "-", "-", "deny:error")
            self._finish(conn, threading.current_thread(), ctx)

    # --- one connection
    def _read_head(self, conn):
        """(head bytes, bytes that came after it) or a failure word: "long", "slow", "closed"."""
        end = time.monotonic() + self._header_timeout
        conn.settimeout(TICK)
        buf = b""
        while True:
            at = buf.find(b"\r\n\r\n")
            if (at if at >= 0 else len(buf)) > MAX_HEAD_BYTES:
                return "long"
            if at >= 0:
                return buf[:at], buf[at + 4:]
            if self._stopping.is_set() or time.monotonic() >= end:
                return "slow"
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return "closed"
            if not data:
                return "closed"
            buf += data

    def _authorized(self, values):
        if len(values) != 1 or not values[0].lower().startswith("basic "):
            return False
        try:
            user = base64.b64decode(values[0][6:].strip(), validate=True).split(b":", 1)[0]
        except (binascii.Error, ValueError):
            return False
        return hmac.compare_digest(user, self._token_bytes)

    def _check_target(self, target):
        """(None, host) when the target may be connected to, else (reason, host to log: a clean name or "-")."""
        host, sep, port = target.rpartition(":")
        if not sep:
            return "host", "-"
        bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
        if _is_ip(bare):
            return "ip_literal", "-"
        if not _NAME.fullmatch(host):
            return "host", "-"
        if port != str(CONNECT_PORT):
            return "port", host.lower()
        try:
            norm = urlpolicy.check_url("https://%s/" % host, self._policy)
        except urlpolicy.Blocked:
            return "host", "-"
        if norm.is_ip:
            return "ip_literal", "-"
        return None, norm.host

    @staticmethod
    def _plain_target(target):
        """(reason, host to log, port to log) of a request that is not a CONNECT. An absolute `http://` URL is what a
        client sends when a redirect took it to plain http: the name it names is recorded like any refused place."""
        if not target.lower().startswith("http://"):
            return "method", "-", "-"
        try:
            parts = urlsplit(target)
            host = (parts.hostname or "").lower().rstrip(".")
            port = str(parts.port or 80)
        except ValueError:
            return "http_downgrade", "-", "-"
        if not _NAME.fullmatch(host) or _is_ip(host):
            host = "-"
        return "http_downgrade", host, port

    def _handle(self, conn, ctx):
        head = self._read_head(conn)
        if head == "long":
            self._answer(ctx, conn, 400)
            self._say(ctx, "-", "-", "deny:bad_request")
            return
        if isinstance(head, str):
            self._say(ctx, "-", "-", "deny:slow")
            return
        text, rest = head
        lines = text.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        auth = [line.split(":", 1)[1].strip() for line in lines[1:] if line.lower().startswith("proxy-authorization:")]
        if len(parts) != 3:
            self._answer(ctx, conn, 400)
            self._say(ctx, "-", "-", "deny:bad_request")
            return
        if not self._authorized(auth):
            self._answer(ctx, conn, 407, 'Proxy-Authenticate: Basic realm="crawl-studio"\r\n')
            self._say(ctx, "-", "-", "deny:auth")
            return
        if parts[0] != "CONNECT":
            reason, host, port = self._plain_target(parts[1])
            self._deny(reason, host)
            self._answer(ctx, conn, 405)
            self._say(ctx, host, port, "deny:" + reason)
            return
        reason, host = self._check_target(parts[1])
        port = parts[1].rpartition(":")[2]
        port = port if port.isdigit() and len(port) <= 5 else "-"
        if reason is None and self.over_cap.is_set():
            reason = "cap"
        if reason is not None:
            self._deny(reason, host)
            self._answer(ctx, conn, 403)
            self._say(ctx, host, port, "deny:" + reason)
            return
        if not self._slots.acquire(blocking=False):  # a tunnel is taken only by a request that passed every check
            self._answer(ctx, conn, 503)
            self._say(ctx, host, port, "deny:busy")
            return
        ctx.slot = True
        ctx.leave_pending()
        self._open_tunnel(conn, host, rest, ctx)

    def _open_tunnel(self, conn, host, rest, ctx):
        try:
            stream = self._backend.connect_tcp(host, CONNECT_PORT, timeout=self._connect_timeout)
        except Exception as exc:  # noqa: BLE001 - every way of not connecting is answered, not swallowed
            code = getattr(exc, "code", None)
            reason = getattr(exc, "reason", None)
            if code == "URL_BLOCKED":
                reason = "ip_not_public" if reason == "ip_not_public" else "port"
                self._deny(reason, host)
                self._answer(ctx, conn, 403)
            else:
                if code == "TIMEOUT" or isinstance(exc, (httpcore.ConnectTimeout, socket.timeout)):
                    reason = "timeout"
                else:
                    reason = "dns_failed" if reason == "dns_failed" else "connect"
                self._fail(reason, host)  # the engine will only see "502": this is what says why
                self._answer(ctx, conn, 502)
            self._say(ctx, host, CONNECT_PORT, "deny:" + reason)
            return
        up = stream.get_extra_info("socket")
        if up is None:
            stream.close()
            self._fail("connect", host)
            self._answer(ctx, conn, 502)
            self._say(ctx, host, CONNECT_PORT, "deny:connect")
            return
        if not self._track(up):  # `stop()` ran while we were connecting: nothing may stay open after it returned
            stream.close()
            _close(up)
            self._say(ctx, host, CONNECT_PORT, "deny:stopped")
            return
        decision = "deny:error"
        try:
            ctx.answered = True
            conn.settimeout(IO_TIMEOUT)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._pump(conn, up, rest, ctx)
            decision = "deny:cap" if self.over_cap.is_set() else "allow"
        except Exception:  # noqa: BLE001 - the client left, or a socket died: say so, with the bytes counted so far
            pass
        finally:
            self._untrack(up)
            stream.close()
            _close(up)
            self._say(ctx, host, CONNECT_PORT, decision)

    def _pump(self, client, up, rest, ctx):
        """Copy both ways until both sides are done; `ctx.count` is the bytes that came back from the remote side."""
        up.settimeout(IO_TIMEOUT)
        if rest:
            up.sendall(rest)
        first_eof = None
        sel = selectors.DefaultSelector()
        try:
            sel.register(client, selectors.EVENT_READ, up)
            sel.register(up, selectors.EVENT_READ, client)
            while sel.get_map() and not self._stopping.is_set() and not self.over_cap.is_set():
                if first_eof is not None and time.monotonic() - first_eof > HALF_CLOSE_GRACE:
                    break
                for key, _ in sel.select(TICK):
                    src, dst = key.fileobj, key.data
                    try:
                        data = src.recv(65536)
                        if data and src is up:  # counted when received; the packet that crosses the cap is still passed on
                            ctx.count += len(data)
                            with self._lock:
                                self._bytes_in += len(data)
                                if self._bytes_in > self._byte_cap:
                                    self.over_cap.set()
                        if data:
                            dst.sendall(data)
                    except OSError:
                        return
                    if not data:
                        sel.unregister(src)
                        first_eof = first_eof or time.monotonic()
                        try:
                            dst.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
        finally:
            sel.close()
