"""SPR-03 TK24 (FR14.1-FR14.5, DN13, SC19): a failed item is tried again inside the job. RED until TK25.

    retrying.BACKOFF = (2, 4, 8, 16, 32); retrying.RETRY_AFTER_CAP = 120
    retrying.should_retry(entry, adapter, timeout_kind) -> bool
    retrying.wait_seconds(attempt, retry_after, now) -> float | None
    _Job.run_with_retries(index, spec) -> entry         (deps `sleep`, `now`, `clock`)
    netfetch.FetchOutcome.timeout_kind in connect / idle / item / job

Phan quyet (DN13, with the proxy's `TIMEOUT` of the SPR-03 review): a media item has no `timeout_kind` of its own, the
runner derives one. A `TIMEOUT` that the item's egress proxy recorded (a connect that did not finish, `retryable: true`)
is "connect" and is tried again; a `TIMEOUT` of `media_item_timeout` or `job_timeout` (`retryable: false`) is "item" or
"job" and is not. The direct adapter says it itself (`FetchOutcome.timeout_kind`); a TIMEOUT without a kind is not
tried again."""

import dataclasses
import email.utils
import os
import socket
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agent_crawl_studio.netfetch import FetchOutcome

from test_netfetch import LOOP, Resolver, Sink, failed, fetch, lim, nf, policy_for, server  # noqa: F401
from test_run_batch import NOW, files_of, go, job_dir_of, world  # noqa: F401
from test_run_media import YT, media_deps, tmp_dirs
from test_run_media_proxy import Rig, rig  # noqa: F401

class _Lazy:
    """`retrying` imported on first use, so that every test is red on its own (not one collection error)."""

    def __getattr__(self, name):
        import agent_crawl_studio.retrying as module
        return getattr(module, name)


retrying = _Lazy()


# ----------------------------------------------------------------------------- the policy (retrying.py)
def test_the_constants_of_the_policy():
    assert retrying.BACKOFF == (2, 4, 8, 16, 32) and retrying.RETRY_AFTER_CAP == 120


def entry(code, retryable, status="failed", adapter="direct-http"):
    item = {"id": "0001", "status": status, "adapter": adapter, "artifacts": []}
    if code is not None:
        item["error"] = {"code": code, "retryable": retryable}
    return item


SHOULD = [  # (code, retryable, adapter, timeout_kind, tried again?)
    ("NETWORK_ERROR", True, "direct-http", None, True),
    ("NETWORK_ERROR", True, "yt-dlp", None, True),            # a dns failure through the proxy
    ("NETWORK_ERROR", False, "direct-http", None, False),     # 404, 403 forbidden, a certificate that does not verify
    ("NETWORK_ERROR", False, "yt-dlp", None, False),          # 403 of yt-dlp
    ("RATE_LIMITED", True, "direct-http", None, True),
    ("RATE_LIMITED", True, "yt-dlp", None, False),            # FR14.5: retryable for `retry`, not inside the job
    ("TIMEOUT", True, "direct-http", "connect", True),
    ("TIMEOUT", True, "direct-http", "idle", True),
    ("TIMEOUT", True, "direct-http", "item", False),
    ("TIMEOUT", True, "direct-http", "job", False),
    ("TIMEOUT", True, "direct-http", None, False),
    ("TIMEOUT", True, "yt-dlp", "connect", True),             # the proxy's connect timeout
    ("TIMEOUT", False, "yt-dlp", "item", False),
    ("TIMEOUT", False, "yt-dlp", "job", False),
    ("NETWORK_ERROR", True, "extract", None, True),           # SPR-04 FR18.7: the download step is a direct download
    ("RATE_LIMITED", True, "extract", None, True),
    ("TIMEOUT", True, "extract", "connect", True),
    ("TIMEOUT", True, "extract", "idle", True),
    ("TIMEOUT", False, "extract", "extract", False),          # the extraction ran out of time: final (FR19.3)
    ("TIMEOUT", False, "extract", "job", False),
    ("TOOL_FAILED", False, "extract", None, False),
    ("CONTENT_MISMATCH", False, "extract", None, False),
    ("DEPENDENCY_MISSING", False, "extract", None, False),
    ("URL_BLOCKED", False, "direct-http", None, False),
    ("REDIRECT_BLOCKED", False, "direct-http", None, False),
    ("AUTH_REQUIRED", False, "direct-http", None, False),
    ("SIZE_LIMIT", False, "yt-dlp", None, False),
    ("CONTENT_MISMATCH", False, "direct-http", None, False),
    ("UNSUPPORTED_SOURCE", False, "direct-http", None, False),
    ("DEPENDENCY_MISSING", False, "yt-dlp", None, False),
    ("TOOL_FAILED", False, "yt-dlp", None, False),
    ("OUTPUT_WRITE_FAILED", False, "direct-http", None, False),
    ("OUTPUT_PATH_BLOCKED", False, "direct-http", None, False),
]


@pytest.mark.parametrize("code, retryable, adapter, kind, expected", SHOULD,
                         ids=["%s-%s-%s-%s" % row[:4] for row in SHOULD])
def test_should_retry_follows_dn13(code, retryable, adapter, kind, expected):
    assert retrying.should_retry(entry(code, retryable, adapter=adapter), adapter, kind) is expected


def test_only_a_failed_item_is_tried_again():
    assert retrying.should_retry(entry(None, None, "succeeded"), "direct-http", None) is False
    assert retrying.should_retry(entry("CANCELLED", True, "cancelled"), "direct-http", None) is False
    assert retrying.should_retry(entry("NETWORK_ERROR", True, "blocked"), "direct-http", None) is False
    assert retrying.should_retry(entry("NETWORK_ERROR", True, "skipped"), "direct-http", None) is False


def test_the_waits_double_and_end_at_32():
    assert [retrying.wait_seconds(n, None, NOW) for n in (1, 2, 3, 4, 5)] == [2, 4, 8, 16, 32]


@pytest.mark.parametrize("text, expected", [("7", 7), ("0", 0), ("120", 120), ("121", None), ("86400", None),
                                            ("soon", None), ("-5", None), ("1.5", None), ("", None), ("  9  ", 9)])
def test_retry_after_in_seconds(text, expected):
    assert retrying.wait_seconds(1, text, NOW) == expected


def test_retry_after_as_an_http_date():
    def at(seconds):
        return email.utils.format_datetime(NOW + timedelta(seconds=seconds), usegmt=True)
    assert retrying.wait_seconds(1, at(30), NOW) == 30
    assert retrying.wait_seconds(1, at(-60), NOW) == 0          # a date in the past: no wait
    assert retrying.wait_seconds(1, at(121), NOW) is None       # beyond the cap
    assert retrying.wait_seconds(1, "Fri, 99 Zzz 2026 99:99:99 GMT", NOW) is None


def test_a_retry_after_date_without_a_zone_is_taken_as_utc():
    assert retrying.wait_seconds(1, "Fri, 13 Nov 2026 12:00:30 -0000", NOW) == 30


def test_the_value_of_retry_after_replaces_the_backoff():
    assert retrying.wait_seconds(3, "7", NOW) == 7


# ----------------------------------------------------------------------------- limits (FR14.1)
def test_retries_zero_is_valid_and_only_the_concurrency_warning_stays():
    from agent_crawl_studio import limits
    effective, warnings = limits.resolve({"retries": 0})
    assert effective["retries"] == 0 and warnings == ["concurrency_not_applied"]
    assert limits.resolve({})[0]["retries"] == 3 and limits.resolve({})[1] == ["concurrency_not_applied"]


# ----------------------------------------------------------------------------- inside the job (direct items)
class Script:
    """A `fetch` dep that answers with the next outcome of a list (the last one repeats) and counts the calls."""

    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    def __call__(self, url, expect, limits, sink, policy, **kw):
        self.calls.append(url)
        step = self.steps[min(len(self.calls), len(self.steps)) - 1]
        if step.status == "ok":
            sink(b"hello")
        return dataclasses.replace(step, final_url=url, final_url_redacted=url)


def bad(code="NETWORK_ERROR", reason="http_status", http_status=503, retryable=True, headers=None, kind=None):
    extra = {} if kind is None else {"timeout_kind": kind}
    return FetchOutcome(status="failed", code=code, reason=reason, http_status=http_status, retryable=retryable,
                        headers_clean=headers or {}, **extra)


GOOD = FetchOutcome(status="ok", http_status=200, headers_clean={"content-type": "text/plain"}, bytes_received=5)


def run_direct(world, server, script, sleeps, urls=None, limits=None, **deps):
    deps = dict(deps, fetch=script, sleep=sleeps.append)
    return go(world, server, urls or [server.url("/a")], deps=deps, limits=limits or {})


def test_503_four_times_with_three_retries_is_four_attempts_and_waits_2_4_8(world, server):
    script, sleeps = Script(bad()), []
    result, code = run_direct(world, server, script, sleeps, limits={"retries": 3})
    item = result["items"][0]
    assert len(script.calls) == 4 and sleeps == [2, 4, 8] and item["attempts"] == 4
    assert item["status"] == "failed" and item["error"]["code"] == "NETWORK_ERROR" and item["error"]["http_status"] == 503
    assert code == 1 and result["warnings"] == ["concurrency_not_applied"]


def test_the_default_of_three_retries_now_works(world, server):
    script, sleeps = Script(bad()), []
    result, _ = run_direct(world, server, script, sleeps)
    assert len(script.calls) == 4 and result["items"][0]["attempts"] == 4


def test_retries_zero_runs_every_item_once(world, server):
    script, sleeps = Script(bad()), []
    result, _ = run_direct(world, server, script, sleeps, limits={"retries": 0})
    assert len(script.calls) == 1 and sleeps == [] and result["items"][0].get("attempts", 1) == 1


def test_an_item_that_works_on_the_second_try_succeeds_with_one_file(world, server):
    script, sleeps = Script(bad(), GOOD), []
    result, code = run_direct(world, server, script, sleeps)
    item = result["items"][0]
    assert code == 0 and item["status"] == "succeeded" and item["attempts"] == 2 and "error" not in item
    assert sleeps == [2] and len(files_of(world, result)) == 1
    assert os.listdir(os.path.join(job_dir_of(world, result), ".tmp")) == []


def test_an_item_that_works_the_first_time_has_no_attempts_and_no_wait(world, server):
    script, sleeps = Script(GOOD), []
    result, _ = run_direct(world, server, script, sleeps)
    assert sleeps == [] and len(script.calls) == 1 and "attempts" not in result["items"][0]


def test_the_wait_is_per_item_and_the_next_item_starts_with_2_again(world, server):
    script, sleeps = Script(bad(), GOOD, bad(), GOOD), []
    result, code = run_direct(world, server, script, sleeps, urls=[server.url("/a"), server.url("/b")])
    assert code == 0 and sleeps == [2, 2] and [i["attempts"] for i in result["items"]] == [2, 2]


def test_retry_after_seven_waits_seven_seconds(world, server):
    script, sleeps = Script(bad("RATE_LIMITED", None, 429, True, {"retry-after": "7"}), GOOD), []
    result, code = run_direct(world, server, script, sleeps)
    assert sleeps == [7] and code == 0 and result["items"][0]["attempts"] == 2


def test_retry_after_121_stops_after_one_attempt_and_keeps_the_error(world, server):
    script, sleeps = Script(bad("RATE_LIMITED", None, 429, True, {"retry-after": "121"}), GOOD), []
    result, code = run_direct(world, server, script, sleeps)
    item = result["items"][0]
    assert len(script.calls) == 1 and sleeps == [] and item["error"]["code"] == "RATE_LIMITED"
    assert item["error"]["retryable"] is True and "attempts" not in item


def test_retry_after_as_a_date_30_seconds_ahead_waits_30(world, server):
    when = email.utils.format_datetime(NOW + timedelta(seconds=30), usegmt=True)
    script, sleeps = Script(bad(headers={"retry-after": when}), GOOD), []
    result, _ = run_direct(world, server, script, sleeps, now=lambda: NOW)
    assert sleeps == [30] and result["items"][0]["attempts"] == 2


def test_retry_after_of_a_503_is_read_but_of_a_500_it_is_not(world, server):
    script, sleeps = Script(bad(http_status=500, headers={"retry-after": "7"}), GOOD), []
    run_direct(world, server, script, sleeps)
    assert sleeps == [2]  # FR14.3 names 429 and 503 only


@pytest.mark.parametrize("kind, calls", [("idle", 4), ("connect", 4), ("item", 1), ("job", 1), (None, 1)])
def test_a_timeout_is_tried_again_only_when_it_is_the_connect_or_the_idle_one(world, server, kind, calls):
    script, sleeps = Script(bad("TIMEOUT", None, None, True, kind=kind)), []
    result, _ = run_direct(world, server, script, sleeps)
    assert len(script.calls) == calls and result["items"][0]["error"]["code"] == "TIMEOUT"


@pytest.mark.parametrize("outcome", [
    bad("NETWORK_ERROR", "http_status", 404, False),
    bad("NETWORK_ERROR", "forbidden", 403, False),
    bad("NETWORK_ERROR", "tls_verify_failed", None, False),
    bad("AUTH_REQUIRED", None, 401, False),
    bad("SIZE_LIMIT", None, 200, False),
    bad("CONTENT_MISMATCH", None, 200, False),
    FetchOutcome(status="blocked", code="URL_BLOCKED", reason="ip_not_public"),
    FetchOutcome(status="blocked", code="REDIRECT_BLOCKED", reason="downgrade"),
], ids=["404", "403", "tls", "401", "size", "mismatch", "url_blocked", "redirect_blocked"])
def test_an_error_that_cannot_change_is_run_once(world, server, outcome):
    script, sleeps = Script(outcome), []
    run_direct(world, server, script, sleeps)
    assert len(script.calls) == 1 and sleeps == []


def test_a_wait_that_would_pass_the_job_timeout_is_not_waited(world, server, fake_clock):
    script = Script(bad())
    result, _ = go(world, server, [server.url("/a")], deps={"fetch": script, "sleep": fake_clock.sleep, "clock": fake_clock},
                   limits={"job_timeout": 5})
    # 0 + 2 < 5 -> wait 2 (now 2); 2 + 4 >= 5 -> no wait: two attempts, the last error kept
    assert len(script.calls) == 2 and fake_clock.sleeps == [2]
    assert result["items"][0]["attempts"] == 2 and result["items"][0]["error"]["code"] == "NETWORK_ERROR"
    assert result["status"] == "failed"


def test_a_first_wait_longer_than_the_whole_job_is_not_waited(world, server, fake_clock):
    script = Script(bad())
    go(world, server, [server.url("/a")], deps={"fetch": script, "sleep": fake_clock.sleep, "clock": fake_clock}, limits={"job_timeout": 1})
    assert len(script.calls) == 1 and fake_clock.sleeps == []


def test_ctrl_c_in_the_wait_cancels_the_job_and_exits_130(world, server):
    script = Script(bad())

    def interrupted(seconds):
        raise KeyboardInterrupt

    result, code = go(world, server, [server.url("/a"), server.url("/b")], deps={"fetch": script, "sleep": interrupted})
    assert code == 130 and result["status"] == "cancelled"
    assert [i["status"] for i in result["items"]] == ["cancelled", "cancelled"] and len(script.calls) == 1
    assert files_of(world, result) == [] and os.listdir(os.path.join(job_dir_of(world, result), ".tmp")) == []


# ----------------------------------------------------------------------------- inside the job (media items)
def staged(*scenarios, spawned=None, **extra):
    """media_deps whose engine plays the scenarios in turn (the last one repeats): one per attempt."""
    seen = []

    def prefix():
        seen.append(1)
        return [sys.executable, "-P", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "fake_ytdlp.py"),
                scenarios[min(len(seen), len(scenarios)) - 1]]

    deps = media_deps("ok_mp4", spawned=spawned, **extra)
    deps["engine_prefix"] = prefix
    deps["seen"] = seen
    return deps


def run_media(world, server, deps, sleeps, **limits):
    deps = dict(deps, sleep=sleeps.append)
    return go(world, server, [YT], deps=deps, mode="media", limits=limits)


def test_a_media_dns_failure_then_a_good_run_succeeds_with_two_attempts(world, server):
    deps, sleeps = staged("fail_dns", "ok_mp4"), []
    result, code = run_media(world, server, deps, sleeps)
    item = result["items"][0]
    assert code == 0 and item["status"] == "succeeded" and item["attempts"] == 2 and sleeps == [2]
    assert item["artifacts"][0]["engine"] == "yt-dlp" and tmp_dirs(world, result) == [] and len(files_of(world, result)) == 1


def test_a_media_429_is_run_once_and_stays_retryable_for_the_retry_command(world, server):
    deps, sleeps = staged("fail_429", "ok_mp4"), []
    result, code = run_media(world, server, deps, sleeps)
    item = result["items"][0]
    assert len(deps["seen"]) == 1 and sleeps == [] and item["error"]["code"] == "RATE_LIMITED"
    assert item["error"]["retryable"] is True and "attempts" not in item


def test_a_media_403_is_run_once(world, server):
    deps, sleeps = staged("fail_403", "ok_mp4"), []
    result, _ = run_media(world, server, deps, sleeps)
    assert len(deps["seen"]) == 1 and result["items"][0]["error"]["retryable"] is False


def test_the_media_item_timeout_is_run_once(world, server):
    deps, sleeps = staged("sleep_then_ok"), []
    result, _ = run_media(world, server, deps, sleeps, media_item_timeout=0.3)
    item = result["items"][0]
    assert item["error"]["code"] == "TIMEOUT" and item["error"]["retryable"] is False
    assert len(deps["seen"]) == 1 and sleeps == []


class Proxy:
    """An egress proxy that only records: `failures` is what it saw go wrong (the engine says only "502")."""

    made = None

    def __init__(self, failures, token):
        self.failures, self.denied, self.url = failures, [], "http://%s:x@127.0.0.1:1" % token
        self.over_cap = type("Never", (), {"is_set": staticmethod(lambda: False)})()
        self.started = self.stopped = 0
        self.bytes_in = 0

    def start(self):
        self.started += 1
        return self.url

    def stop(self):
        self.stopped += 1


def proxies(world_tokens):
    made = []

    def make(policy, *, byte_cap, connect_timeout, log):
        proxy = Proxy(world_tokens[min(len(made), len(world_tokens) - 1)], "%064x" % (len(made) + 1))
        made.append(proxy)
        return proxy
    make.made = made
    return make


def test_a_connect_timeout_of_the_proxy_is_tried_again(world, server):  # the TIMEOUT of the SPR-03 review, FR14.2
    factory = proxies([[("www.youtube.com", "timeout")], []])
    deps, sleeps = staged("fail_tunnel_502", "ok_mp4", egress=factory), []
    result, code = run_media(world, server, deps, sleeps)
    item = result["items"][0]
    assert code == 0 and item["status"] == "succeeded" and item["attempts"] == 2 and sleeps == [2]
    assert [(p.started, p.stopped) for p in factory.made] == [(1, 1), (1, 1)]  # a proxy of its own for every attempt


def test_a_dns_failure_through_the_proxy_is_tried_again_and_other_codes_are_not(world, server):
    factory = proxies([[("www.youtube.com", "dns_failed")], [("www.youtube.com", "other")], []])
    deps, sleeps = staged("fail_tunnel_502", "fail_tunnel_502", "ok_mp4", egress=factory), []
    result, code = run_media(world, server, deps, sleeps)
    assert result["items"][0]["attempts"] == 3 and sleeps == [2, 4] and code == 0


def test_a_timeout_of_the_proxy_that_never_goes_away_stops_after_the_retries(world, server):
    factory = proxies([[("www.youtube.com", "timeout")]])
    deps, sleeps = staged("fail_tunnel_502", egress=factory), []
    result, _ = run_media(world, server, deps, sleeps, retries=2)
    item = result["items"][0]
    assert item["attempts"] == 3 and item["error"]["code"] == "TIMEOUT" and item["error"]["retryable"] is True
    assert sleeps == [2, 4] and len(factory.made) == 3 and all(p.stopped == 1 for p in factory.made)


def test_every_attempt_has_its_own_proxy_token_log_and_closed_handles(world, server, rig):  # noqa: F811
    deps, sleeps = staged("fail_dns", "via_proxy", egress=rig.factory()), []
    result, code = run_media(world, server, deps, sleeps, max_bytes_per_file=100000)
    assert code == 0 and result["items"][0]["attempts"] == 2 and len(rig.proxies) == 2
    first, second = (proxy.url.split("//")[1].split(":")[0] for proxy in rig.proxies)
    assert first != second
    job = job_dir_of(world, result)
    for folder, _, names in os.walk(job):
        for name in names:
            with open(os.path.join(folder, name), "rb") as handle:
                data = handle.read()
            assert first.encode() not in data and second.encode() not in data, name
    with open(os.path.join(job, "logs", "egress.log"), encoding="utf-8") as handle:
        assert handle.read().splitlines() == ["www.youtube.com 443 allow 150000"]  # only the second attempt connected
    assert os.path.exists(os.path.join(job, "logs", "0001-ytdlp.log"))  # the first attempt's stderr
    for proxy in rig.proxies:
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", int(proxy.url.rsplit(":", 1)[1])), timeout=2).close()
    os.remove(os.path.join(job, "logs", "egress.log"))  # on Windows an open handle would refuse this
    os.remove(os.path.join(job, "logs", "run.log"))


def test_the_engine_log_of_every_attempt_is_kept_under_its_own_name(world, server):
    deps, sleeps = staged("fail_dns"), []
    result, _ = run_media(world, server, deps, sleeps, retries=1)
    names = sorted(name for name in os.listdir(os.path.join(job_dir_of(world, result), "logs")) if "ytdlp" in name)
    assert names == ["0001-ytdlp-2.log", "0001-ytdlp.log"]


# ----------------------------------------------------------------------------- what netfetch says about a TIMEOUT
def test_a_slow_name_lookup_is_a_connect_timeout(nf, server):  # noqa: F811
    import time
    resolver = Resolver({"pinned.example": [lambda: (time.sleep(3), [LOOP])[1]]})
    out, _ = fetch(nf, server.url("/ok"), policy_for(server, resolver), connect_timeout=0.4)
    failed(out, "TIMEOUT", retryable=True)
    assert out.timeout_kind == "connect"


def test_a_server_that_stops_answering_is_an_idle_timeout(nf, server):  # noqa: F811
    out, _ = fetch(nf, server.url("/slow?delay=3"), policy_for(server), idle_timeout=0.4)
    failed(out, "TIMEOUT", retryable=True)
    assert out.timeout_kind == "idle"
    out, _ = fetch(nf, server.url("/stall?send=5&delay=5"), policy_for(server), idle_timeout=0.4)
    assert out.code == "TIMEOUT" and out.timeout_kind == "idle"


def test_the_item_deadline_is_an_item_timeout_also_when_it_cuts_a_wait_that_was_shorter_than_idle(nf, server):  # noqa: F811
    out, _ = fetch(nf, server.url("/drip?n=200&every=0.05"), policy_for(server), idle_timeout=5, item_timeout=0.5)
    assert out.code == "TIMEOUT" and out.timeout_kind == "item"
    out, _ = fetch(nf, server.url("/stall?send=5&delay=5"), policy_for(server), idle_timeout=30, item_timeout=0.5)
    assert out.code == "TIMEOUT" and out.timeout_kind == "item"


def test_the_job_deadline_is_a_job_timeout(nf, server, fake_clock):  # noqa: F811
    fake_clock.now = 100
    out, _ = fetch(nf, server.url("/ok"), policy_for(server), extra={"clock": fake_clock, "job_deadline": 50})
    assert out.code == "TIMEOUT" and out.timeout_kind == "job"
    sink = Sink(fake_clock, advance=60)
    fake_clock.now = 100
    out, _ = fetch(nf, server.url("/file?size=%d" % (4 * 2 ** 20)), policy_for(server), sink=sink,
                   extra={"clock": fake_clock, "job_deadline": 150})
    assert out.code == "TIMEOUT" and out.timeout_kind == "job"


def test_a_late_item_by_the_injected_clock_is_an_item_timeout(nf, server, fake_clock):  # noqa: F811
    sink = Sink(fake_clock, advance=3)
    out, _ = fetch(nf, server.url("/file?size=%d" % (4 * 2 ** 20)), policy_for(server), sink=sink, item_timeout=5,
                   extra={"clock": fake_clock})
    assert out.code == "TIMEOUT" and out.timeout_kind == "item"


def test_an_outcome_that_is_not_a_timeout_has_no_kind(nf, server):  # noqa: F811
    out, _ = fetch(nf, server.url("/ok"), policy_for(server))
    assert out.status == "ok" and out.timeout_kind is None
    out, _ = fetch(nf, server.url("/status/503"), policy_for(server))
    assert out.code == "NETWORK_ERROR" and out.timeout_kind is None
