"""HTTP(S) download with a pinned IP (SPEC FR2.4-FR2.7, FR3.1-FR3.4, FR3.9).

The check of an address and the connection to it are ONE step (`PinnedBackend.connect_tcp`): resolve once, keep
the addresses the policy accepts, connect to one of those by its literal IP. TLS, SNI and `Host` keep the original
name. Every `httpcore.*` exception is turned into `NetError` (they are not `httpx.HTTPError`). Redirects are
followed here, one fresh client per step, each step passing the URL policy again. Nothing reads the environment
and nothing here can switch the policy off: the policy arrives as a parameter of the code."""

import contextlib
import dataclasses
import errno
import http.cookiejar
import socket
import ssl
import threading
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from . import __version__, sniff, urlpolicy

USER_AGENT = "agent-crawl-studio/%s (OpcOS local tool)" % __version__
ALLOWED_REQUEST_HEADERS = ("host", "user-agent", "accept", "accept-encoding", "connection")
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
HEADERS_KEPT = ("content-type", "content-disposition", "last-modified", "retry-after")
MAX_HEADER_TEXT = 200
UNREACHABLE = frozenset({10051, 10065, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN})
MAX_RESOLVER_THREADS = 8  # lookups that may be stuck at the same time; one more is refused at once (TIMEOUT), never waited for
RESOLVER_SLOTS = threading.BoundedSemaphore(MAX_RESOLVER_THREADS)
TIMEOUT_CLASSES = (httpcore.ConnectTimeout, httpcore.ReadTimeout, httpcore.WriteTimeout, httpcore.PoolTimeout)


class NetError(Exception):
    """What the transport raises instead of any httpcore exception: a code (URL_BLOCKED, NETWORK_ERROR, TIMEOUT),
    a `reason` from a closed list (or None) and, for a TIMEOUT, the `kind` of wait that ran out: "connect", "idle"
    or "deadline" (the time of the item or of the job). Never carries exception text."""

    def __init__(self, code, reason=None, kind=None):
        super().__init__(code)
        self.code = code
        self.reason = reason
        self.kind = kind


class DeadlineTimeout(httpcore.ReadTimeout):
    """The absolute deadline of a request has passed (not a wait of the socket)."""


def _chain(exc):
    seen = []
    while exc is not None and len(seen) < 8 and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def classify(exc, body=False):
    """The NetError for an exception that came out of httpcore (by class, never by text). `body` is True for an
    error that surfaced while the body was being read: only then is a protocol error a truncated body."""
    if isinstance(exc, NetError):
        return exc
    if isinstance(exc, DeadlineTimeout):
        return NetError("TIMEOUT", kind="deadline")
    if isinstance(exc, TIMEOUT_CLASSES):
        return NetError("TIMEOUT", kind="idle" if isinstance(exc, (httpcore.ReadTimeout, httpcore.WriteTimeout)) else "connect")
    chain = _chain(exc)
    for found in chain:
        if isinstance(found, ssl.SSLCertVerificationError):
            return NetError("NETWORK_ERROR", "tls_verify_failed")
    for found in chain:
        if isinstance(found, ssl.SSLError):
            return NetError("NETWORK_ERROR", "other")
        if isinstance(found, ConnectionRefusedError):
            return NetError("NETWORK_ERROR", "connection_refused")
        if isinstance(found, (ConnectionResetError, ConnectionAbortedError)):
            return NetError("NETWORK_ERROR", "connection_reset")
        if isinstance(found, socket.gaierror):
            return NetError("NETWORK_ERROR", "dns_failed")
        if isinstance(found, OSError) and (found.errno in UNREACHABLE or getattr(found, "winerror", None) in UNREACHABLE):
            return NetError("NETWORK_ERROR", "network_unreachable")
    if body and isinstance(exc, httpcore.RemoteProtocolError):
        return NetError("NETWORK_ERROR", "truncated_body")
    return NetError("NETWORK_ERROR", "other")


class DeadlineStream(httpcore.NetworkStream):
    """A network stream whose every read, write and TLS handshake is capped by the time left until the backend's
    absolute deadline: a server that sends one byte at a time cannot stretch a request past it."""

    def __init__(self, stream, backend):
        self._stream, self._backend = stream, backend

    def read(self, max_bytes, timeout=None):
        return self._stream.read(max_bytes, timeout=self._backend.remaining(timeout))

    def write(self, buffer, timeout=None):
        self._stream.write(buffer, timeout=self._backend.remaining(timeout))

    def close(self):
        self._stream.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        try:
            timeout = self._backend.remaining(timeout)
        except BaseException:  # the deadline has passed: the connection must not be left open behind us
            self._stream.close()
            raise
        return DeadlineStream(self._stream.start_tls(ssl_context, server_hostname, timeout=timeout), self._backend)

    def get_extra_info(self, info):
        return self._stream.get_extra_info(info)


class PinnedBackend(httpcore.NetworkBackend):
    """Resolve, filter and connect in one call, to an IP that passed `policy.ip_ok`."""

    def __init__(self, policy, clock=time.monotonic):
        self.policy = policy
        self._clock = clock  # the one monotonic clock of this backend; tests may inject it
        self._inner = httpcore.SyncBackend()
        self.deadline = None  # absolute `clock()` value set per request by the transport

    def remaining(self, timeout):
        """`timeout` capped by the time left before the deadline; a passed deadline is a timeout."""
        if self.deadline is None:
            return timeout
        left = self.deadline - self._clock()
        if left <= 0:
            raise DeadlineTimeout("deadline")
        return left if timeout is None else min(timeout, left)

    def _resolve(self, host, port, timeout):
        box = {}
        worker = None
        got = False
        try:  # a Ctrl+C before the thread runs must give the slot back: nothing else would (the sweep of RQ-011)
            got = RESOLVER_SLOTS.acquire(blocking=False)
            if not got:
                raise NetError("TIMEOUT", kind="connect")

            def lookup():
                try:
                    box["infos"] = self.policy.resolver(host, port, proto=socket.IPPROTO_TCP)
                except BaseException as exc:  # noqa: BLE001 - whatever a resolver raises is "did not resolve"
                    box["error"] = exc
                finally:
                    RESOLVER_SLOTS.release()

            worker = threading.Thread(target=lookup, daemon=True)
            worker.start()
        except BaseException:
            if got and (worker is None or worker.ident is None):  # the thread never ran: its `finally` will not release
                RESOLVER_SLOTS.release()
            raise
        worker.join(timeout)
        if worker.is_alive():
            raise NetError("TIMEOUT", kind="connect")
        try:
            return [info[4][0] for info in box["infos"]]
        except (KeyError, TypeError, IndexError):  # a failed lookup, or an answer that is not a list of address records
            raise NetError("NETWORK_ERROR", "dns_failed") from None

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if port not in self.policy.ports:
            raise NetError("URL_BLOCKED", "port_not_allowed")
        timeout = self.remaining(timeout)
        started = self._clock()
        addresses = self._resolve(host, port, timeout)
        if not addresses:
            raise NetError("NETWORK_ERROR", "dns_failed")
        usable = [ip for ip in addresses if self.policy.ip_ok(ip)]
        if not usable:
            raise NetError("URL_BLOCKED", "ip_not_public")
        last = None
        for ip in usable:  # the literal IP: the name is never resolved a second time
            left = None if timeout is None else timeout - (self._clock() - started)
            if left is not None and left <= 0:  # one deadline for the whole connect phase, not one per address
                raise last or httpcore.ConnectTimeout("connect deadline")
            try:
                return DeadlineStream(self._inner.connect_tcp(ip, port, timeout=left, local_address=local_address,
                                                              socket_options=socket_options), self)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        raise last

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise NetError("URL_BLOCKED", "bad_authority")

    def sleep(self, seconds):
        time.sleep(seconds)


class _GuardedStream(httpx.SyncByteStream):
    """The body stream, with errors that surface while reading turned into NetError too."""

    def __init__(self, stream):
        self._stream = stream

    def __iter__(self):
        try:
            for chunk in self._stream:
                yield chunk
        except Exception as exc:  # noqa: BLE001
            raise classify(exc, body=True) from None

    def close(self):
        self._stream.close()


class PinnedTransport(httpx.BaseTransport):
    """httpx transport over `httpcore.ConnectionPool` with the pinned backend and an explicit TLS context
    (a custom transport does not inherit `trust_env` from the Client)."""

    def __init__(self, policy, limits, network_backend=None):
        backend = network_backend if network_backend is not None else PinnedBackend(policy)
        self._backend = backend
        context = httpx.create_ssl_context(verify=True, trust_env=False)
        context.keylog_filename = None  # the standard library reads SSLKEYLOGFILE: never write session secrets
        self._pool = httpcore.ConnectionPool(ssl_context=context, network_backend=backend, max_connections=1,
                                             http1=True, http2=False)

    def handle_request(self, request):
        url = request.url
        core = httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=url.raw_scheme, host=url.raw_host, port=url.port, target=url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions)
        if isinstance(self._backend, PinnedBackend):
            self._backend.deadline = request.extensions.get("deadline")
        try:
            response = self._pool.handle_request(core)
        except Exception as exc:  # noqa: BLE001
            raise classify(exc) from None
        return httpx.Response(status_code=response.status, headers=response.headers,
                              stream=_GuardedStream(response.stream), extensions=response.extensions)

    def close(self):
        self._pool.close()


class _NoCookies(http.cookiejar.DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


def build_client(policy, timeouts, transport=None):
    """A NEW client for one request: no redirects, no environment, no cookies, our User-Agent."""
    if transport is None:
        transport = PinnedTransport(policy, timeouts)
    client = httpx.Client(
        transport=transport, follow_redirects=False, trust_env=False,
        timeout=httpx.Timeout(connect=timeouts["connect_timeout"], read=timeouts["idle_timeout"],
                              write=timeouts["idle_timeout"], pool=timeouts["connect_timeout"]),
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    client.cookies.jar.set_policy(_NoCookies())
    return client


@dataclasses.dataclass
class FetchOutcome:
    status: str  # "ok" | "failed" | "blocked"
    code: str | None = None
    reason: str | None = None
    http_status: int | None = None
    retryable: bool = False
    redirects: int = 0
    final_url: str = None
    final_url_redacted: str = None
    headers_clean: dict = dataclasses.field(default_factory=dict)
    bytes_received: int = 0
    warnings: list = dataclasses.field(default_factory=list)
    timeout_kind: str | None = None  # a TIMEOUT only: "connect" | "idle" | "item" | "job" (FR14.2, FR14.5)


def _clean_header(value):
    text = "".join(ch for ch in value if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Zl", "Zp"))
    return text[:MAX_HEADER_TEXT]


def _headers_clean(headers):
    kept = {}
    for name in HEADERS_KEPT:
        value = headers.get(name)
        cleaned = _clean_header(value) if value is not None else ""
        if cleaned:
            kept[name] = cleaned
    return kept


def _content_length(headers):
    value = headers.get("content-length", "").strip()
    return int(value) if value.isdigit() else None


def _time_left(now, item_deadline, job_deadline):
    left = item_deadline - now
    if job_deadline is not None:
        left = min(left, job_deadline - now)
    return max(left, 0.001)


class _Run:
    """The state of one fetch_item call."""

    def __init__(self, expect, limits, sink, policy, client, total_remaining, clock, job_deadline):
        self.expect, self.limits, self.sink, self.policy, self.client = expect, limits, sink, policy, client
        self.total_remaining, self.clock, self.job_deadline = total_remaining, clock, job_deadline
        self.item_deadline = clock() + limits["item_timeout"]
        self.step = 0
        self.first_path = ""
        self.redirects = 0
        self.final_url = None
        self.headers = {}
        self.left = None  # the time the request in hand was allowed, set by `hop`

    def outcome(self, status, code=None, reason=None, http_status=None, **extra):
        if code == "NETWORK_ERROR":
            # a 5xx may pass; a 4xx, a forbidden answer and a certificate that does not verify will not by themselves
            retryable = http_status >= 500 if reason == "http_status" else reason not in ("forbidden", "tls_verify_failed")
        else:
            retryable = code in ("TIMEOUT", "RATE_LIMITED")
        return FetchOutcome(
            status=status, code=code, reason=reason, http_status=http_status, retryable=retryable,
            redirects=self.redirects, final_url=self.final_url,
            final_url_redacted=urlpolicy.redact(self.final_url) if self.final_url else None,
            headers_clean=self.headers, **extra)

    def blocked(self, reason):
        return self.outcome("blocked", "URL_BLOCKED" if self.step == 0 else "REDIRECT_BLOCKED", reason)

    def redirect_blocked(self, reason):
        return self.outcome("blocked", "REDIRECT_BLOCKED", reason)

    def late(self):
        now = self.clock()
        return now >= self.item_deadline or (self.job_deadline is not None and now >= self.job_deadline)

    def deadline_kind(self):
        """Which deadline ends the request first: the job's or the item's."""
        return "job" if self.job_deadline is not None and self.job_deadline <= self.item_deadline else "item"

    def timed_out(self, kind):
        return self.outcome("failed", "TIMEOUT", timeout_kind=kind)

    def hop(self, target):
        """One request. Returns (FetchOutcome, None) when finished, or (None, location) for a redirect."""
        if self.late():
            return self.timed_out(self.deadline_kind()), None
        own = self.client is None
        client = build_client(self.policy, self.limits) if own else self.client
        left = self.left = _time_left(self.clock(), self.item_deadline, self.job_deadline)
        timeout = httpx.Timeout(connect=min(self.limits["connect_timeout"], left),
                                read=min(self.limits["idle_timeout"], left),
                                write=min(self.limits["idle_timeout"], left),
                                pool=min(self.limits["connect_timeout"], left))
        client.cookies.clear()  # a client that was handed to us may hold cookies: none is ever sent
        try:
            with contextlib.closing(self.send(client, target.url, timeout, left)) as response:
                self.redirects = self.step
                self.headers = _headers_clean(response.headers)
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        return self.outcome("failed", "NETWORK_ERROR", "http_status", response.status_code), None
                    return None, location
                return self.read(response, target), None
        except NetError as exc:
            return self.net_failure(exc), None
        except (httpx.InvalidURL, httpx.RemoteProtocolError):
            # httpx itself reads the Location of every 3xx answer, even with follow_redirects off, and refuses
            # one it cannot parse (only a 3xx answer reaches that code: the transport speaks NetError)
            self.redirects = self.step
            return self.redirect_blocked("parser_error"), None
        finally:
            if own:
                client.close()

    def send(self, client, url, timeout, left):
        """One GET on `client`, whatever the client was set up with: no redirect following, no authentication, and
        no header beyond a fixed set (a handed-over client cannot add Cookie, Authorization, Proxy-Authorization..)."""
        request = client.build_request("GET", url, timeout=timeout, extensions={"deadline": time.monotonic() + left})
        for name in list(request.headers.keys()):  # only a fixed set of headers goes out, whatever the client carries
            if name.lower() not in ALLOWED_REQUEST_HEADERS:
                del request.headers[name]
        request.headers["host"] = request.url.netloc.decode("ascii")  # from the URL, never from the client
        request.headers["user-agent"] = USER_AGENT
        request.headers["accept"] = "*/*"
        request.headers["accept-encoding"] = "identity"
        request.headers["connection"] = "keep-alive"
        return client.send(request, stream=True, auth=None, follow_redirects=False)

    def net_failure(self, exc):
        if exc.code == "URL_BLOCKED":
            return self.blocked(exc.reason)
        if exc.code == "TIMEOUT":
            return self.timed_out(self.timeout_kind(exc.kind))
        return self.outcome("failed", exc.code, exc.reason)

    def timeout_kind(self, kind):
        """A wait that was cut short by the time left of the item or the job is a timeout of that deadline, not of
        the connect or idle wait it was part of."""
        if kind in ("connect", "idle") and self.left is not None and self.left <= self.limits[kind + "_timeout"]:
            kind = "deadline"
        return self.deadline_kind() if kind == "deadline" else kind

    def read(self, response, target):
        status = response.status_code
        if status in (401, 407):
            return self.outcome("failed", "AUTH_REQUIRED", http_status=status)
        if status == 403:  # often a site refusing this kind of client, not a login that is missing
            return self.outcome("failed", "NETWORK_ERROR", "forbidden", status)
        if status == 429:
            return self.outcome("failed", "RATE_LIMITED", http_status=status)
        if status != 200:  # 1xx, 204, any other 2xx (a partial 206 is not what was asked for), 3xx, 4xx, 5xx
            return self.outcome("failed", "NETWORK_ERROR", "http_status", status)
        encodings = [part.strip().lower() for part in response.headers.get("content-encoding", "").split(",")]
        if any(part not in ("", "identity") for part in encodings):
            return self.outcome("failed", "CONTENT_MISMATCH", "content_encoding_unsupported", status)
        declared = _content_length(response.headers)
        html_type = sniff.is_html_content_type(response.headers.get("content-type"))
        binary_url = self.expect != "html" and any(
            sniff.has_binary_extension(path) for path in (self.first_path, urlsplit(target.url).path))
        if binary_url and html_type:
            return self.outcome("failed", "CONTENT_MISMATCH", None, status)
        cap = self.limits["max_bytes_per_file"]
        if self.total_remaining is not None:
            cap = min(cap, self.total_remaining)
        if self.expect == "html" or html_type:
            cap = min(cap, self.limits["max_html_bytes"])
        if declared is not None and declared > cap:
            return self.outcome("failed", "SIZE_LIMIT", None, status)
        return self.body(response, status, binary_url, declared, cap)

    def take(self, chunk):
        """Hand a chunk to the sink within the limits. Returns an outcome to stop with, or None."""
        if self.declared is not None and self.received + len(chunk) > self.declared:
            chunk = chunk[:self.declared - self.received]  # exactly the declared length (h11 drops the excess itself)
        if self.received + len(chunk) > self.cap:
            return self.outcome("failed", "SIZE_LIMIT", None, self.http)
        self.sink(chunk)
        self.received += len(chunk)
        return None

    def body(self, response, status, binary_url, declared, cap):
        self.received, self.declared, self.cap, self.http = 0, declared, cap, status
        held, sniffing = b"", binary_url  # a binary file is judged on its first 512 bytes (or all of a shorter body)
        for chunk in response.iter_raw():
            if sniffing:
                held += chunk
                if len(held) < sniff.HEAD_BYTES:
                    continue
                sniffing, chunk, held = False, held, b""
                if sniff.looks_like_html(chunk):
                    return self.outcome("failed", "CONTENT_MISMATCH", None, status)
            stop = self.take(chunk)
            if stop is not None:
                return stop
            if declared is not None and self.received >= declared:
                break
            if self.late():
                return self.outcome("failed", "TIMEOUT", None, status, timeout_kind=self.deadline_kind())
        if sniffing and held:
            if sniff.looks_like_html(held):
                return self.outcome("failed", "CONTENT_MISMATCH", None, status)
            stop = self.take(held)
            if stop is not None:
                return stop
        if declared is not None and self.received < declared:
            return self.outcome("failed", "NETWORK_ERROR", "truncated_body", status)
        return self.outcome("ok", http_status=status, bytes_received=self.received)


def fetch_item(url, expect, limits, sink, policy, client=None, *, total_remaining=None, clock=time.monotonic,
               job_deadline=None):
    """Download one URL into `sink(chunk)`. Returns a FetchOutcome; never raises for a network or policy
    problem. `redirects` counts the redirects that were followed (a response received after the first).
    `client` is only a hook for tests (a client handed over is still held back: no redirects, no cookies, no
    authentication, a fixed set of headers); the command line and the launcher never pass one."""
    run = _Run(expect, limits, sink, policy, client, total_remaining, clock, job_deadline)
    try:
        target = urlpolicy.check_url(url, policy)
    except urlpolicy.Blocked as exc:
        return run.blocked(exc.reason)
    run.first_path = urlsplit(target.url).path
    seen = set()
    while True:
        seen.add(target.url)
        run.final_url = target.url
        outcome, location = run.hop(target)
        if outcome is not None:
            return outcome
        if run.step >= limits["max_redirects"]:
            return run.redirect_blocked("redirect_limit")
        try:
            following = urlpolicy.check_url(urljoin(target.url, location), policy)
        except (urlpolicy.Blocked, ValueError) as exc:  # ValueError: urljoin on a Location that is not a URL
            return run.redirect_blocked(getattr(exc, "reason", "parser_error"))
        if target.scheme == "https" and following.scheme != "https":
            return run.redirect_blocked("downgrade")
        if following.url in seen:
            return run.redirect_blocked("redirect_loop")
        target = following
        run.step += 1
