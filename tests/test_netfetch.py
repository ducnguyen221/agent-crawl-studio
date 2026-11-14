"""netfetch: HTTP(S) download with a pinned IP, redirects checked at every step, TLS that cannot be turned off
(SPEC FR2.4-FR2.7, FR3.1-FR3.4, FR3.8-FR3.9; PLAN TK12). RED until TK13 writes `agent_crawl_studio.netfetch`.

Interface these tests pin (PROGRESS, Phan quyet TK12; PLAN TK13 gives the same shape):

    build_client(policy, timeouts, transport=None) -> httpx.Client      # `timeouts` = effective limits dict
    fetch_item(url, expect, limits, sink, policy, client=None, *, total_remaining=None,
               clock=time.monotonic, job_deadline=None) -> FetchOutcome
    FetchOutcome: status ("ok" | "failed" | "blocked"), code (error code or None), reason, http_status, retryable,
                  redirects (redirects FOLLOWED), final_url, final_url_redacted, headers_clean, bytes_received, warnings
    sink(chunk: bytes) is called with body bytes only; on a failed outcome the caller (TK15) discards the .part
    PinnedBackend(policy) (httpcore.NetworkBackend) ; PinnedTransport(policy, limits, network_backend=None)
    NetError(code, reason): what the transport raises instead of any httpcore.* exception

Policy is injected by parameters only (Policy(ports=..., ip_ok=..., resolver=...)); there is no switch that turns
the policy off. Loopback servers stand in for the Internet: `pinned.example` is a name the fake resolver maps to
127.0.0.1, and `ip_ok` is the injected notion of "public" (only 127.0.0.1)."""

import dataclasses
import inspect
import socket
import ssl
import sys
import time
from pathlib import Path

import httpcore
import httpx
import pytest

import agent_crawl_studio
from agent_crawl_studio import errors, limits as limits_mod, urlpolicy
from agent_crawl_studio.urlpolicy import Policy

from fixtures.httpd import LocalServer
from fixtures.tls import TlsUnavailable, make_cert

LOOP = "127.0.0.1"
MiB = 2 ** 20
HOST = "files.example.com"


# ----------------------------------------------------------------------------- helpers
@pytest.fixture
def nf():
    """The module under test, imported lazily so that every test is red on its own (not one collection error)."""
    import agent_crawl_studio.netfetch as module
    return module


def loop_only(address):
    return address == LOOP


class Resolver:
    """Fake `policy.resolver`: table maps host -> list of answers, one per call (the last one repeats). An answer is
    a list of IP strings, an Exception to raise, or a callable run first (for a slow resolver). Records every call."""

    def __init__(self, table=None, default=None):
        self.table = table or {}
        self.default = default
        self.calls = []

    def __call__(self, host, port, proto=0, *args, **kwargs):
        self.calls.append(host)
        answers = self.table.get(host, self.default)
        if answers is None:
            raise AssertionError("the resolver was asked about %r and must not have been" % host)
        answer = answers[min(self.calls.count(host), len(answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            answer = answer()
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answer]


def hosts(*names):
    return {name: [[LOOP]] for name in names}


def policy_for(server, resolver=None, ok=loop_only, extra_ports=()):
    resolver = resolver if resolver is not None else Resolver(hosts("pinned.example", "other.example"))
    return Policy(ports=(server.port,) + tuple(extra_ports), ip_ok=ok, resolver=resolver)


def lim(**override):
    return limits_mod.resolve(override)[0]


class Sink:
    """The body consumer. `clock`/`advance`: every chunk moves an injected clock forward (to test deadlines)."""

    def __init__(self, clock=None, advance=0):
        self.chunks = []
        self.clock, self.advance = clock, advance

    def __call__(self, chunk):
        self.chunks.append(bytes(chunk))
        if self.clock is not None:
            self.clock.now += self.advance

    @property
    def data(self):
        return b"".join(self.chunks)


def fetch(nf, url, policy, expect="auto", client=None, sink=None, extra=None, **limit_overrides):
    sink = sink if sink is not None else Sink()
    out = nf.fetch_item(url, expect, lim(**limit_overrides), sink, policy, client=client, **(extra or {}))
    return out, sink


def failed(out, code, reason=None, retryable=None, http_status=None, status="failed"):
    assert out.status == status and out.code == code, (out.status, out.code, out.reason)
    if reason is not None:
        assert out.reason == reason
    if retryable is not None:
        assert out.retryable is retryable
    if http_status is not None:
        assert out.http_status == http_status


@pytest.fixture
def server():
    with LocalServer() as srv:
        yield srv


@pytest.fixture
def tls_server(tmp_path):
    try:
        crt, key = make_cert(tmp_path)
    except TlsUnavailable as exc:
        pytest.skip("BLOCKED_ENV: %s" % exc)
    with LocalServer(tls=(str(crt), str(key))) as srv:
        yield srv, str(crt)


@pytest.fixture
def connects(monkeypatch):
    """Every address any socket in the process connects to (the pinned connection must be the only one)."""
    calls = []
    real = socket.socket.connect

    def spy(self, address, *args):
        calls.append(address)
        return real(self, address, *args)

    monkeypatch.setattr(socket.socket, "connect", spy)
    return calls


@pytest.fixture
def names_resolved(monkeypatch):
    """Names given to the real socket.getaddrinfo (the pinned connect must use the checked IP, never the name)."""
    seen = []
    real = socket.getaddrinfo

    def spy(host, *args, **kwargs):
        seen.append(host)
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", spy)
    return seen


# ----------------------------------------------------------------------------- pinned versions
def test_the_transport_is_written_against_these_exact_library_versions():
    assert httpx.__version__ == "0.28.1"
    assert httpcore.__version__ == "1.0.9"


# ----------------------------------------------------------------------------- shape of the interface
def test_build_client_returns_a_new_client_with_the_fixed_settings(nf, server):
    policy = policy_for(server)
    one, two = nf.build_client(policy, lim()), nf.build_client(policy, lim())
    assert isinstance(one, httpx.Client) and one is not two
    assert one.follow_redirects is False and one.trust_env is False
    assert one.headers["accept-encoding"] == "identity"
    assert one.headers["user-agent"] == "agent-crawl-studio/%s (OpcOS local tool)" % agent_crawl_studio.__version__
    assert not one.headers["user-agent"].startswith("python-httpx")
    assert len(one.cookies.jar) == 0
    one.close(), two.close()


def test_build_client_maps_the_timeouts_from_the_limits(nf, server):
    client = nf.build_client(policy_for(server), lim(connect_timeout=7, idle_timeout=11))
    assert client.timeout.connect == 7 and client.timeout.read == 11
    client.close()


def test_the_interface_has_exactly_the_documented_parameters_and_no_switch(nf):
    assert list(inspect.signature(nf.build_client).parameters) == ["policy", "timeouts", "transport"]
    assert list(inspect.signature(nf.fetch_item).parameters) == [
        "url", "expect", "limits", "sink", "policy", "client", "total_remaining", "clock", "job_deadline", "network_room"]
    assert list(inspect.signature(nf.PinnedBackend).parameters) == ["policy", "clock"]
    assert list(inspect.signature(nf.PinnedTransport).parameters) == ["policy", "limits", "network_backend"]


def test_the_outcome_has_the_documented_fields(nf):
    names = {field.name for field in dataclasses.fields(nf.FetchOutcome)}
    assert {"status", "code", "reason", "http_status", "retryable", "redirects", "final_url", "final_url_redacted",
            "headers_clean", "bytes_received", "warnings"} <= names


def test_the_source_reads_no_environment_and_never_turns_verification_off(nf):
    text = Path(inspect.getsourcefile(nf)).read_text(encoding="utf-8")
    for needle in ("os.environ", "getenv", "verify=False", "trust_env=True", "_create_unverified_context",
                   "CERT_NONE", "check_hostname = False", "check_hostname=False", "follow_redirects=True", "proxies"):
        assert needle not in text, needle


# ----------------------------------------------------------------------------- what is sent
def test_a_plain_download_sends_our_user_agent_identity_and_nothing_secret(nf, server):
    out, sink = fetch(nf, server.url("/file?size=200000"), policy_for(server))
    assert out.status == "ok" and out.code is None and out.reason is None and out.http_status == 200
    assert sink.data == b"x" * 200000 and out.bytes_received == 200000 and out.redirects == 0
    sent = server.requests[0]["headers"]
    assert sent["user-agent"] == "agent-crawl-studio/%s (OpcOS local tool)" % agent_crawl_studio.__version__
    assert sent["accept-encoding"] == "identity"
    assert "cookie" not in sent and "authorization" not in sent and "referer" not in sent
    assert sent["host"] == "pinned.example:%d" % server.port


def test_only_the_normalised_url_reaches_the_wire(nf, server):
    out, _ = fetch(nf, server.url("/file?size=3&x=1#fragment", host="PINNED.Example").replace("http://", "HTTP://"),
                   policy_for(server))
    assert out.status == "ok"
    assert server.requests[0]["path"] == "/file?size=3&x=1"
    assert server.requests[0]["headers"]["host"] == "pinned.example:%d" % server.port


def test_outcome_headers_are_a_clean_allow_list_without_set_cookie(nf, server):
    out, _ = fetch(nf, server.url("/ok"), policy_for(server))
    assert set(out.headers_clean) <= {"content-type", "content-disposition", "last-modified", "retry-after"}
    assert out.headers_clean["content-type"] == "text/plain"
    assert "sid=abc" not in repr(out)


def test_untrusted_header_text_is_cleaned_and_shortened(nf, server):
    out, _ = fetch(nf, server.url("/hdr"), policy_for(server))
    disposition = out.headers_clean["content-disposition"]
    assert "\u202e" not in disposition and "\x85" not in disposition and len(disposition) <= 200
    assert disposition.startswith("attachment") and out.headers_clean["last-modified"].startswith("Wed, 21 Oct 2015")


def test_the_proxy_netrc_and_certificate_environment_is_ignored(nf, server, monkeypatch, tmp_path):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine pinned.example login someone password hunter2\n", encoding="ascii")
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nobody listens: a proxy would break the download
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "nothing.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "nothing.pem"))
    out, sink = fetch(nf, server.url("/ok"), policy_for(server))
    assert out.status == "ok" and sink.data == b"hello"
    assert server.requests[0]["path"] == "/ok" and "authorization" not in server.requests[0]["headers"]


# ----------------------------------------------------------------------------- pinned IP (FR2.4)
def test_it_connects_only_to_the_checked_address_and_resolves_once(nf, server, connects, names_resolved):
    resolver = Resolver({"pinned.example": [[LOOP], ["10.9.9.9"]]})  # the second answer must never be asked for
    out, sink = fetch(nf, server.url("/ok"), policy_for(server, resolver))
    assert out.status == "ok" and sink.data == b"hello"
    assert resolver.calls == ["pinned.example"]
    assert connects == [(LOOP, server.port)]
    assert "pinned.example" not in names_resolved  # the connect used the checked IP, never the name


def test_dns_that_changes_after_the_check_cannot_reach_a_refused_address(nf, server, connects):
    resolver = Resolver({"pinned.example": [["10.9.9.9"], [LOOP]]})  # first answer refused, second would be loopback
    out, _ = fetch(nf, server.url("/ok"), policy_for(server, resolver))
    failed(out, "URL_BLOCKED", "ip_not_public", status="blocked")
    assert resolver.calls == ["pinned.example"] and connects == [] and server.connections == 0


def test_with_mixed_answers_only_the_public_one_is_used(nf, server, connects):
    resolver = Resolver({"pinned.example": [["10.0.0.5", "192.168.1.1", LOOP, "169.254.169.254"]]})
    out, sink = fetch(nf, server.url("/ok"), policy_for(server, resolver))
    assert out.status == "ok" and sink.data == b"hello"
    assert connects == [(LOOP, server.port)]


@pytest.mark.parametrize("answer", [["10.0.0.5"], ["10.0.0.5", "192.168.1.1"], ["::1"], ["fe80::1"], ["169.254.169.254"]])
def test_no_public_address_at_all_is_url_blocked_without_any_connection(nf, server, connects, answer):
    out, _ = fetch(nf, server.url("/ok"), policy_for(server, Resolver({"pinned.example": [answer]})))
    failed(out, "URL_BLOCKED", "ip_not_public", retryable=False, status="blocked")
    assert connects == [] and server.connections == 0


def test_the_default_ip_rule_refuses_loopback_even_when_the_name_resolves_there(nf, server, connects):
    policy = Policy(ports=(server.port,), resolver=Resolver({"pinned.example": [[LOOP]]}))  # default ip_ok
    out, _ = fetch(nf, server.url("/ok"), policy)
    failed(out, "URL_BLOCKED", status="blocked")
    assert connects == [] and server.connections == 0


def test_a_slow_resolver_is_a_timeout_and_never_a_hang(nf, server, connects):
    resolver = Resolver({"pinned.example": [lambda: (time.sleep(3), [LOOP])[1]]})
    started = time.monotonic()
    out, _ = fetch(nf, server.url("/ok"), policy_for(server, resolver), connect_timeout=0.4)
    assert time.monotonic() - started < 2
    failed(out, "TIMEOUT", retryable=True)
    assert connects == [] and server.connections == 0


def test_a_failing_resolver_is_dns_failed(nf, server):
    for answer in (socket.gaierror(11001, "getaddrinfo failed"), []):
        out, _ = fetch(nf, server.url("/ok"), policy_for(server, Resolver({"pinned.example": [answer]})))
        failed(out, "NETWORK_ERROR", "dns_failed", retryable=True)


def test_the_connect_step_checks_the_port_again(nf, server, connects):
    backend = nf.PinnedBackend(Policy(ports=(server.port,), ip_ok=loop_only, resolver=Resolver(hosts("pinned.example"))))
    with pytest.raises(nf.NetError) as caught:
        backend.connect_tcp("pinned.example", server.port + 1, timeout=1)
    assert caught.value.code == "URL_BLOCKED" and caught.value.reason == "port_not_allowed"
    assert connects == []


def test_the_connect_step_checks_a_literal_address_too(nf, connects):
    backend = nf.PinnedBackend(Policy(ports=(80,), ip_ok=loop_only))  # default resolver: a literal needs no DNS
    with pytest.raises(nf.NetError) as caught:
        backend.connect_tcp("10.1.1.1", 80, timeout=1)
    assert caught.value.code == "URL_BLOCKED" and caught.value.reason == "ip_not_public"
    assert connects == []


def test_the_default_policy_refuses_a_local_url_before_any_network(nf, server, connects, names_resolved):
    for host in ("127.0.0.1", "localhost", "[::1]", "2130706433"):
        out, _ = fetch(nf, "http://%s:%d/ok" % (host, server.port), Policy())
        failed(out, "URL_BLOCKED", status="blocked")
    assert connects == [] and names_resolved == [] and server.connections == 0


def test_the_real_resolver_answer_for_localhost_is_refused_by_the_default_rule():
    infos = socket.getaddrinfo("localhost", 80, proto=socket.IPPROTO_TCP)  # a real call, answered from the local hosts file
    assert infos and not any(Policy().ip_ok(info[4][0]) for info in infos)


# ----------------------------------------------------------------------------- fresh client, no cookies (FR2.6)
def test_two_items_on_the_same_host_share_neither_cookies_nor_a_connection(nf, server):
    for _ in range(2):
        out, _ = fetch(nf, server.url("/ok"), policy_for(server))
        assert out.status == "ok"
    assert "cookie" not in server.requests[1]["headers"]
    assert server.connections == 2 and server.requests[0]["client_port"] != server.requests[1]["client_port"]


def test_without_an_injected_client_every_request_gets_its_own_closed_client(nf, server, monkeypatch):
    made = []
    real = nf.build_client

    def recording(*args, **kwargs):
        client = real(*args, **kwargs)
        made.append(client)
        return client

    monkeypatch.setattr(nf, "build_client", recording)
    out, _ = fetch(nf, server.url("/redir?to=/ok&cookie=1"), policy_for(server))
    assert out.status == "ok" and out.redirects == 1
    assert len(made) == 2 and made[0] is not made[1] and all(client.is_closed for client in made)
    assert "cookie" not in server.requests[1]["headers"]


def test_a_cookie_set_on_a_redirect_is_not_sent_to_the_next_step(nf, server):
    out, _ = fetch(nf, server.url("/redir?to=/echo&cookie=1"), policy_for(server))
    assert out.status == "ok" and [r["path"] for r in server.requests] == ["/redir?to=/echo&cookie=1", "/echo"]
    assert "cookie" not in server.requests[1]["headers"]


# ----------------------------------------------------------------------------- redirects with a real server (FR2.5)
def test_a_relative_redirect_is_followed(nf, server):
    out, sink = fetch(nf, server.url("/redir?to=/ok"), policy_for(server))
    assert out.status == "ok" and sink.data == b"hello" and out.redirects == 1 and out.http_status == 200
    assert out.final_url == server.url("/ok")


def test_a_redirect_to_another_host_is_resolved_and_checked_again(nf, server):
    resolver = Resolver(hosts("pinned.example", "other.example"))
    out, _ = fetch(nf, server.url("/redir?to=" + server.url("/ok", host="other.example")), policy_for(server, resolver))
    assert out.status == "ok" and out.redirects == 1
    assert resolver.calls == ["pinned.example", "other.example"]
    assert server.requests[1]["headers"]["host"] == "other.example:%d" % server.port


def test_a_redirect_to_a_host_with_only_non_public_addresses_is_redirect_blocked(nf, server, connects):
    resolver = Resolver({"pinned.example": [[LOOP]], "other.example": [["10.0.0.9"]]})
    out, _ = fetch(nf, server.url("/redir?to=" + server.url("/ok", host="other.example")), policy_for(server, resolver))
    failed(out, "REDIRECT_BLOCKED", retryable=False, status="blocked")
    assert out.redirects == 0 and connects == [(LOOP, server.port)] and len(server.requests) == 1


def test_a_redirect_whose_dns_changes_between_steps_is_still_pinned(nf, server, connects):
    resolver = Resolver({"pinned.example": [[LOOP]], "other.example": [[LOOP], ["10.0.0.9"]]})
    out, _ = fetch(nf, server.url("/redir?to=" + server.url("/ok", host="other.example")), policy_for(server, resolver))
    assert out.status == "ok" and connects == [(LOOP, server.port)] * 2
    assert resolver.calls.count("other.example") == 1


# ----------------------------------------------------------------------------- redirects with a mock transport (no sockets)
def mock_client(nf, handler, resolver=None, **limit_overrides):
    resolver = resolver or Resolver()  # asks nothing: with a mock transport nothing may be resolved
    return nf.build_client(Policy(resolver=resolver), lim(**limit_overrides), transport=httpx.MockTransport(handler)), resolver


def script(answers):
    """handler answering by URL from `answers` (url -> httpx.Response); records requests in `calls`."""
    calls = []

    def handler(request):
        calls.append(request)
        return answers.get(str(request.url), reply(599, b"unexpected " + str(request.url).encode()))

    return handler, calls


def reply(status, body=b"", **headers):
    """A canned response whose body is a real stream: a MockTransport response built with `content=` is already
    consumed, and the tool reads bodies with iter_raw (SPEC FR3.1)."""
    headers.setdefault("Content-Length", str(len(body)))
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


def redirect(to, code=302, **headers):
    return reply(code, Location=to.encode("utf-8"), **headers)  # bytes: a header value may hold non-ASCII text


def ok_body(content=b"data"):
    return reply(200, content, **{"Content-Type": "application/octet-stream"})


def mock_fetch(nf, answers, start="https://files.example.com/a", extra=None, **limit_overrides):
    handler, calls = script(answers)
    client, resolver = mock_client(nf, handler, **limit_overrides)
    out, sink = fetch(nf, start, Policy(resolver=resolver), client=client, extra=extra, **limit_overrides)
    return out, sink, calls, resolver


BAD_LOCATIONS = [
    "http://127.0.0.1/x", "http://2130706433/", "http://localhost/x", "http://[::1]/x", "http://169.254.169.254/latest",
    "http://10.0.0.1/", "https://files.example.com:8443/x", "file:///c:/windows/win.ini", "ftp://files.example.com/x",
    "https://u:p@files.example.com/x", "http://files.example.com/x", "javascript:alert(1)", "https://files.example.com/a b",
    "https://files.example.com/a\\b", "http://intranet/",
    "https://files.example.com/a\tb", "https://files.example.com/a\r\nX-Evil: 1", "/\\evil.example",
    "https://127。" "0。" "0。" "1/", "https://@files.example.com/", "https://[fe80::1%251]/",
]


@pytest.mark.parametrize("location", BAD_LOCATIONS)
def test_an_unsafe_redirect_target_is_refused_without_a_request(nf, no_socket, location):
    out, _, calls, resolver = mock_fetch(nf, {"https://files.example.com/a": redirect(location)})
    failed(out, "REDIRECT_BLOCKED", retryable=False, status="blocked")
    assert len(calls) == 1 and out.redirects == 0 and resolver.calls == []


def test_an_upgrade_from_http_to_https_is_allowed(nf, no_socket):
    out, sink, calls, _ = mock_fetch(nf, {"http://files.example.com/a": redirect("https://files.example.com/a", 301),
                                          "https://files.example.com/a": ok_body()}, start="http://files.example.com/a")
    assert out.status == "ok" and out.redirects == 1 and sink.data == b"data" and out.final_url == "https://files.example.com/a"


def test_relative_and_scheme_relative_locations_are_resolved(nf, no_socket):
    out, _, calls, _ = mock_fetch(nf, {"https://files.example.com/dir/a": redirect("b"),
                                       "https://files.example.com/dir/b": redirect("//other.example.com/x"),
                                       "https://other.example.com/x": ok_body()}, start="https://files.example.com/dir/a")
    assert out.status == "ok" and out.redirects == 2 and [str(c.url) for c in calls][-1] == "https://other.example.com/x"


def test_a_redirect_loop_is_blocked_before_the_url_is_requested_twice(nf, no_socket):
    out, _, calls, _ = mock_fetch(nf, {"https://files.example.com/a": redirect("/b"), "https://files.example.com/b": redirect("/a")})
    failed(out, "REDIRECT_BLOCKED", status="blocked")
    assert [str(c.url) for c in calls] == ["https://files.example.com/a", "https://files.example.com/b"] and out.redirects == 1


def chain(length, final=ok_body):
    """/0 -> /1 -> ... -> /length where /length answers with `final()`."""
    answers = {"https://files.example.com/%d" % i: redirect("/%d" % (i + 1)) for i in range(length)}
    answers["https://files.example.com/%d" % length] = final()
    return answers


def test_five_redirects_are_allowed_and_a_sixth_is_not(nf, no_socket):
    out, _, calls, _ = mock_fetch(nf, chain(5), start="https://files.example.com/0")
    assert out.status == "ok" and out.redirects == 5 and len(calls) == 6
    out, _, calls, _ = mock_fetch(nf, chain(6), start="https://files.example.com/0")
    failed(out, "REDIRECT_BLOCKED", status="blocked")
    assert out.redirects == 5 and len(calls) == 6


def test_max_redirects_comes_from_the_limits_up_to_the_hard_cap_of_ten(nf, no_socket):
    out, _, calls, _ = mock_fetch(nf, chain(5), start="https://files.example.com/0", max_redirects=2)
    failed(out, "REDIRECT_BLOCKED", status="blocked")
    assert out.redirects == 2 and len(calls) == 3
    out, _, calls, _ = mock_fetch(nf, chain(11), start="https://files.example.com/0", max_redirects=10)
    failed(out, "REDIRECT_BLOCKED", status="blocked")
    assert out.redirects == 10 and len(calls) == 11
    out, _, calls, _ = mock_fetch(nf, chain(10), start="https://files.example.com/0", max_redirects=10)
    assert out.status == "ok" and out.redirects == 10


def test_every_redirect_status_in_the_list_is_followed(nf, no_socket):
    for code in (301, 302, 303, 307, 308):
        out, sink, _, _ = mock_fetch(nf, {"https://files.example.com/a": redirect("/b", code), "https://files.example.com/b": ok_body()})
        assert out.status == "ok" and out.redirects == 1 and sink.data == b"data", code


@pytest.mark.parametrize("code", [300, 304, 305, 306])
def test_other_3xx_answers_are_a_network_error_not_a_redirect(nf, no_socket, code):
    out, _, calls, _ = mock_fetch(nf, {"https://files.example.com/a": redirect("/b", code)})
    failed(out, "NETWORK_ERROR", http_status=code)
    assert len(calls) == 1


@pytest.mark.parametrize("code", [100, 204])
def test_a_one_hundred_or_empty_204_answer_is_a_network_error(nf, no_socket, code):
    out, sink, _, _ = mock_fetch(nf, {"https://files.example.com/a": reply(code)})
    failed(out, "NETWORK_ERROR", http_status=code)
    assert out.status == "failed" and sink.data == b""


def test_no_cookie_or_authorization_is_ever_sent_even_after_set_cookie(nf, no_socket):
    out, _, calls, _ = mock_fetch(nf, {"https://files.example.com/a": redirect("/b", **{"Set-Cookie": "sid=1; Path=/"}),
                                       "https://files.example.com/b": ok_body()})
    assert out.status == "ok"
    for request in calls:
        assert "cookie" not in request.headers and "authorization" not in request.headers


def test_the_final_url_is_reported_redacted(nf, no_socket):
    target = "https://files.example.com/f.pdf?token=SECRET&session"
    out, _, _, _ = mock_fetch(nf, {"https://files.example.com/a": redirect(target), target: ok_body()})
    assert out.status == "ok" and out.final_url_redacted == urlpolicy.redact(out.final_url)
    assert "SECRET" not in out.final_url_redacted and "SECRET" not in repr(out.headers_clean)


# ----------------------------------------------------------------------------- HTTP status mapping (FR3.3)
@pytest.mark.parametrize("code, error, retryable", [
    (401, "AUTH_REQUIRED", False), (407, "AUTH_REQUIRED", False), (429, "RATE_LIMITED", True),
    (404, "NETWORK_ERROR", False), (410, "NETWORK_ERROR", False), (418, "NETWORK_ERROR", False),
    (500, "NETWORK_ERROR", True), (502, "NETWORK_ERROR", True), (503, "NETWORK_ERROR", True),
])
def test_http_status_codes_map_to_the_fixed_errors(nf, server, code, error, retryable):
    out, sink = fetch(nf, server.url("/status/%d" % code), policy_for(server))
    failed(out, error, retryable=retryable, http_status=code)
    assert sink.data == b""
    if error == "NETWORK_ERROR":
        assert out.reason == "http_status"


def test_a_429_keeps_a_cleaned_retry_after_and_does_not_wait(nf, server):
    started = time.monotonic()
    out, _ = fetch(nf, server.url("/status/429"), policy_for(server))
    assert out.headers_clean["retry-after"] == "30" and time.monotonic() - started < 5


# ----------------------------------------------------------------------------- bytes and encodings (FR3.1, FR3.9)
@pytest.mark.parametrize("coding", ["gzip", "deflate", "br", "x-gzip", "zstd", "compress", "gzip+gzip", "gzip+identity", "GZIP"])
def test_any_content_encoding_other_than_identity_is_refused(nf, server, coding):
    out, sink = fetch(nf, server.url("/enc/%s?size=100" % coding), policy_for(server))
    failed(out, "CONTENT_MISMATCH", "content_encoding_unsupported", retryable=False)
    assert sink.data == b"" and out.status == "failed"


@pytest.mark.parametrize("coding", ["identity", "Identity"])
def test_identity_content_encoding_is_accepted(nf, server, coding):
    out, sink = fetch(nf, server.url("/enc/%s?size=100" % coding), policy_for(server))
    assert out.status == "ok" and sink.data == b"x" * 100


@pytest.mark.parametrize("coding", ["gzip", "gzip+gzip"])
def test_an_encoded_bomb_is_refused_at_the_header_after_a_tiny_read(nf, server, coding):
    out, sink = fetch(nf, server.url("/enc/%s?size=%d" % (coding, 64 * MiB)), policy_for(server))
    failed(out, "CONTENT_MISMATCH", "content_encoding_unsupported")
    time.sleep(0.3)
    assert sink.data == b"" and server.sent < 8 * MiB, server.sent


def test_a_content_length_over_the_limit_is_refused_before_the_body_is_read(nf, server):
    out, sink = fetch(nf, server.url("/file?size=%d" % (64 * MiB)), policy_for(server), max_bytes_per_file=1000)
    failed(out, "SIZE_LIMIT", retryable=False)
    time.sleep(0.3)
    assert sink.data == b"" and server.sent < 8 * MiB, server.sent


def test_a_stream_without_content_length_is_cut_at_the_limit(nf, server):
    out, sink = fetch(nf, server.url("/nocl?size=5000"), policy_for(server), max_bytes_per_file=1000)
    failed(out, "SIZE_LIMIT", retryable=False)
    assert len(sink.data) <= 1000


def test_a_body_exactly_at_the_limit_is_accepted(nf, server):
    out, sink = fetch(nf, server.url("/nocl?size=1000"), policy_for(server), max_bytes_per_file=1000)
    assert out.status == "ok" and len(sink.data) == 1000 and out.bytes_received == 1000


def test_the_remaining_total_budget_is_a_limit_too(nf, server):
    out, sink = fetch(nf, server.url("/nocl?size=5000"), policy_for(server), extra={"total_remaining": 500})
    failed(out, "SIZE_LIMIT")
    assert len(sink.data) <= 500
    out, sink = fetch(nf, server.url("/file?size=2000"), policy_for(server), extra={"total_remaining": 500})
    failed(out, "SIZE_LIMIT")
    assert sink.data == b""  # a known Content-Length over the budget is refused up front


def test_html_is_limited_by_max_html_bytes(nf, server):
    out, sink = fetch(nf, server.url("/html?size=5000"), policy_for(server), expect="html", max_html_bytes=1000)
    failed(out, "SIZE_LIMIT")
    assert len(sink.data) <= 1000
    out, sink = fetch(nf, server.url("/html?size=900"), policy_for(server), expect="html", max_html_bytes=1000)
    assert out.status == "ok" and len(sink.data) == 900


def test_a_body_shorter_than_content_length_is_a_truncated_body(nf, server):
    out, _ = fetch(nf, server.url("/short?cl=1000&send=10"), policy_for(server))
    failed(out, "NETWORK_ERROR", "truncated_body", retryable=True)


def test_a_body_longer_than_content_length_is_cut_at_the_declared_length(nf, server):
    """RF100: exactly N bytes are accepted; no warning (h11 drops the excess, nothing public reveals it)."""
    out, sink = fetch(nf, server.url("/bigcl?cl=10&size=50"), policy_for(server))
    assert out.status == "ok" and sink.data == b"x" * 10 and out.bytes_received == 10 and out.warnings == []


# ----------------------------------------------------------------------------- HTML instead of a binary (FR3.4)
@pytest.mark.parametrize("query", ["kind=html", "kind=html&ctype=application/octet-stream"])
def test_html_in_place_of_a_pdf_stops_after_the_first_chunk(nf, server, query):
    out, sink = fetch(nf, server.url("/report.pdf?%s&size=%d" % (query, 32 * MiB)), policy_for(server))
    failed(out, "CONTENT_MISMATCH", retryable=False)
    time.sleep(0.3)
    assert sink.data == b"" and server.sent < 8 * MiB, server.sent


def test_a_real_pdf_and_an_expected_html_page_are_accepted(nf, server):
    out, sink = fetch(nf, server.url("/report.pdf?size=5000"), policy_for(server))
    assert out.status == "ok" and sink.data.startswith(b"%PDF-")
    out, sink = fetch(nf, server.url("/report.pdf?kind=html&size=3000"), policy_for(server), expect="html")
    assert out.status == "ok" and sink.data.startswith(b"<!DOCTYPE html>")
    out, sink = fetch(nf, server.url("/html?size=3000"), policy_for(server), expect="auto")
    assert out.status == "ok" and sink.data.startswith(b"<!DOCTYPE html>")  # no binary extension: HTML is what it is


def test_expect_binary_also_refuses_html_for_a_known_binary_extension(nf, server):
    out, sink = fetch(nf, server.url("/picture.png?kind=html&size=3000"), policy_for(server), expect="binary")
    failed(out, "CONTENT_MISMATCH")
    assert sink.data == b""


# ----------------------------------------------------------------------------- time limits (FR3.2)
@pytest.fixture
def closed_port():
    probe = socket.socket()
    probe.bind((LOOP, 0))
    port = probe.getsockname()[1]
    probe.close()  # nobody listens now
    return port


@pytest.mark.skipif(sys.platform != "win32", reason="SPEC FR2.7: only Windows turns a closed loopback port into a timeout below ~2 s")
def test_connect_timeout_on_a_closed_loopback_port_is_a_timeout_on_windows(nf, server, closed_port):
    started = time.monotonic()
    out, _ = fetch(nf, "http://pinned.example:%d/x" % closed_port, policy_for(server, extra_ports=(closed_port,)),
                   connect_timeout=0.5)
    assert time.monotonic() - started < 1.9
    failed(out, "TIMEOUT", retryable=True)


@pytest.mark.skipif(sys.platform != "win32", reason="the ~2.1 s refusal delay is the Windows behaviour measured in TK11")
def test_a_closed_port_with_a_long_connect_timeout_is_connection_refused(nf, server, closed_port):
    """Windows reports the refusal after ~2.1 s (SPEC FR2.7): this test takes that long."""
    out, _ = fetch(nf, "http://pinned.example:%d/x" % closed_port, policy_for(server, extra_ports=(closed_port,)),
                   connect_timeout=5)
    failed(out, "NETWORK_ERROR", "connection_refused", retryable=True)


def test_idle_timeout_while_waiting_for_the_header(nf, server):
    started = time.monotonic()
    out, _ = fetch(nf, server.url("/slow?delay=3"), policy_for(server), idle_timeout=0.4)
    assert time.monotonic() - started < 2.5
    failed(out, "TIMEOUT", retryable=True)


def test_idle_timeout_while_the_body_stalls(nf, server):
    started = time.monotonic()
    out, _ = fetch(nf, server.url("/stall?send=5&delay=5"), policy_for(server), idle_timeout=0.4)
    assert time.monotonic() - started < 2.5
    failed(out, "TIMEOUT", retryable=True)


def test_item_timeout_stops_a_slow_trickle_that_never_goes_idle(nf, server):
    started = time.monotonic()
    out, _ = fetch(nf, server.url("/drip?n=200&every=0.05"), policy_for(server), idle_timeout=5, item_timeout=0.5)
    assert time.monotonic() - started < 3
    failed(out, "TIMEOUT", retryable=True)


def test_item_timeout_also_cuts_a_stall_that_is_shorter_than_the_idle_timeout(nf, server):
    started = time.monotonic()
    out, _ = fetch(nf, server.url("/stall?send=5&delay=5"), policy_for(server), idle_timeout=30, item_timeout=0.5)
    assert time.monotonic() - started < 3
    failed(out, "TIMEOUT", retryable=True)


def test_item_timeout_with_an_injected_clock(nf, server, fake_clock):
    sink = Sink(fake_clock, advance=3)
    out, _ = fetch(nf, server.url("/file?size=%d" % (4 * MiB)), policy_for(server), sink=sink, item_timeout=5,
                   extra={"clock": fake_clock})
    failed(out, "TIMEOUT", retryable=True)
    assert 2 <= len(sink.chunks) <= 3


def test_a_job_deadline_already_passed_means_no_connection_at_all(nf, server, fake_clock, connects):
    fake_clock.now = 100
    out, sink = fetch(nf, server.url("/ok"), policy_for(server), extra={"clock": fake_clock, "job_deadline": 50})
    failed(out, "TIMEOUT", retryable=True)
    assert connects == [] and server.connections == 0 and sink.data == b""


def test_a_job_deadline_reached_during_the_body_stops_the_download(nf, server, fake_clock):
    fake_clock.now = 100
    sink = Sink(fake_clock, advance=60)
    out, _ = fetch(nf, server.url("/file?size=%d" % (4 * MiB)), policy_for(server), sink=sink,
                   extra={"clock": fake_clock, "job_deadline": 150})
    failed(out, "TIMEOUT", retryable=True)
    assert 1 <= len(sink.chunks) <= 2


# ----------------------------------------------------------------------------- real network errors keep our classes (FR2.7)
def test_a_connection_reset_by_the_server(nf, server):
    out, _ = fetch(nf, server.url("/reset"), policy_for(server))
    failed(out, "NETWORK_ERROR", "connection_reset", retryable=True)


class Raiser(httpcore.NetworkBackend):
    def __init__(self, error):
        self.error = error

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        raise self.error

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise self.error

    def sleep(self, seconds):
        time.sleep(seconds)


def chained(outer, inner):
    try:
        try:
            raise inner
        except BaseException as cause:  # noqa: BLE001
            raise outer from cause
    except BaseException as error:  # noqa: BLE001
        return error


HTTPCORE_ERRORS = [
    ("connect_timeout", lambda: httpcore.ConnectTimeout("t"), "TIMEOUT", None),
    ("read_timeout", lambda: httpcore.ReadTimeout("t"), "TIMEOUT", None),
    ("write_timeout", lambda: httpcore.WriteTimeout("t"), "TIMEOUT", None),
    ("pool_timeout", lambda: httpcore.PoolTimeout("t"), "TIMEOUT", None),
    ("refused", lambda: chained(httpcore.ConnectError("x"), ConnectionRefusedError()), "NETWORK_ERROR", "connection_refused"),
    ("reset", lambda: chained(httpcore.ReadError("x"), ConnectionResetError()), "NETWORK_ERROR", "connection_reset"),
    ("aborted", lambda: chained(httpcore.WriteError("x"), ConnectionAbortedError()), "NETWORK_ERROR", "connection_reset"),
    ("dns", lambda: chained(httpcore.ConnectError("x"), socket.gaierror(11001, "x")), "NETWORK_ERROR", "dns_failed"),
    ("unreachable", lambda: chained(httpcore.ConnectError("x"), OSError(10051, "unreachable")), "NETWORK_ERROR", "network_unreachable"),
    ("tls", lambda: chained(httpcore.ConnectError("x"), ssl.SSLCertVerificationError("bad")), "NETWORK_ERROR", "tls_verify_failed"),
    ("tls_other", lambda: chained(httpcore.ConnectError("x"), ssl.SSLError("handshake")), "NETWORK_ERROR", "other"),
    ("remote_protocol_before_the_body", lambda: httpcore.RemoteProtocolError("peer closed"), "NETWORK_ERROR", "other"),  # RF124
    ("proxy", lambda: httpcore.ProxyError("x"), "NETWORK_ERROR", "other"),
    ("unsupported", lambda: httpcore.UnsupportedProtocol("x"), "NETWORK_ERROR", "other"),
    ("local_protocol", lambda: httpcore.LocalProtocolError("x"), "NETWORK_ERROR", "other"),
    ("plain_connect_error", lambda: httpcore.ConnectError("x"), "NETWORK_ERROR", "other"),
]


@pytest.mark.parametrize("name, make, code, reason", HTTPCORE_ERRORS, ids=[row[0] for row in HTTPCORE_ERRORS])
def test_no_httpcore_exception_ever_reaches_the_caller(nf, server, name, make, code, reason):
    policy = policy_for(server)
    transport = nf.PinnedTransport(policy, lim(), network_backend=Raiser(make()))
    with pytest.raises(nf.NetError) as caught:  # the transport itself already speaks our classes
        transport.handle_request(httpx.Request("GET", server.url("/ok")))
    assert not type(caught.value).__module__.startswith("httpcore")
    assert caught.value.code == code and (reason is None or caught.value.reason == reason)
    client = nf.build_client(policy, lim(), transport=nf.PinnedTransport(policy, lim(), network_backend=Raiser(make())))
    out, _ = fetch(nf, server.url("/ok"), policy, client=client)
    assert out.status == "failed" and out.code == code and out.code != "TOOL_FAILED"
    assert out.code == "TIMEOUT" or out.reason in errors.REASONS
    if reason is not None:
        assert out.reason == reason
    client.close()


# ----------------------------------------------------------------------------- TLS (FR2.6)
def test_the_tls_context_is_built_explicitly_with_verification_on_and_no_environment(nf, server, monkeypatch):
    built = []
    real = httpx.create_ssl_context

    def recording(*args, **kwargs):
        ctx = real(*args, **kwargs)
        built.append((args, kwargs, ctx))
        return ctx

    monkeypatch.setattr(httpx, "create_ssl_context", recording)
    nf.build_client(policy_for(server), lim()).close()
    assert built, "build_client must call httpx.create_ssl_context itself (a custom transport does not inherit it)"
    for args, kwargs, ctx in built:
        assert args == () and kwargs == {"verify": True, "trust_env": False}
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


def test_a_self_signed_certificate_is_refused_and_no_request_is_sent(nf, tls_server):
    server, _crt = tls_server
    out, _ = fetch(nf, server.url("/ok"), policy_for(server))
    failed(out, "NETWORK_ERROR", "tls_verify_failed")
    assert server.requests == []


def test_ssl_environment_variables_cannot_make_the_certificate_trusted(nf, tls_server, monkeypatch, tmp_path):
    server, crt = tls_server
    monkeypatch.setenv("SSL_CERT_FILE", crt)
    monkeypatch.setenv("SSL_CERT_DIR", str(Path(crt).parent))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", crt)
    monkeypatch.setenv("CURL_CA_BUNDLE", crt)
    out, _ = fetch(nf, server.url("/ok"), policy_for(server))
    failed(out, "NETWORK_ERROR", "tls_verify_failed")
    assert server.requests == []


def trusting(monkeypatch, crt):
    """Make the tool's own context trust the test certificate, leaving verification and name check on."""
    real = httpx.create_ssl_context

    def make(*args, **kwargs):
        ctx = real(*args, **kwargs)
        ctx.load_verify_locations(cafile=crt)
        return ctx

    monkeypatch.setattr(httpx, "create_ssl_context", make)


def test_a_trusted_certificate_works_and_sni_and_host_keep_the_original_name(nf, tls_server, monkeypatch, connects):
    server, crt = tls_server
    trusting(monkeypatch, crt)
    out, sink = fetch(nf, server.url("/ok"), policy_for(server))
    assert out.status == "ok" and sink.data == b"hello"
    assert server.sni == ["pinned.example"] and server.requests[0]["headers"]["host"] == "pinned.example:%d" % server.port
    assert connects == [(LOOP, server.port)]


def test_a_certificate_for_another_name_is_refused_even_when_trusted(nf, tls_server, monkeypatch):
    server, crt = tls_server
    trusting(monkeypatch, crt)
    out, _ = fetch(nf, server.url("/ok", host="other.example"), policy_for(server))
    failed(out, "NETWORK_ERROR", "tls_verify_failed")
    assert server.requests == []
