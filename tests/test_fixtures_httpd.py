"""The local HTTP(S) fixture itself (tests/fixtures): if a netfetch test is red, it must not be the fixture's fault.
Uses plain httpx against 127.0.0.1 (a loopback server owned by the test; no policy code is involved)."""

import socket
import ssl
import time

import httpx
import pytest

from fixtures.httpd import LocalServer
from fixtures.tls import TlsUnavailable, make_cert

MiB = 2 ** 20


@pytest.fixture
def server():
    with LocalServer() as srv:
        yield srv


def get(server, path, **kw):
    return httpx.get("http://127.0.0.1:%d%s" % (server.port, path), trust_env=False, timeout=5, **kw)


def test_ok_file_and_status_routes(server):
    assert get(server, "/ok").content == b"hello"
    assert get(server, "/ok").headers["set-cookie"].startswith("sid=abc")
    assert len(get(server, "/file?size=70000").content) == 70000
    assert get(server, "/status/404").status_code == 404
    assert get(server, "/status/429").headers["retry-after"] == "30"
    assert get(server, "/no-such-route").status_code == 404


def test_it_records_requests_connections_and_headers(server):
    get(server, "/echo?q=1", headers={"X-Probe": "yes"})
    assert server.requests[-1]["path"] == "/echo?q=1"
    assert server.requests[-1]["headers"]["x-probe"] == "yes"
    assert server.connections >= 1


def test_encoded_and_unencoded_bodies(server):
    for path, wanted in (("/enc/br?size=10", "br"), ("/enc/gzip+gzip?size=10", "gzip, gzip")):
        with httpx.stream("GET", "http://127.0.0.1:%d%s" % (server.port, path), trust_env=False, timeout=5) as resp:
            assert resp.headers["content-encoding"] == wanted
            assert b"".join(resp.iter_raw()) == b"x" * 10  # raw bytes: nothing is decoded
    assert get(server, "/nocl?size=3000").content == b"x" * 3000
    assert "content-length" not in get(server, "/nocl?size=10").headers


def test_html_and_binary_paths(server):
    page = get(server, "/doc.pdf?kind=html&size=3000")
    assert page.headers["content-type"] == "text/html" and page.content.startswith(b"<!DOCTYPE html>")
    real = get(server, "/doc.pdf?size=3000")
    assert real.headers["content-type"] == "application/pdf" and real.content.startswith(b"%PDF-")


def test_lying_content_length_both_ways(server):
    with pytest.raises(httpx.RemoteProtocolError):
        get(server, "/short?cl=1000&send=10")
    assert len(get(server, "/bigcl?cl=10&size=10").content) == 10


def test_reset_stall_and_drip(server):
    with pytest.raises(httpx.HTTPError):
        get(server, "/reset")
    with pytest.raises(httpx.ReadTimeout):
        httpx.get("http://127.0.0.1:%d/stall?send=5&delay=3" % server.port, trust_env=False, timeout=0.4)
    started = time.monotonic()
    assert len(get(server, "/drip?n=5&every=0.02").content) == 5
    assert time.monotonic() - started < 3


def test_redirect_route_and_cookie(server):
    got = get(server, "/redir?to=/ok&code=307&cookie=1", follow_redirects=False)
    assert got.status_code == 307 and got.headers["location"] == "/ok" and "hop=1" in got.headers["set-cookie"]


def test_header_route_carries_bidi_and_control_characters(server):
    got = get(server, "/hdr")
    disposition = got.headers["content-disposition"]
    assert "‮" in disposition and "\x85" in disposition and len(disposition) > 400


def test_a_client_that_stops_reading_early_leaves_sent_small(server):
    total = 64 * MiB
    with httpx.stream("GET", "http://127.0.0.1:%d/enc/gzip?size=%d" % (server.port, total), trust_env=False,
                      timeout=5) as resp:
        assert resp.headers["content-encoding"] == "gzip"  # read the header, then leave without the body
    time.sleep(0.3)
    assert server.sent < 8 * MiB, server.sent


def test_https_server_serves_a_self_signed_cert_for_the_wanted_name(tmp_path):
    try:
        crt, key = make_cert(tmp_path)
    except TlsUnavailable as exc:
        pytest.skip("BLOCKED_ENV: %s" % exc)
    with LocalServer(tls=(str(crt), str(key))) as srv:
        trusting = ssl.create_default_context(cafile=str(crt))
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as raw:
            with trusting.wrap_socket(raw, server_hostname="pinned.example") as tls:
                tls.sendall(b"GET /ok HTTP/1.1\r\nHost: pinned.example\r\nConnection: close\r\n\r\n")
                data = b""
                while True:
                    piece = tls.recv(4096)
                    if not piece:
                        break
                    data += piece
                assert data.endswith(b"hello")
        assert srv.sni == ["pinned.example"]
        with pytest.raises(ssl.SSLCertVerificationError):  # the default store does not know this certificate
            with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as raw:
                ssl.create_default_context().wrap_socket(raw, server_hostname="pinned.example")
        with pytest.raises(ssl.SSLCertVerificationError):  # trusted certificate, other name
            with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as raw:
                trusting.wrap_socket(raw, server_hostname="other.example")
