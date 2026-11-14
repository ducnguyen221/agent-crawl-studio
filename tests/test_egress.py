"""EgressProxy: per-item CONNECT proxy with a token, a public-IP filter and a byte cap (SPEC FR13.1-FR13.5, FR13.8;
PLAN TK18). RED until TK19 writes `agent_crawl_studio.egress`.

The "Internet" is one local TCP server (`_Origin`). A subclass of the real `PinnedBackend` sends every connection
there while recording the literal IP it was asked to connect to, so the real resolve-filter-pin code runs."""

import socket
import threading
import time

import httpcore
import pytest

from agent_crawl_studio import egress, netfetch
from agent_crawl_studio.egress import EgressProxy
from agent_crawl_studio.urlpolicy import Policy

TOKEN = "ab" * 32
PUBLIC = "93.184.216.34"
NAMES = {"cdn.example.com": PUBLIC, "private.example.com": "10.0.0.1", "meta.example.com": "169.254.169.254",
         "mapped.example.com": "::ffff:127.0.0.1", "nat64.example.com": "64:ff9b::7f00:1"}


class _Origin:
    """Echoes what it receives, or (push set) sends `push` at once and then only listens."""

    def __init__(self, push=None, hold=False):
        self.push = push
        self.hold = hold  # keeps the connection after the client's EOF (a half-closed tunnel)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.2)
        self.stopped = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stopped:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            if self.push is not None:
                conn.sendall(self.push)
            while True:
                data = conn.recv(65536)
                if not data:
                    while self.hold and not self.stopped:
                        time.sleep(0.05)
                    break
                if self.push is None:
                    conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()

    def stop(self):
        self.stopped = True
        self.sock.close()


class _Bare:
    def get_extra_info(self, name):
        return None

    def close(self):
        pass


class _Rig:
    def __init__(self, origin):
        self.origin = origin
        self.log = []
        self.lookups = []
        self.pinned = []
        self.proxies = []
        self.flip = 0
        self.fail = None  # an exception the pinned connect raises
        self.gate = None  # an Event the pinned connect waits for
        self.streams = []
        self.bare = False  # the connect returns a stream that has no socket

    def resolver(self, host, port, proto=0):
        self.lookups.append(host)
        if host == "flip.example.com":
            self.flip += 1
            ip = PUBLIC if self.flip == 1 else "127.0.0.1"
        elif host in NAMES:
            ip = NAMES[host]
        else:
            raise socket.gaierror(11001, "no such name")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    def backend_of(self, policy):
        rig = self

        class Inner(httpcore.SyncBackend):
            def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
                rig.pinned.append(host)
                if rig.gate is not None:
                    rig.gate.wait(10)
                if rig.fail is not None:
                    raise rig.fail
                if rig.bare:
                    return _Bare()
                stream = super().connect_tcp("127.0.0.1", rig.origin.port, timeout=timeout)
                rig.streams.append(stream)
                return stream

        backend = netfetch.PinnedBackend(policy)
        backend._inner = Inner()
        return backend

    def make(self, **kw):
        kw.setdefault("byte_cap", 2 ** 20)
        kw.setdefault("connect_timeout", 5.0)
        policy = Policy(ports=(443,), resolver=self.resolver)
        proxy = EgressProxy(policy, log=self.log.append, token_of=lambda n: TOKEN, backend_of=self.backend_of, **kw)
        proxy.start()
        self.proxies.append(proxy)
        return proxy


@pytest.fixture
def rig():
    origin = _Origin()
    r = _Rig(origin)
    yield r
    for proxy in r.proxies:
        proxy.stop()
    origin.stop()


def _wait(condition, seconds=5.0):
    """Polls until `condition()` holds (the log line is written after the reply); False when it never does."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def _port(proxy):
    return int(proxy.url.rsplit(":", 1)[1])


def _auth(token=TOKEN):
    import base64
    return b"Proxy-Authorization: Basic " + base64.b64encode(("%s:x" % token).encode()) + b"\r\n"


def _connect(proxy, target, auth=True, raw=None):
    """An open socket after sending CONNECT (or `raw`)."""
    s = socket.create_connection(("127.0.0.1", _port(proxy)), timeout=5)
    s.sendall(raw if raw is not None else b"CONNECT %s HTTP/1.1\r\nHost: %s\r\n%s\r\n" % (
        target.encode(), target.encode(), _auth() if auth else b""))
    return s


def _head(s):
    buf = b""
    while b"\r\n\r\n" not in buf:
        data = s.recv(4096)
        if not data:
            break
        buf += data
    first, _, rest = buf.partition(b"\r\n")
    head, _, body = rest.partition(b"\r\n\r\n")
    return (int(first.split()[1]) if first else 0), head.decode("latin-1"), body


def _drain(s):
    total = 0
    s.settimeout(5)
    try:
        while True:
            data = s.recv(65536)
            if not data:
                break
            total += len(data)
    except OSError:
        pass
    return total


def test_token_required(rig):
    proxy = rig.make()
    s = _connect(proxy, "cdn.example.com:443", auth=False)
    code, head, _ = _head(s)
    assert code == 407 and 'Proxy-Authenticate: Basic realm="crawl-studio"' in head
    s = socket.create_connection(("127.0.0.1", _port(proxy)), timeout=5)
    s.sendall(b"CONNECT cdn.example.com:443 HTTP/1.1\r\n" + _auth("cd" * 32) + b"\r\n")
    assert _head(s)[0] == 407
    assert not rig.pinned


def test_only_connect_method(rig):
    s = _connect(rig.make(), "", raw=b"GET http://x/ HTTP/1.1\r\n" + _auth() + b"\r\n")
    assert _head(s)[0] == 405


@pytest.mark.parametrize("target,reason", [
    ("127.0.0.1:443", "ip_literal"), ("[::1]:443", "ip_literal"), ("cdn.example.com:80", "port"),
    ("cdn.example.com:0443", "port"), ("a@b.com:443", "host"), ("private.example.com:443", "ip_not_public"),
    ("meta.example.com:443", "ip_not_public"), ("mapped.example.com:443", "ip_not_public"),
    ("nat64.example.com:443", "ip_not_public"),
    ("127.1:443", "host"), ("0x7f000001:443", "host"), ("2130706433:443", "host"), ("localhost:443", "host"),
    ("a..b.com:443", "host"), ("-x.com:443", "host")])
def test_refused_targets_get_403(rig, target, reason):
    proxy = rig.make()
    assert _head(_connect(proxy, target))[0] == 403
    assert [r for _, r in proxy.denied] == [reason]
    assert _wait(lambda: any(line.split()[2] == "deny:" + reason for line in rig.log))
    assert not rig.pinned


def test_unresolvable_name_is_502_not_a_silent_close(rig):
    proxy = rig.make()
    assert _head(_connect(proxy, "nowhere.example.com:443"))[0] == 502
    assert proxy.denied == [] and proxy.failures == [("nowhere.example.com", "dns_failed")]  # RF1: the reason is kept


@pytest.mark.parametrize("error,reason", [
    (httpcore.ConnectError("refused"), "connect"), (httpcore.ConnectTimeout("slow"), "timeout"),
    (netfetch.NetError("TIMEOUT"), "timeout")])
def test_a_connect_that_fails_is_a_502_with_its_reason_kept(rig, error, reason):
    rig.fail = error
    proxy = rig.make()
    assert _head(_connect(proxy, "cdn.example.com:443"))[0] == 502
    assert proxy.failures == [("cdn.example.com", reason)] and proxy.denied == []
    assert _wait(lambda: rig.log and rig.log[0] == "cdn.example.com 443 deny:%s 0" % reason)


def test_denied_and_failures_are_bounded(rig):
    proxy = rig.make()
    for _ in range(egress.MAX_RECORDS + 20):
        _head(_connect(proxy, "127.0.0.1:443"))
    assert len(proxy.denied) == egress.MAX_RECORDS
    rig.fail = httpcore.ConnectError("x")
    for _ in range(egress.MAX_RECORDS + 5):
        _head(_connect(proxy, "cdn.example.com:443"))
    assert len(proxy.failures) == egress.MAX_RECORDS


def test_a_plain_http_request_records_the_host_it_named(rig):  # RF7: a redirect to http://internal/
    proxy = rig.make()
    for raw_target in ("http://internal.example.com/x?y=1", "http://u:p@Other.Example.com:8080/", "http://[::1]/",
                       "http://10.0.0.1/"):
        s = _connect(proxy, "", raw=b"GET %s HTTP/1.1\r\n" % raw_target.encode() + _auth() + b"\r\n")
        assert _head(s)[0] == 405
    assert proxy.denied == [("internal.example.com", "http_downgrade"), ("other.example.com", "http_downgrade"),
                            ("-", "http_downgrade"), ("-", "http_downgrade")]
    assert _wait(lambda: len(rig.log) == 4)
    assert "internal.example.com 80 deny:http_downgrade 0" in rig.log
    assert "other.example.com 8080 deny:http_downgrade 0" in rig.log
    s = _connect(proxy, "", raw=b"GET /relative HTTP/1.1\r\n" + _auth() + b"\r\n")
    assert _head(s)[0] == 405 and len(proxy.denied) == 4  # no named host: the old "method" refusal, not recorded
    assert _wait(lambda: rig.log[-1] == "- - deny:method 0")


def test_tunnels_are_taken_after_the_token_not_before(rig):  # security RF1
    proxy = rig.make(max_tunnels=16)
    silent = [socket.create_connection(("127.0.0.1", _port(proxy)), timeout=5) for _ in range(16)]
    try:
        s = _connect(proxy, "cdn.example.com:443")
        assert _head(s)[0] == 200
        s.close()
    finally:
        for sock in silent:
            sock.close()


def test_connections_before_the_token_have_their_own_bound(rig):
    proxy = rig.make(max_pending=2, header_timeout=0.5)
    silent = [socket.create_connection(("127.0.0.1", _port(proxy)), timeout=5) for _ in range(2)]
    try:
        assert _wait(lambda: len(proxy._threads) == 2)
        assert _head(_connect(proxy, "cdn.example.com:443"))[0] == 503
        assert [s.recv(10) for s in silent] == [b"", b""]  # the silent ones are closed at the header timeout
        assert _wait(lambda: _head(_connect(proxy, "cdn.example.com:443"))[0] == 200)  # and the places are back
    finally:
        for sock in silent:
            sock.close()


def test_one_lookup_per_connect_and_connects_to_the_checked_ip(rig):
    proxy = rig.make()
    s1 = _connect(proxy, "flip.example.com:443")
    assert _head(s1)[0] == 200
    assert _head(_connect(proxy, "flip.example.com:443"))[0] == 403  # the second answer is 127.0.0.1
    assert rig.lookups.count("flip.example.com") == 2
    assert rig.pinned == [PUBLIC]
    s1.close()


def test_tunnel_roundtrip_and_counter_over_two_tunnels(rig):
    proxy = rig.make()
    socks = [_connect(proxy, "cdn.example.com:443"), _connect(proxy, "cdn.example.com:443")]
    for s in socks:
        assert _head(s)[0] == 200
    payload = bytes(range(256)) * 4
    for s in socks:
        s.sendall(payload)
    for s in socks:
        got = b""
        while len(got) < len(payload):
            got += s.recv(4096)
        assert got == payload
        s.close()
    assert proxy.bytes_in == 2 * len(payload)
    assert _wait(lambda: sum(1 for line in rig.log if " allow " in line) == 2)


def test_byte_cap_closes_tunnel_and_sets_event(rig):
    rig.origin.stop()
    rig.origin = _Origin(push=b"x" * (400 * 1024))
    proxy = rig.make(byte_cap=10 * 1024)
    s = _connect(proxy, "cdn.example.com:443")
    assert _head(s)[0] == 200
    got = _drain(s)
    assert proxy.over_cap.wait(5) and got < 400 * 1024


def test_each_item_has_its_own_counter(rig):
    first = rig.make()
    second = rig.make()
    s = _connect(first, "cdn.example.com:443")
    _head(s)
    s.sendall(b"hello")
    s.recv(16)
    assert first.bytes_in == 5 and second.bytes_in == 0 and not second.over_cap.is_set()
    s.close()


def test_slow_header_is_closed(rig):
    proxy = rig.make(header_timeout=0.3)
    s = socket.create_connection(("127.0.0.1", _port(proxy)), timeout=5)
    assert s.recv(100) == b""


def test_tunnel_limit_gives_503(rig):
    proxy = rig.make(max_tunnels=2)
    held = [_connect(proxy, "cdn.example.com:443") for _ in range(2)]
    for s in held:
        assert _head(s)[0] == 200
    assert _head(_connect(proxy, "cdn.example.com:443"))[0] == 503


def test_long_request_line_is_400(rig):
    s = _connect(rig.make(), "", raw=b"CONNECT " + b"a" * 9000 + b":443 HTTP/1.1\r\n\r\n")
    assert _head(s)[0] == 400


def test_stop_closes_port_and_threads(rig):
    before = {t for t in threading.enumerate() if t.name.startswith("egress")}
    proxy = rig.make()
    s = _connect(proxy, "cdn.example.com:443")
    assert _head(s)[0] == 200
    port = _port(proxy)
    started = time.monotonic()
    proxy.stop()
    assert time.monotonic() - started < 2.5
    assert [t for t in threading.enumerate() if t.name.startswith("egress") and t not in before] == []
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)
    s.close()


def test_log_never_has_the_token_and_bad_host_is_a_dash(rig):
    proxy = rig.make()
    _head(_connect(proxy, "a@b.com:443"))
    _head(_connect(proxy, "cdn.example.com:443", auth=False))
    assert _wait(lambda: len(rig.log) == 2)
    assert any(line.startswith("- ") and "deny:host" in line for line in rig.log)
    assert not any(TOKEN in line for line in rig.log)


def test_malformed_requests(rig):
    proxy = rig.make()
    assert _head(_connect(proxy, "", raw=b"CONNECT nowhere\r\n\r\n"))[0] == 400
    bad = b"CONNECT cdn.example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic !!notbase64!!\r\n\r\n"
    assert _head(_connect(proxy, "", raw=bad))[0] == 407
    twice = b"CONNECT cdn.example.com:443 HTTP/1.1\r\n" + _auth() + _auth() + b"\r\n"
    assert _head(_connect(proxy, "", raw=twice))[0] == 407
    assert _head(_connect(proxy, "cdn.example.com"))[0] == 403  # no port at all
    s = socket.create_connection(("127.0.0.1", _port(proxy)), timeout=5)
    s.close()  # hangs up before any header


def test_after_the_cap_new_connects_are_refused_and_a_broken_log_is_survived(rig):
    def broken(line):
        raise OSError("log full")

    proxy = EgressProxy(Policy(ports=(443,), resolver=rig.resolver), byte_cap=1, connect_timeout=5.0, log=broken,
                        token_of=lambda n: TOKEN, backend_of=rig.backend_of)
    with proxy:
        rig.proxies.append(proxy)
        with pytest.raises(RuntimeError):
            proxy.start()
        s = _connect(proxy, "cdn.example.com:443")
        _head(s)
        s.sendall(b"abc")
        assert proxy.over_cap.wait(5)
        assert _head(_connect(proxy, "cdn.example.com:443"))[0] == 403
        s.close()


class _FlakyListener:
    """Wraps the real listening socket; the first `n` accepts fail like a reset in the queue."""

    def __init__(self, real, n):
        self.real, self.n = real, n

    def accept(self):
        if self.n > 0:
            self.n -= 1
            raise OSError(10054, "reset in the queue")
        return self.real.accept()

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_a_transient_accept_error_does_not_end_the_acceptor(rig):  # RF2
    proxy = rig.make()
    proxy._listener = _FlakyListener(proxy._listener, 2)
    assert _wait(lambda: rig.log.count("- - deny:accept 0") == 2)
    assert _head(_connect(proxy, "cdn.example.com:443"))[0] == 200
    proxy.stop()
    assert rig.log.count("- - deny:accept 0") == 2  # a stop is not an accept failure: nothing more is said


def test_an_acceptor_whose_socket_is_gone_gives_up_after_a_bound(rig, monkeypatch):
    monkeypatch.setattr(egress, "MAX_ACCEPT_ERRORS", 3)
    proxy = rig.make()
    proxy._listener = _FlakyListener(proxy._listener, 10 ** 6)
    proxy._acceptor.join(5)
    assert not proxy._acceptor.is_alive() and rig.log.count("- - deny:accept 0") == 3


def test_an_unexpected_error_in_a_connection_is_a_502_and_one_line(rig, monkeypatch):  # RF3
    proxy = rig.make()

    def boom(target):
        raise RuntimeError("surprise")

    monkeypatch.setattr(proxy, "_check_target", boom)
    assert _head(_connect(proxy, "cdn.example.com:443"))[0] == 502
    assert _wait(lambda: rig.log == ["- - deny:error 0"])
    monkeypatch.undo()
    assert _head(_connect(proxy, "cdn.example.com:443"))[0] == 200  # the proxy goes on


def test_a_tunnel_that_breaks_leaves_one_line_with_the_bytes_so_far(rig, monkeypatch):  # RF3
    proxy = rig.make()

    def broken(client, up, rest, ctx):
        ctx.count = 7
        raise OSError("select on a closed socket")

    monkeypatch.setattr(proxy, "_pump", broken)
    s = _connect(proxy, "cdn.example.com:443")
    assert _head(s)[0] == 200
    assert _wait(lambda: rig.log == ["cdn.example.com 443 deny:error 7"])
    s.close()


def test_bytes_sent_with_the_connect_reach_the_far_side(rig):  # RF9 (the `rest` branch)
    proxy = rig.make()
    raw = b"CONNECT cdn.example.com:443 HTTP/1.1\r\n" + _auth() + b"\r\nhello"
    s = _connect(proxy, "", raw=raw)
    code, _, got = _head(s)
    assert code == 200
    while len(got) < 5:
        got += s.recv(16)
    assert got == b"hello"
    s.close()


def test_the_other_side_has_a_grace_after_one_side_finished(rig, monkeypatch):  # RF9 (the grace branch)
    rig.origin.stop()
    rig.origin = _Origin(hold=True)
    monkeypatch.setattr(egress, "HALF_CLOSE_GRACE", 0.3)
    proxy = rig.make()
    s = _connect(proxy, "cdn.example.com:443")
    assert _head(s)[0] == 200
    s.shutdown(socket.SHUT_WR)
    started = time.monotonic()
    assert _drain(s) == 0 and time.monotonic() - started < 3
    assert _wait(lambda: any(" allow " in line for line in rig.log))
    s.close()


def test_a_port_that_cannot_be_opened_raises_and_leaves_nothing(rig, monkeypatch):  # RF9 (the bind failure)
    monkeypatch.setattr(egress, "LISTEN_HOST", "999.0.0.1")
    proxy = EgressProxy(Policy(ports=(443,), resolver=rig.resolver), byte_cap=1, connect_timeout=1.0,
                        log=rig.log.append, backend_of=rig.backend_of)
    with pytest.raises(OSError):
        proxy.start()
    assert proxy.url is None and proxy._listener is None


def test_a_connect_that_finishes_after_stop_is_closed_not_kept(rig):  # RF4
    rig.gate = threading.Event()
    proxy = rig.make()
    s = _connect(proxy, "cdn.example.com:443")
    assert _wait(lambda: len(proxy._threads) == 1 and len(rig.pinned) == 1)  # the thread is inside the connect
    started = time.monotonic()
    proxy.stop(timeout=0.4)
    assert time.monotonic() - started < 1.5  # the wait is bounded by `timeout`, not by the connect
    rig.gate.set()  # the connect now succeeds, after `stop()` returned
    assert _wait(lambda: len(rig.streams) == 1 and any("deny:stopped" in line for line in rig.log))
    assert _wait(lambda: rig.streams[0].get_extra_info("socket").fileno() == -1)  # no open connection stays behind
    s.close()


def test_nothing_is_tracked_once_stopping(rig):  # RF4
    proxy = rig.make()
    proxy.stop()
    sock = socket.socket()
    assert proxy._track(sock) is False and sock not in proxy._sockets
    sock.close()


def test_a_stream_without_a_socket_is_a_502(rig):
    rig.bare = True
    proxy = rig.make()
    assert _head(_connect(proxy, "cdn.example.com:443"))[0] == 502
    assert proxy.failures == [("cdn.example.com", "connect")]
    assert _wait(lambda: rig.log == ["cdn.example.com 443 deny:connect 0"])


def test_a_plain_request_with_an_impossible_port_is_still_a_downgrade(rig):
    proxy = rig.make()
    s = _connect(proxy, "", raw=b"GET http://host.example.com:99999999/ HTTP/1.1\r\n" + _auth() + b"\r\n")
    assert _head(s)[0] == 405 and proxy.denied == [("-", "http_downgrade")]


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")  # the acceptor dies on purpose
def test_a_thread_that_cannot_start_gives_its_places_back(rig, monkeypatch):
    real = threading.Thread

    class NoStart(real):
        def start(self):
            if self.name == "egress-conn":
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(egress.threading, "Thread", NoStart)
    proxy = rig.make(max_pending=1)
    s = socket.create_connection(("127.0.0.1", _port(proxy)), timeout=5)
    assert s.recv(10) == b""  # the connection was closed
    proxy._acceptor.join(5)
    assert not proxy._acceptor.is_alive() and proxy._pending.acquire(blocking=False)  # the place is free again
    s.close()
