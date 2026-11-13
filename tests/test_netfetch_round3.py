"""Review of TK13 (RF120-RF128): deadlines that hold in the header phase, a sniff that waits for enough bytes, binary
extension from the original or the final URL, an injected client that cannot follow redirects or keep cookies,
`truncated_body` only for a body that started, a bounded number of resolver threads, no raw URL going out."""

import json
import threading
import time

import httpcore
import httpx
import pytest

from agent_crawl_studio.urlpolicy import Policy

from test_netfetch import (LOOP, Resolver, Sink, connects, failed, fetch, hosts, lim, mock_fetch, nf, ok_body,  # noqa: F401
                           policy_for, mock_client, redirect, reply, script, server)
from test_run_batch import go, job_dir_of, world  # noqa: F401

A = "https://files.example.com/a"


# ----------------------------------------------------------------------------- RF120: absolute deadlines in the header phase
def test_a_header_that_dribbles_in_is_cut_by_item_timeout(nf, server):
    started = time.monotonic()
    out, _ = fetch(nf, server.url("/dribblehdr?n=40&every=0.25"), policy_for(server), idle_timeout=1, item_timeout=1)
    assert time.monotonic() - started < 2.5
    failed(out, "TIMEOUT", retryable=True)


def test_a_chunk_extension_that_dribbles_in_is_cut_by_item_timeout(nf, server):
    started = time.monotonic()
    out, _ = fetch(nf, server.url("/dribbleext?n=40&every=0.25"), policy_for(server), idle_timeout=1, item_timeout=1)
    assert time.monotonic() - started < 2.5
    failed(out, "TIMEOUT", retryable=True)


def test_a_job_deadline_in_the_header_phase_is_enforced_too(nf, server):
    started = time.monotonic()
    out, _ = fetch(nf, server.url("/dribblehdr?n=40&every=0.25"), policy_for(server), idle_timeout=30, item_timeout=60,
                   extra={"job_deadline": time.monotonic() + 1})
    assert time.monotonic() - started < 2.5
    failed(out, "TIMEOUT", retryable=True)


class FakeStream(httpcore.NetworkStream):
    def __init__(self):
        self.calls, self.closed = [], False

    def read(self, max_bytes, timeout=None):
        self.calls.append(("read", timeout))
        return b"x"

    def write(self, buffer, timeout=None):
        self.calls.append(("write", timeout))

    def close(self):
        self.closed = True

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.calls.append(("tls", timeout))
        return self

    def get_extra_info(self, info):
        return "extra:" + info


def test_the_deadline_stream_caps_every_operation_by_the_time_that_is_left(nf):
    backend = nf.PinnedBackend(Policy())
    inner = FakeStream()
    stream = nf.DeadlineStream(inner, backend)
    assert stream.read(10, timeout=5) == b"x" and stream.get_extra_info("socket") == "extra:socket"  # no deadline set
    backend.deadline = time.monotonic() + 0.5
    stream.read(10, timeout=5)
    stream.read(10, timeout=None)
    stream.write(b"a", timeout=9)
    assert stream.start_tls(None, "h", timeout=7).read(1, timeout=3) == b"x"
    capped = [timeout for _, timeout in inner.calls]
    assert capped[0] == 5 and all(0 < timeout <= 0.5 for timeout in capped[1:]) and len(capped) == 6
    stream.close()
    assert inner.closed
    backend.deadline = time.monotonic() - 1
    for call in (lambda: stream.read(1), lambda: stream.write(b"x"), lambda: stream.start_tls(None)):
        with pytest.raises(httpcore.ReadTimeout):
            call()


# ----------------------------------------------------------------------------- RF121 / RF122: HTML instead of a binary
def test_html_split_into_tiny_chunks_is_still_refused_for_a_pdf(nf, server):
    out, sink = fetch(nf, server.url("/report.pdf?kind=split"), policy_for(server))
    failed(out, "CONTENT_MISMATCH")
    assert sink.data == b""


def test_a_short_real_file_is_still_delivered_whole(nf, server):
    out, sink = fetch(nf, server.url("/report.pdf?size=40"), policy_for(server))
    assert out.status == "ok" and len(sink.data) == 40 and sink.data.startswith(b"%PDF-")


def test_a_short_binary_body_over_the_limit_is_refused_after_the_wait(nf, no_socket):
    stream = httpx.ByteStream(b"%PDF-1.7" + b"0" * 40)  # no Content-Length: the limit is met only while reading
    out, sink, _, _ = mock_fetch(nf, {"https://files.example.com/a.pdf": httpx.Response(200, stream=stream)},
                                 start="https://files.example.com/a.pdf", max_bytes_per_file=10)
    failed(out, "SIZE_LIMIT")
    assert sink.data == b""


LOGIN = b"<!DOCTYPE html><html>login</html>"


def after_redirect(nf, expect):
    answers = {"https://files.example.com/doc.pdf": redirect("/login"), "https://files.example.com/login": reply(200, LOGIN)}
    handler, _ = script(answers)
    client, resolver = mock_client(nf, handler)
    sink = Sink()
    out = nf.fetch_item("https://files.example.com/doc.pdf", expect, lim(), sink, Policy(resolver=resolver), client=client)
    return out, sink


@pytest.mark.parametrize("expect", ["auto", "binary"])
def test_a_binary_extension_on_the_first_url_counts_even_after_a_redirect(nf, no_socket, expect):
    out, sink = after_redirect(nf, expect)
    failed(out, "CONTENT_MISMATCH")
    assert sink.data == b""


def test_expect_html_still_accepts_the_login_page_after_such_a_redirect(nf, no_socket):
    out, sink = after_redirect(nf, "html")
    assert out.status == "ok" and sink.data == LOGIN


# ----------------------------------------------------------------------------- RF123: an injected client cannot follow or remember
def test_an_injected_client_that_follows_redirects_and_holds_cookies_is_still_held_back(nf, no_socket):
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("cookie")))
        return redirect("http://127.0.0.1/secret") if len(seen) == 1 else ok_body()
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    client.cookies.set("sid", "leak", domain="files.example.com")
    out = nf.fetch_item(A, "auto", lim(), Sink(), Policy(resolver=Resolver()), client=client)
    failed(out, "REDIRECT_BLOCKED", status="blocked")
    assert seen == [(A, None)]  # one request only, and without the cookie that was planted


# ----------------------------------------------------------------------------- RF124: truncated_body only for a started body
def test_a_protocol_error_before_any_body_is_not_a_truncated_body(nf, server):
    out, _ = fetch(nf, server.url("/te-gzip"), policy_for(server))
    failed(out, "NETWORK_ERROR", "other")


def test_a_remote_protocol_error_from_the_connection_is_other_but_in_the_body_it_is_truncated(nf):
    assert nf.classify(httpcore.RemoteProtocolError("x")).reason == "other"
    assert nf.classify(httpcore.RemoteProtocolError("x"), body=True).reason == "truncated_body"


# ----------------------------------------------------------------------------- RF126: bounded resolver threads
def test_stuck_resolver_threads_are_bounded(nf):
    release = threading.Event()
    resolver = Resolver({"slow.example": [lambda: (release.wait(5), [LOOP])[1]]})
    backend = nf.PinnedBackend(Policy(ports=(80,), ip_ok=lambda a: True, resolver=resolver))
    stuck = []
    for _ in range(nf.MAX_RESOLVER_THREADS):
        with pytest.raises(nf.NetError):
            backend.connect_tcp("slow.example", 80, timeout=0.05)
        stuck.append(1)
    started = time.monotonic()
    with pytest.raises(nf.NetError) as caught:  # no thread left: refused at once, the resolver is not asked again
        backend.connect_tcp("slow.example", 80, timeout=0.2)
    assert caught.value.code == "TIMEOUT" and time.monotonic() - started < 1
    assert len(resolver.calls) == nf.MAX_RESOLVER_THREADS
    release.set()
    for _ in range(100):  # the stuck lookups finish and give their places back
        if backend_free(nf):
            break
        time.sleep(0.05)
    assert backend_free(nf)


def backend_free(nf):
    got = [nf.RESOLVER_SLOTS.acquire(blocking=False) for _ in range(nf.MAX_RESOLVER_THREADS)]
    for ok in got:
        if ok:
            nf.RESOLVER_SLOTS.release()
    return all(got)


# ----------------------------------------------------------------------------- RF125 / RF127: nothing raw goes out
def test_an_unexpected_failure_leaves_no_traceback_and_no_text_anywhere(world, server, capsys):
    def explode(url, expect, limits, sink, policy, **kw):
        raise RuntimeError("SECRET detail")
    result, _ = go(world, server, [server.url("/ok")], deps={"fetch": explode})
    captured = capsys.readouterr()
    assert captured.err == "" and captured.out == ""
    log = open(job_dir_of(world, result) + "/logs/run.log", encoding="utf-8").read()
    assert "SECRET" not in log and "Traceback" not in log and "SECRET" not in json.dumps(result)


def test_only_the_redacted_final_url_is_ever_written(world, server):
    url = server.url("/redir?to=/file?size=10%26token=SECRET2")
    result, _ = go(world, server, [url])
    job = job_dir_of(world, result)
    for name in ("manifest.json", "logs/run.log"):
        assert "SECRET2" not in open(job + "/" + name, encoding="utf-8").read()
    assert "SECRET2" not in json.dumps(result)
    assert "REDACTED" in result["items"][0]["untrusted"]["final_url_redacted"]
