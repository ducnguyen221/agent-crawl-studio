"""EgressProxy: per-item CONNECT proxy with a token, a public-IP filter and a byte cap (SPEC FR13.1-FR13.5, FR13.8;
PLAN TK18). RED until TK19 writes `agent_crawl_studio.egress`.

The "Internet" is one local TCP server (`_Origin`). A subclass of the real `PinnedBackend` sends every connection
there while recording the literal IP it was asked to connect to, so the real resolve-filter-pin code runs."""

import socket
import threading
import time

import httpcore
import pytest

from agent_crawl_studio import netfetch
from agent_crawl_studio.egress import EgressProxy
from agent_crawl_studio.urlpolicy import Policy

TOKEN = "ab" * 32
PUBLIC = "93.184.216.34"
NAMES = {"cdn.example.com": PUBLIC, "private.example.com": "10.0.0.1", "meta.example.com": "169.254.169.254",
         "mapped.example.com": "::ffff:127.0.0.1", "nat64.example.com": "64:ff9b::7f00:1"}


class _Origin:
    """Echoes what it receives, or (push set) sends `push` at once and then only listens."""

    def __init__(self, push=None):
        self.push = push
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


class _Rig:
    def __init__(self, origin):
        self.origin = origin
        self.log = []
        self.lookups = []
        self.pinned = []
        self.proxies = []
        self.flip = 0

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
                return super().connect_tcp("127.0.0.1", rig.origin.port, timeout=timeout)

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
    ("nat64.example.com:443", "ip_not_public")])
def test_refused_targets_get_403(rig, target, reason):
    proxy = rig.make()
    assert _head(_connect(proxy, target))[0] == 403
    assert [r for _, r in proxy.denied] == [reason]
    time.sleep(0.2)
    assert any(line.split()[2] == "deny:" + reason for line in rig.log)
    assert not rig.pinned


def test_unresolvable_name_is_502_not_a_silent_close(rig):
    proxy = rig.make()
    assert _head(_connect(proxy, "nowhere.example.com:443"))[0] == 502
    assert proxy.denied == []


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
    time.sleep(0.5)
    assert sum(1 for line in rig.log if " allow " in line) == 2


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
    time.sleep(0.3)
    assert rig.log[0].startswith("- ") and "deny:host" in rig.log[0]
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
