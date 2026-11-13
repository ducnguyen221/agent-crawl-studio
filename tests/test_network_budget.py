"""SPR-04 TK6 (FR23.1-FR23.3, FR24.1, SC31, SC32; DN19, RF19): the network budget of an item and of a job, `network_bytes`
in the result, and a token that never reaches `logs/egress.log`. RED on the code of SPR-03 (`21ee025`).

    runner.NETWORK_FACTOR = 2; _Job.net_item, _Job.net_job, _Job.net_room()
    netfetch.fetch_item(..., network_room=None) and `FetchOutcome.bytes_received` also when it fails
    EgressProxy(byte_cap=<net_room>); ManifestWriter.finish(..., network_bytes=None); `schema.validate_result`
    _Job.egress_line(line): filter the characters, then hide the tokens, then cut to 200 characters

The "Internet" is the local fixture server (`server.sent` counts the body bytes it wrote, `server.requests` every request)
and a fake proxy that counts what the engine would have pulled. A fake clock is not needed: the paths here wait on nothing
(the `sleep` dep of the tests is a no-op)."""

import dataclasses
import json
import os
import threading

import pytest

from agent_crawl_studio import egress, errors, manifest, netfetch, outroot, runner, schema, urlpolicy, ytdlp
from agent_crawl_studio.ytdlp import MediaOutcome

from test_fix_c import _job
from test_netfetch import Sink, fetch, lim, nf, policy_for, server  # noqa: F401
from test_run_batch import go, job_dir_of, manifest_of, request_of, world  # noqa: F401
from test_run_media import YT, media_deps

ZWSP = "​"
TOKEN = "ab" * 32


def short(server_, tag, claimed, sent):
    """A URL whose answer claims `claimed` bytes and sends `sent`, then hangs up: a retryable `truncated_body`."""
    return server_.url("/short?cl=%d&send=%d&tag=%s" % (claimed, sent, tag))


# ----------------------------------------------------------------------------- the counter of netfetch (FR23.1)
def test_a_failed_download_reports_the_bytes_it_received(nf, server):  # noqa: F811
    out, sink = fetch(nf, server.url("/short?cl=1000&send=300"), policy_for(server))
    assert out.status == "failed" and out.code == "NETWORK_ERROR" and out.reason == "truncated_body"
    assert out.bytes_received == 300 == len(sink.data)


def test_a_body_past_the_network_room_is_a_size_limit_that_still_says_what_came_in(nf, server):  # noqa: F811
    sink = Sink()
    out = nf.fetch_item(server.url("/short?cl=1000&send=1000"), "auto", lim(), sink, policy_for(server), network_room=400)
    assert out.status == "failed" and out.code == "SIZE_LIMIT" and out.retryable is False
    assert 400 < out.bytes_received <= 1000 and len(sink.data) <= 400  # what came in, but nothing past the room was kept


def test_a_room_that_is_not_crossed_changes_nothing(nf, server):  # noqa: F811
    out = nf.fetch_item(server.url("/file?size=500"), "auto", lim(), Sink(), policy_for(server), network_room=500)
    assert out.status == "ok" and out.bytes_received == 500


# ----------------------------------------------------------------------------- the budget arithmetic (FR23.1, FR23.2)
def _limited(tmp_path, **limits):
    job, job_dir = _job(tmp_path)
    job.limits = dict(job.limits, **limits)
    return job


def test_the_network_factor_is_two():
    assert runner.NETWORK_FACTOR == 2


def test_the_room_is_the_less_of_the_rest_of_the_item_and_of_the_job(tmp_path):
    job = _limited(tmp_path, max_bytes_per_file=100, max_total_bytes=150)
    assert job.net_room() == 200  # 2 x 100 for the item, 2 x 150 for the job
    job.net_item, job.net_job = 120, 120
    assert job.net_room() == 80
    job.net_item, job.net_job = 10, 250  # the job has less left (300 - 250) than the item (200 - 10)
    assert job.net_room() == 50
    job.net_item, job.net_job = 250, 250  # past the budget never shows as a negative room
    assert job.net_room() == 0


def test_count_net_adds_to_the_item_and_to_the_job(tmp_path):
    job = _limited(tmp_path)
    job.count_net(5)
    job.count_net(7)
    assert (job.net_item, job.net_job) == (12, 12)


# ----------------------------------------------------------------------------- a direct item over its attempts (SC31)
def test_attempts_of_one_item_share_a_budget_of_twice_the_per_file_limit(world, server):  # noqa: F811
    cap = 20000  # the item may take 40 000 bytes from the network over all its attempts
    result, code = go(world, server, [short(server, "a", cap, 15000)], limits={"max_bytes_per_file": cap, "retries": 3})
    item = result["items"][0]
    assert item["status"] == "failed" and item["error"]["code"] == "SIZE_LIMIT"  # the third attempt crossed the budget
    assert item["attempts"] == 3  # not the 4 attempts `retries: 3` would allow
    assert 2 * cap < result["network_bytes"] <= server.sent  # past the room by at most the chunk that crossed it
    assert result["network_bytes"] - 2 * cap < 15000
    assert len(server.requests) == 3
    assert schema.validate_result(result) == []


def cut_after(amount, calls):
    """A `fetch` that takes `amount` bytes from the network each time and then fails, retryable (a cut body)."""
    def fetch_cut(url, expect, limits, sink, policy, **kwargs):
        calls.append(kwargs.get("network_room"))
        sink(b"x" * amount)
        return netfetch.FetchOutcome(status="failed", code="NETWORK_ERROR", reason="truncated_body", http_status=200,
                                     retryable=True, bytes_received=amount, final_url=url, final_url_redacted=url)
    return fetch_cut


def test_without_room_left_the_item_is_not_tried_and_the_last_error_stays(world, server):  # noqa: F811
    cap, rooms = 20000, []
    result, code = go(world, server, [server.url("/ok")], deps={"fetch": cut_after(20000, rooms)},
                      limits={"max_bytes_per_file": cap, "retries": 3})
    item = result["items"][0]  # two attempts of 20 000 fill the budget of 40 000: no third attempt
    assert rooms == [40000, 20000]  # the room each attempt was given
    assert item["attempts"] == 2
    assert item["error"]["code"] == "NETWORK_ERROR" and item["error"]["reason"] == "truncated_body"  # the last error, kept
    assert result["network_bytes"] == 40000


def test_a_new_item_starts_with_a_new_item_budget(world, server):  # noqa: F811
    rooms = []
    result, code = go(world, server, [server.url("/ok"), server.url("/echo")], deps={"fetch": cut_after(20000, rooms)},
                      limits={"max_bytes_per_file": 20000, "retries": 3})
    assert rooms == [40000, 20000, 40000, 20000]  # the second item got a whole budget again, not what the first left
    assert [item["attempts"] for item in result["items"]] == [2, 2] and result["network_bytes"] == 80000


def test_a_job_that_spent_its_budget_opens_no_connection_for_the_items_after_it(world, server):  # noqa: F811
    limits = {"max_bytes_per_file": 50000, "max_total_bytes": 50000, "retries": 3}  # job budget: 100 000 bytes
    urls = [short(server, "d1", 40000, 30000), short(server, "d2", 40000, 30000), short(server, "d3", 40000, 30000)]
    result, code = go(world, server, urls, limits=limits)
    first, second, third = result["items"]
    assert second["error"]["code"] == "SIZE_LIMIT"  # it crossed the budget of the job while it ran
    assert third["status"] == "failed" and third["error"]["code"] == "SIZE_LIMIT" and third["artifacts"] == []
    assert not any("tag=d3" in request["path"] for request in server.requests)  # no connection, no request
    assert result["network_bytes"] > 100000 and schema.validate_result(result) == []


def test_network_bytes_is_what_the_server_wrote_when_nothing_was_cut(world, server):  # noqa: F811
    urls = [server.url("/file?size=30000"), server.url("/file?size=50000"), server.url("/status/404")]
    result, code = go(world, server, urls)
    assert code == 3 and result["network_bytes"] == 80000 == server.sent  # FR23.3, within 1 %: here exactly
    assert manifest_of(world, result)["network_bytes"] == 80000


def test_network_bytes_of_a_job_that_downloaded_nothing_is_zero(world, server):  # noqa: F811
    result, code = go(world, server, ["ftp://files.example.com/x"])
    assert result["network_bytes"] == 0


def test_a_ctrl_c_in_a_download_keeps_the_bytes_counted_so_far(world, server):  # noqa: F811
    def interrupted(url, expect, limits, sink, policy, **kwargs):
        sink(b"x" * 1000)
        raise KeyboardInterrupt

    result, code = go(world, server, [server.url("/ok")], deps={"fetch": interrupted})
    assert code == 130 and result["status"] == "cancelled" and result["network_bytes"] == 1000
    assert manifest_of(world, result)["network_bytes"] == 1000


def test_a_ctrl_c_in_the_last_write_still_says_how_much_came_in(world, server, monkeypatch):  # noqa: F811
    real = runner._write_final
    calls = []

    def once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "_write_final", once)
    result, code = go(world, server, [server.url("/file?size=700")])
    assert code == 130 and result["status"] == "cancelled" and result["network_bytes"] == 700


# ----------------------------------------------------------------------------- a media item through the proxy (SC31)
class FakeProxy:
    """What the job needs of an EgressProxy; `moved` is what the engine pulled through it in that attempt."""

    made = []

    def __init__(self, policy, *, byte_cap, connect_timeout, log):
        self.byte_cap, self.log = byte_cap, log
        self.bytes_in = 0
        self.over_cap = threading.Event()
        self.denied, self.failures = [], []
        self.url = "http://%064x:x@127.0.0.1:9" % (len(self.made) + 1)
        self.made.append(self)

    def start(self):
        return self.url

    def stop(self, timeout=2.0):
        pass


def _engine(moves):
    """A `run_media_item` that pulls `moves[n]` bytes in attempt n+1 and fails: retryable, or SIZE_LIMIT past the cap."""
    calls = []

    def run(url, item_id, dirs, limits, tools, *, budget, room, clock, deps, proxy=None):
        moved = moves[min(len(calls), len(moves) - 1)]
        calls.append(proxy.byte_cap)
        proxy.bytes_in = moved
        if moved > proxy.byte_cap:
            proxy.over_cap.set()
            return MediaOutcome("failed", "SIZE_LIMIT", None, False, None, {}, None, [])
        return MediaOutcome("failed", "NETWORK_ERROR", "connect", True, None, {}, None, [])
    run.caps = calls
    return run


@pytest.fixture
def media(monkeypatch):
    FakeProxy.made = []

    def install(moves):
        engine = _engine(moves)
        monkeypatch.setattr(ytdlp, "run_media_item", engine)
        return engine
    return install


def run_media(world, server, deps_extra=None, **limits):  # noqa: F811
    deps = media_deps("ok_mp4", egress=FakeProxy, **(deps_extra or {}))
    return go(world, server, [YT], deps=deps, mode="media", limits=limits)


def test_the_proxy_of_a_second_attempt_gets_the_rest_of_the_budget(world, server, media):  # noqa: F811
    engine = media([150000, 60000])
    result, code = run_media(world, server, max_bytes_per_file=100000, retries=3)
    item = result["items"][0]
    assert engine.caps == [200000, 50000]  # the cap of attempt 2 is what attempt 1 left
    assert item["attempts"] == 2 and item["error"]["code"] == "SIZE_LIMIT"
    assert result["network_bytes"] == 210000 and [p.byte_cap for p in FakeProxy.made] == [200000, 50000]


def test_a_media_item_with_no_room_left_is_not_tried_again(world, server, media):  # noqa: F811
    engine = media([200000])
    result, code = run_media(world, server, max_bytes_per_file=100000, retries=3)
    item = result["items"][0]
    assert engine.caps == [200000] and "attempts" not in item and len(FakeProxy.made) == 1
    assert item["error"]["code"] == "NETWORK_ERROR" and item["error"]["retryable"] is True  # the last error stays
    assert result["network_bytes"] == 200000


def test_a_media_item_after_a_spent_job_budget_starts_no_proxy_and_no_engine(world, server, media):  # noqa: F811
    engine = media([300000])
    deps = media_deps("ok_mp4", egress=FakeProxy)
    result, code = go(world, server, [YT, YT], deps=deps, mode="media",
                      limits={"max_bytes_per_file": 100000, "max_total_bytes": 100000, "retries": 0})
    first, second = result["items"]
    assert first["error"]["code"] == "SIZE_LIMIT" and second["error"]["code"] == "SIZE_LIMIT"
    assert second["adapter"] == "yt-dlp" and len(engine.caps) == 1 and len(FakeProxy.made) == 1
    assert result["network_bytes"] == 300000


def test_a_media_item_counts_what_came_through_its_proxy_when_it_ends_by_ctrl_c(world, server, monkeypatch):  # noqa: F811
    FakeProxy.made = []

    def interrupted(url, item_id, dirs, limits, tools, *, budget, room, clock, deps, proxy=None):
        proxy.bytes_in = 4321
        raise KeyboardInterrupt

    monkeypatch.setattr(ytdlp, "run_media_item", interrupted)
    result, code = run_media(world, server, retries=0)
    assert code == 130 and result["network_bytes"] == 4321


# ----------------------------------------------------------------------------- the result says it (FR23.3)
@pytest.mark.parametrize("value", [-1, 1.5, "7", True, None, [1]])
def test_a_wrong_network_bytes_is_not_a_valid_result(world, server, value):  # noqa: F811
    result, _ = go(world, server, [server.url("/ok")])
    assert schema.validate_result(dict(result, network_bytes=value)) == ["network_bytes"]


def test_a_missing_network_bytes_is_still_valid_for_an_old_result(world, server):  # noqa: F811
    result, _ = go(world, server, [server.url("/ok")])
    old = {key: value for key, value in result.items() if key != "network_bytes"}
    assert schema.validate_result(old) == [] and schema.validate_result(dict(result, network_bytes=0)) == []


def test_the_manifest_writer_adds_network_bytes_only_when_it_is_given(tmp_path):
    writer = manifest.ManifestWriter(str(tmp_path))
    writer.start("20261113-120000-aaaaaa", "t", {}, 0)
    writer.finish("succeeded", "t", write=False)
    assert "network_bytes" not in writer.doc
    writer.finish("succeeded", "t", write=False, network_bytes=0)
    assert writer.doc["network_bytes"] == 0


def test_the_result_of_auto_and_direct_equals_the_golden_sample_but_for_network_bytes(tmp_path):
    """FR21.5, SC29 (`tests/fixtures/golden/README.md`): after the normalisation of the sample, the only difference
    of an `auto` / `direct` result is `network_bytes` (the two new `limits_effective` keys come with TK7)."""
    from fixtures.golden import gen_golden
    from fixtures.httpd import LocalServer
    import socket
    work = str(tmp_path / "work")
    env = dict(os.environ, CRAWL_STUDIO_WORK=work)
    with LocalServer() as srv:
        policy = urlpolicy.Policy(
            ports=(srv.port,), ip_ok=lambda address: address == "127.0.0.1",
            resolver=lambda host, port, proto=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
        for mode in ("auto", "direct"):
            request = gen_golden.build(mode, srv)
            result, code = runner.run(request, env, {"policy": policy})
            assert code == 0
            clean = gen_golden.normalise(result, srv.port, work, result["job_id"])
            assert clean.pop("network_bytes") == 5000
            golden_path = os.path.join(os.path.dirname(__file__), "fixtures", "golden", "result-%s.json" % mode)
            with open(golden_path, encoding="utf-8") as handle:
                assert clean == json.load(handle), mode


# ----------------------------------------------------------------------------- the token stays out of the log (FR24.1, SC32)
def _lines(tmp_path, job, *lines):
    for line in lines:
        job.egress_line(line)
    job.close_egress_log()
    return (tmp_path / "job" / "logs" / "egress.log").read_text(encoding="utf-8").splitlines()


def test_token_of_the_proxy_in_a_host_is_hidden_in_the_log(tmp_path):
    job, _ = _job(tmp_path)
    job.tokens.append(TOKEN)
    assert _lines(tmp_path, job, "%s.cdn.example.com 443 allow 10" % TOKEN) == ["REDACTED.cdn.example.com 443 allow 10"]


def test_token_with_a_zero_width_space_between_its_letters_is_still_hidden(tmp_path):
    job, _ = _job(tmp_path)
    job.tokens.append(TOKEN)
    spaced = TOKEN[:20] + ZWSP + TOKEN[20:40] + ZWSP + ZWSP + TOKEN[40:]
    (line,) = _lines(tmp_path, job, "x.example.com %s 443 allow 1" % spaced)
    assert line == "x.example.com REDACTED 443 allow 1" and TOKEN not in line and ZWSP not in line


def test_a_64_hex_before_userinfo_marker_is_hidden_even_when_it_is_not_our_token(tmp_path):
    job, _ = _job(tmp_path)  # no token of ours is known yet: the shape alone is enough
    other = "0123456789abcdef" * 4
    (line,) = _lines(tmp_path, job, "%s:x@host.example 443 deny:host 0" % other)
    assert line == "REDACTED:x@host.example 443 deny:host 0"


def test_a_64_hex_with_a_zero_width_space_inside_is_hidden_too(tmp_path):
    job, _ = _job(tmp_path)
    other = "0123456789abcdef" * 4
    (line,) = _lines(tmp_path, job, other[:30] + ZWSP + other[30:] + ":x@h 443 x 0")
    assert line == "REDACTED:x@h 443 x 0"


def test_the_order_is_filter_then_hide_then_cut(tmp_path):
    job, _ = _job(tmp_path)
    job.tokens.append(TOKEN)
    # the token sits at characters 190-254: a cut before the hiding would leave 10 of its characters in the log
    line = "h" * 190 + TOKEN + " tail"
    (written,) = _lines(tmp_path, job, line)
    assert written == ("h" * 190 + "REDACTED tail")[:200] and TOKEN[:10] not in written
    assert len(written) <= 200


def test_a_line_is_cut_to_200_characters_after_the_cleaning(tmp_path):
    job, _ = _job(tmp_path)
    (written,) = _lines(tmp_path, job, ("a" + ZWSP) * 300)
    assert written == "a" * 200


def test_control_characters_never_reach_the_log(tmp_path):
    job, _ = _job(tmp_path)
    (written,) = _lines(tmp_path, job, "a\x1b[31m‮b\x00c d")
    assert written == "a[31mbcd"


def test_every_token_of_every_attempt_is_hidden(tmp_path):
    job, _ = _job(tmp_path)
    first, second = "1" * 64, "2" * 64
    job.tokens += [first, second]
    assert _lines(tmp_path, job, "%s.a.example 443 allow 1" % first, "%s.b.example 443 allow 2" % second) == [
        "REDACTED.a.example 443 allow 1", "REDACTED.b.example 443 allow 2"]


def test_a_line_that_is_not_text_is_written_as_an_empty_line(tmp_path):
    job, _ = _job(tmp_path)
    assert _lines(tmp_path, job, None) == [""]


def test_token_in_the_egress_log_of_a_real_run_is_hidden_and_found_nowhere_in_the_job(world, server):  # noqa: F811
    """End to end: the proxy of the run is real; right before it stops, a host the other side chose is logged."""
    made = []

    class Talking(egress.EgressProxy):
        def stop(self, timeout=2.0):
            token = self.url.split("//")[1].split(":")[0]
            spaced = token[:9] + ZWSP + token[9:33] + ZWSP + token[33:]
            self._emit("%s.cdn.example.com" % token, 443, "allow", 1)
            self._emit("%s.cdn.example.com" % spaced, 443, "allow", 2)
            self._emit("%s:x@evil.example.com" % ("0f" * 32), 443, "deny:host", 0)
            super().stop(timeout)

    def factory(policy, *, byte_cap, connect_timeout, log):
        proxy = Talking(urlpolicy.Policy(ports=(443,)), byte_cap=byte_cap, connect_timeout=connect_timeout, log=log)
        made.append(proxy)
        return proxy

    deps = media_deps("fail_dns", egress=factory)
    result, code = go(world, server, [YT], deps=deps, mode="media", limits={"retries": 0})
    job = job_dir_of(world, result)
    token = made[0].url.split("//")[1].split(":")[0]
    with open(os.path.join(job, "logs", "egress.log"), encoding="utf-8") as handle:
        text = handle.read()
    assert text.count("REDACTED") >= 3 and "0f" * 32 not in text
    for folder, _, names in os.walk(job):
        for name in names:
            with open(os.path.join(folder, name), "rb") as handle:
                assert token.encode() not in handle.read(), name
