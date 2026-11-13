"""Review of TK12 (RF101-RF106) and the pieces of netfetch the first test file does not reach."""

import socket
import time

import httpcore
import httpx
import pytest

from agent_crawl_studio.urlpolicy import Policy

from test_netfetch import (HOST, LOOP, Resolver, Sink, connects, failed, fetch, hosts, lim, mock_client, mock_fetch,  # noqa: F401
                           nf, ok_body, policy_for, redirect, reply, server)

A = "https://files.example.com/a"


# ----------------------------------------------------------------------------- RF101: chunked bodies and huge headers
def test_chunked_body_is_limited_like_any_other(nf, server):
    out, sink = fetch(nf, server.url("/chunked?size=5000"), policy_for(server), max_bytes_per_file=1000)
    failed(out, "SIZE_LIMIT", retryable=False)
    assert len(sink.data) <= 1000
    out, sink = fetch(nf, server.url("/chunked?size=1000"), policy_for(server), max_bytes_per_file=1000)
    assert out.status == "ok" and sink.data == b"x" * 1000


def test_a_huge_chunk_extension_is_a_network_error_not_memory(nf, server):
    out, sink = fetch(nf, server.url("/chunked?size=10&ext=200000"), policy_for(server))
    failed(out, "NETWORK_ERROR")
    assert out.reason in ("truncated_body", "other")


def test_a_huge_response_header_is_a_network_error(nf, server):
    out, _ = fetch(nf, server.url("/bighdr?size=200000"), policy_for(server))
    failed(out, "NETWORK_ERROR")


# ----------------------------------------------------------------------------- RF102: 30x without a Location
@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_a_redirect_status_without_location_is_a_network_error(nf, no_socket, code):
    out, _, calls, _ = mock_fetch(nf, {A: reply(code)})
    failed(out, "NETWORK_ERROR", "http_status", retryable=False, http_status=code)
    assert len(calls) == 1


# ----------------------------------------------------------------------------- RF103: only a plain 200 is a download
@pytest.mark.parametrize("code", [201, 202, 203, 205, 206])
def test_a_2xx_other_than_200_is_not_a_success(nf, no_socket, code):
    out, sink, _, _ = mock_fetch(nf, {A: reply(code, b"partial")})
    failed(out, "NETWORK_ERROR", "http_status", retryable=False, http_status=code)
    assert sink.data == b""


def test_416_is_a_network_error(nf, server):
    out, _ = fetch(nf, server.url("/status/416"), policy_for(server))
    failed(out, "NETWORK_ERROR", "http_status", retryable=False, http_status=416)


# ----------------------------------------------------------------------------- RF104: one deadline for the whole connect phase
class SlowInner:
    def __init__(self):
        self.calls = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.calls.append((host, timeout))
        time.sleep(timeout + 0.03)  # a little more than asked: the Windows timer may wake early (RF161)
        raise httpcore.ConnectTimeout("simulated")


def three_addresses():
    resolver = Resolver({"pinned.example": [["203.0.113.1", "203.0.113.2", "203.0.113.3"]]})
    return Policy(ports=(80,), ip_ok=lambda address: True, resolver=resolver)


def test_three_unreachable_addresses_share_one_connect_timeout(nf):
    """Simulated (no packet leaves the machine): the inner connect sleeps for the time it is given."""
    backend = nf.PinnedBackend(three_addresses())
    backend._inner = SlowInner()
    with pytest.raises(httpcore.ConnectTimeout):
        backend.connect_tcp("pinned.example", 80, timeout=0.4)
    # deterministic (RF161): the first attempt spent the whole budget, so no second attempt was made, and what the
    # inner connect was given never exceeded the budget; no wall-clock bound that a loaded machine could break
    assert len(backend._inner.calls) == 1 and backend._inner.calls[0][1] <= 0.4


def test_the_shared_connect_timeout_reaches_the_outcome_as_one_timeout(nf):
    policy = three_addresses()
    backend = nf.PinnedBackend(policy)
    backend._inner = SlowInner()
    client = nf.build_client(policy, lim(connect_timeout=0.4), transport=nf.PinnedTransport(policy, lim(), network_backend=backend))
    out = nf.fetch_item("http://pinned.example/x", "auto", lim(connect_timeout=0.4), lambda chunk: None, policy, client=client)
    client.close()
    failed(out, "TIMEOUT", retryable=True)
    assert len(backend._inner.calls) == 1


def test_an_address_that_refuses_lets_the_next_one_try(nf, server, connects):
    resolver = Resolver({"pinned.example": [["127.0.0.2", LOOP]]})  # 127.0.0.2 refuses on this port: the second answer is used
    policy = policy_for(server, resolver, ok=lambda a: a in ("127.0.0.2", LOOP))
    out, sink = fetch(nf, server.url("/ok"), policy, connect_timeout=5)
    assert out.status == "ok" and sink.data == b"hello"
    assert connects[-1] == (LOOP, server.port)


# ----------------------------------------------------------------------------- RF106
def test_http2_is_off(nf, server):
    transport = nf.PinnedTransport(policy_for(server), lim())
    assert transport._pool._http2 is False and transport._pool._http1 is True
    transport.close()


def test_an_idn_host_goes_out_as_ascii_in_the_host_header(nf, server):
    resolver = Resolver({"xn--bcher-kva.example": [[LOOP]]})
    out, _ = fetch(nf, "http://bücher.example:%d/ok" % server.port, policy_for(server, resolver))
    assert out.status == "ok"
    assert server.requests[0]["headers"]["host"] == "xn--bcher-kva.example:%d" % server.port


def test_an_ipv4_mapped_loopback_answer_is_refused_by_the_default_rule_at_connect_time(nf, connects):
    resolver = Resolver({"pinned.example": [["::ffff:0:127.0.0.1"]]})
    backend = nf.PinnedBackend(Policy(ports=(80,), resolver=resolver))  # default ip_ok
    with pytest.raises(nf.NetError) as caught:
        backend.connect_tcp("pinned.example", 80, timeout=1)
    assert caught.value.code == "URL_BLOCKED" and caught.value.reason == "ip_not_public" and connects == []


# ----------------------------------------------------------------------------- the rest of the module
def test_a_malformed_resolver_answer_is_dns_failed(nf):
    backend = nf.PinnedBackend(Policy(ports=(80,), resolver=lambda *a, **k: [("x",)]))
    with pytest.raises(nf.NetError) as caught:
        backend.connect_tcp("pinned.example", 80, timeout=1)
    assert caught.value.code == "NETWORK_ERROR" and caught.value.reason == "dns_failed"


def test_a_unix_socket_is_never_connected(nf):
    with pytest.raises(nf.NetError) as caught:
        nf.PinnedBackend(Policy()).connect_unix_socket("/tmp/x")
    assert caught.value.code == "URL_BLOCKED"


def test_the_backend_sleep_is_a_real_sleep(nf):
    started = time.monotonic()
    nf.PinnedBackend(Policy()).sleep(0.05)
    assert time.monotonic() - started >= 0.04


def test_a_mock_body_longer_than_declared_is_cut(nf, no_socket):
    stream = httpx.ByteStream(b"x" * 50)
    out, sink, _, _ = mock_fetch(nf, {A: httpx.Response(200, headers={"Content-Length": "10"}, stream=stream)})
    assert out.status == "ok" and sink.data == b"x" * 10 and out.warnings == []


def test_a_mock_body_shorter_than_declared_is_a_truncated_body(nf, no_socket):
    stream = httpx.ByteStream(b"x" * 5)
    out, _, _, _ = mock_fetch(nf, {A: httpx.Response(200, headers={"Content-Length": "10"}, stream=stream)})
    failed(out, "NETWORK_ERROR", "truncated_body", retryable=True)


def test_a_mock_html_content_type_for_a_pdf_is_refused_before_any_byte(nf, no_socket):
    out, sink, _, _ = mock_fetch(nf, {"https://files.example.com/a.pdf": reply(200, b"<html>x", **{"Content-Type": "text/html; charset=utf-8"})},
                                 start="https://files.example.com/a.pdf")
    failed(out, "CONTENT_MISMATCH")
    assert sink.data == b""


def test_sniff_helpers():
    from agent_crawl_studio import sniff
    assert sniff.has_binary_extension("/x/a.PDF") and not sniff.has_binary_extension("/x/pdf") and not sniff.has_binary_extension("/x/a.txt")
    assert sniff.looks_like_html(b"\xef\xbb\xbf  \n<!DOCTYPE HTML>") and sniff.looks_like_html(b"<html>")
    assert not sniff.looks_like_html(b"%PDF-1.7") and not sniff.looks_like_html(b"")
    assert sniff.is_html_content_type("Text/HTML; charset=x") and not sniff.is_html_content_type(None)


@pytest.mark.parametrize("exc, expected", [
    (OSError(10051, "x"), ("NETWORK_ERROR", "network_unreachable")),
    (socket.gaierror(1, "x"), ("NETWORK_ERROR", "dns_failed")),
    (ValueError("x"), ("NETWORK_ERROR", "other")),
])
def test_classify_by_class_and_errno(nf, exc, expected):
    outer = exc
    if not isinstance(exc, ValueError):
        outer = httpcore.ConnectError("x")
        outer.__cause__ = exc
    got = nf.classify(outer)
    assert (got.code, got.reason) == expected
    assert nf.classify(got) is got
