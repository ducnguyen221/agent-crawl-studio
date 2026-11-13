"""SPR-03 TK30 (fix round 1 of slice C and the whole sprint): the item deadline in the middle of a body read, the
100 % mark of the old modules (SC22), a firm word of the engine over the proxy's record, a shape check before the
validation of a manifest, one folder per attempt, and the size of `runner._run`."""

import ast
import io
import json
import os
import types

import httpcore
import pytest

from agent_crawl_studio import artifacts, cli, errors, netfetch, retrying, runner, schema, urlpolicy, ytdlp
from agent_crawl_studio import capabilities as cap
from agent_crawl_studio import retrycmd
from agent_crawl_studio.errors import ContractError
from agent_crawl_studio.winpath import PathBlocked

from test_netfetch import nf, policy_for, server, fetch, lim, failed  # noqa: F401
from test_retries import GOOD, Script, bad, proxies, run_media, staged
from test_retry_cmd import request_file, run_main
from test_run_batch import go, job_dir_of, request_of, world  # noqa: F401
from test_run_media import YT, tmp_dirs

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "agent_crawl_studio")


# ----------------------------------------------------------------------------- python RF1, RF6: the item deadline
def test_classify_of_a_deadline_timeout_is_a_deadline_kind():
    out = netfetch.classify(netfetch.DeadlineTimeout("deadline"))
    assert out.code == "TIMEOUT" and out.kind == "deadline"


class _Inner:
    def __init__(self):
        self.seen = []

    def read(self, max_bytes, timeout=None):
        self.seen.append(("read", timeout))
        raise httpcore.ReadTimeout("socket")

    def write(self, buffer, timeout=None):
        self.seen.append(("write", timeout))
        raise httpcore.WriteTimeout("socket")


def _stream(left):
    backend = netfetch.PinnedBackend(None, clock=lambda: 100.0)
    backend.deadline = 100.0 + left
    inner = _Inner()
    return netfetch.DeadlineStream(inner, backend), inner


def test_a_wait_that_the_deadline_cut_short_is_a_deadline_timeout_when_the_socket_runs_out():
    stream, inner = _stream(0.3)
    with pytest.raises(netfetch.DeadlineTimeout):
        stream.read(10, timeout=5)
    with pytest.raises(netfetch.DeadlineTimeout):
        stream.read(10, timeout=None)
    with pytest.raises(netfetch.DeadlineTimeout):
        stream.write(b"x", timeout=5)
    assert inner.seen[0] == ("read", pytest.approx(0.3))


def test_a_wait_that_the_deadline_did_not_cut_stays_an_idle_or_write_timeout():
    stream, _ = _stream(30)
    with pytest.raises(httpcore.ReadTimeout) as caught:
        stream.read(10, timeout=5)
    assert not isinstance(caught.value, netfetch.DeadlineTimeout)
    with pytest.raises(httpcore.WriteTimeout):
        stream.write(b"x", timeout=5)


def test_the_item_deadline_in_a_body_read_is_an_item_timeout_though_idle_is_shorter(nf, server):  # noqa: F811
    out, _ = fetch(nf, server.url("/drip?n=200&every=0.2"), policy_for(server), idle_timeout=0.5, item_timeout=1.0)
    assert out.code == "TIMEOUT" and out.timeout_kind == "item"
    assert retrying.should_retry({"status": "failed", "adapter": "direct-http",
                                  "error": {"code": "TIMEOUT", "retryable": out.retryable}}, "direct-http",
                                 out.timeout_kind) is False


def test_that_item_runs_once_through_the_runner_with_the_real_clock_of_the_backend(world, server):  # noqa: F811
    calls, sleeps = [], []

    def real(url, expect, limits, sink, policy, **kw):
        calls.append(url)
        return netfetch.fetch_item(url, expect, limits, sink, policy, **kw)

    result, _ = go(world, server, [server.url("/drip?n=200&every=0.2")], deps={"fetch": real, "sleep": sleeps.append},
                   limits={"idle_timeout": 0.5, "item_timeout": 1.0, "retries": 3})
    item = result["items"][0]
    assert len(calls) == 1 and sleeps == [] and "attempts" not in item and item["error"]["code"] == "TIMEOUT"


# ----------------------------------------------------------------------------- python RF2: the old modules at 100 %
def _job(tmp_path, check=None):
    job_dir = tmp_path / "job"
    (job_dir / "logs").mkdir(parents=True)
    return runner._Job(request_of(["https://a.example/x"]), {}, urlpolicy.Policy(), None, str(job_dir), check=check), job_dir


def test_the_egress_log_without_a_check_is_made_and_written(tmp_path):
    job, job_dir = _job(tmp_path)
    job.egress_line("a.example 443 allow 1")
    job.close_egress_log()
    assert (job_dir / "logs" / "egress.log").read_text(encoding="utf-8") == "a.example 443 allow 1\n"


def test_a_log_made_where_it_must_not_be_is_taken_away_and_logging_stops(tmp_path):
    calls = []

    def check(path):
        calls.append(path)
        if path.endswith("egress.log"):
            raise PathBlocked("reparse")

    job, job_dir = _job(tmp_path, check)
    job.egress_line("one")
    job.egress_line("two")
    assert job.egress_dead and job.egress_out is None and not (job_dir / "logs" / "egress.log").exists()
    assert len(calls) == 2  # the folder, then the file; the second line did nothing


def test_a_logs_folder_that_is_refused_before_the_file_is_made_makes_nothing(tmp_path):
    def check(path):
        raise PathBlocked("reparse")

    job, job_dir = _job(tmp_path, check)
    job.egress_line("one")
    assert job.egress_dead and os.listdir(job_dir / "logs") == []


def test_a_log_that_cannot_be_opened_ends_the_logging_without_failing(tmp_path):
    job, job_dir = _job(tmp_path)
    os.rmdir(job_dir / "logs")
    job.egress_line("one")
    assert job.egress_dead and job.egress_out is None


def test_the_engine_log_without_a_check_is_kept_under_the_name_of_the_attempt(tmp_path):
    job, job_dir = _job(tmp_path)
    io_dir = tmp_path / "io"
    io_dir.mkdir()
    (io_dir / "stderr.bin").write_bytes(b"ERROR: it failed\n")
    job.engine_log("0001", types.SimpleNamespace(io=str(io_dir)), "tok")
    assert (job_dir / "logs" / "0001-ytdlp.log").read_text(encoding="utf-8") == "ERROR: it failed\n"


def test_a_cli_retry_without_a_work_root_is_work_root_unknown_exit_2(tmp_path):
    def retry_fn(manifest_path, request, env):
        raise cap.WorkRootError("no work root")

    rc, text = run_main(["retry", "--manifest", "M", "--request", request_file(tmp_path, ["https://a.example/x"])], retry_fn)
    obj = json.loads(text)
    assert rc == 2 and obj["job_id"] is None and obj["error"]["code"] == "WORK_ROOT_UNKNOWN"


@pytest.mark.parametrize("attempts", [0, True, "2", 1.5, -1, None])
def test_a_result_with_a_wrong_attempts_is_not_valid(world, server, attempts):  # noqa: F811
    result, _ = go(world, server, [server.url("/a")], deps={"fetch": Script(bad(), GOOD), "sleep": lambda s: None})
    assert result["items"][0]["attempts"] == 2 and schema.validate_result(result) == []
    result["items"][0]["attempts"] = attempts
    assert schema.validate_result(result) == ["items[0].attempts"]


# ----------------------------------------------------------------------------- security RF2: the firm word wins
class _Run:
    returncode = 1


def _dirs(tmp_path, stderr):
    io_dir = tmp_path / "io"
    io_dir.mkdir(exist_ok=True)
    (io_dir / "stderr.bin").write_bytes(stderr)
    return types.SimpleNamespace(io=str(io_dir), dl=str(tmp_path / "dl"))


def _proxy(failures):
    return types.SimpleNamespace(failures=failures, denied=[])


@pytest.mark.parametrize("text, code, reason, retryable", [
    (b"ERROR: HTTP Error 403: Forbidden\n", "NETWORK_ERROR", "forbidden", False),
    (b"ERROR: HTTP Error 429: Too Many Requests\n", "RATE_LIMITED", None, True),
])
def test_a_firm_word_of_the_engine_beats_a_failure_of_another_host(tmp_path, text, code, reason, retryable):
    out = ytdlp._finish(_Run, _dirs(tmp_path, text), {}, [], _proxy([("cdn.example", "dns_failed")]),
                        "https://www.youtube.com/watch?v=x")
    assert (out.code, out.reason, out.retryable) == (code, reason, retryable)


def test_the_proxy_record_is_used_when_the_table_has_no_word_for_it(tmp_path):
    out = ytdlp._finish(_Run, _dirs(tmp_path, b"ERROR: unable to download: 502 Bad Gateway\n"), {}, [],
                        _proxy([("cdn.example", "dns_failed")]), "https://www.youtube.com/watch?v=x")
    assert (out.code, out.reason, out.retryable) == ("NETWORK_ERROR", "dns_failed", True)


def test_a_failure_of_the_input_host_itself_comes_before_the_table(tmp_path):
    out = ytdlp._finish(_Run, _dirs(tmp_path, b"ERROR: HTTP Error 403: Forbidden\n"), {}, [],
                        _proxy([("WWW.YouTube.com.", "dns_failed")]), "https://www.youtube.com/watch?v=x")
    assert (out.code, out.reason, out.retryable) == ("NETWORK_ERROR", "dns_failed", True)


def test_a_403_with_a_dns_failure_of_a_side_host_is_run_once(world, server):  # noqa: F811
    factory = proxies([[("cdn.example", "dns_failed")]])
    deps, sleeps = staged("fail_403", "ok_mp4", egress=factory), []
    result, _ = run_media(world, server, deps, sleeps)
    item = result["items"][0]
    assert len(deps["seen"]) == 1 and sleeps == [] and "attempts" not in item
    assert item["error"]["retryable"] is False and item["error"].get("reason") == "forbidden"


# ----------------------------------------------------------------------------- security RF1: a shape check first
def _boom(*args, **kwargs):
    raise AssertionError("the manifest must be refused before it is validated")


def test_a_manifest_of_many_empty_items_is_refused_before_the_validation(monkeypatch):
    monkeypatch.setattr(schema, "validate_result", _boom)
    with pytest.raises(ContractError) as caught:
        retrycmd._check_shape({"items": [{}] * 10000})
    assert caught.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("items", [None, "x", {}, 5])
def test_a_manifest_whose_items_are_not_a_list_is_refused_before_the_validation(monkeypatch, items):
    monkeypatch.setattr(schema, "validate_result", _boom)
    with pytest.raises(ContractError):
        retrycmd._check_shape({"items": items})
    with pytest.raises(ContractError):
        retrycmd._check_shape({})
    with pytest.raises(ContractError):
        retrycmd._check_shape([])


def test_a_manifest_of_9999_items_still_gets_to_the_validation(monkeypatch):
    monkeypatch.setattr(schema, "validate_result", lambda doc: ["x"])
    with pytest.raises(ContractError):
        retrycmd._check_shape({"items": [{}] * 9999})  # refused by the validation, not by the count


# ----------------------------------------------------------------------------- security RF5: a folder per attempt
def test_every_media_attempt_has_a_folder_of_its_own(world, server, monkeypatch):  # noqa: F811
    roots, real = [], ytdlp.make_item_dirs

    def recording(job_dir, item_id, check, attempt=1):
        dirs = real(job_dir, item_id, check, attempt)
        roots.append(os.path.basename(dirs.root))
        return dirs

    monkeypatch.setattr(ytdlp, "make_item_dirs", recording)
    deps, sleeps = staged("fail_dns", "fail_dns", "ok_mp4"), []
    result, code = run_media(world, server, deps, sleeps)
    assert code == 0 and result["items"][0]["attempts"] == 3
    assert roots == ["0001", "0001-2", "0001-3"] and tmp_dirs(world, result) == []


def test_the_sweep_takes_the_folders_of_later_attempts_too(tmp_path):
    job = tmp_path / "job"
    for name in ("0001", "0001-2", "0001-10", "keep"):
        (job / ".tmp" / name / "dl").mkdir(parents=True)
    artifacts.ArtifactWriter(str(job)).sweep()
    assert sorted(os.listdir(job / ".tmp")) == ["keep"]


def test_a_folder_of_attempt_two_is_made_next_to_the_first(tmp_path):
    first = ytdlp.make_item_dirs(str(tmp_path), "0007", None)
    second = ytdlp.make_item_dirs(str(tmp_path), "0007", None, attempt=2)
    assert os.path.basename(first.root) == "0007" and os.path.basename(second.root) == "0007-2"
    with pytest.raises(ValueError):
        ytdlp.make_item_dirs(str(tmp_path), "0007", None, attempt=0)


# ----------------------------------------------------------------------------- python RF4: the size of `_run`
def test_runner_run_is_at_most_60_lines():
    with open(os.path.join(SRC, "runner.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    sizes = {node.name: node.end_lineno - node.lineno + 1 for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert sizes["_run"] <= 60, sizes["_run"]
