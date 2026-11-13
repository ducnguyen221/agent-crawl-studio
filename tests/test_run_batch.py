"""`runner.run`: a batch from request to manifest (SPEC FR1.5, FR1.7, FR3.2, FR3.5-FR3.8, FR4.5, FR5; PLAN TK14).
RED until TK15 writes `runner.run`.

    runner.run(request, env, deps=None) -> (result dict, exit code)
`deps` is filled only by code: `policy`, the outroot system-call parameters (`attrs_of`, `drive_type_of`,
`sync_root_of`, `free_bytes_of`, `realpath`) and `fetch` (default netfetch.fetch_item), `clock`, `now`, `token_of`,
`sleep`, `rename`, `replace`, `motw`, `create_job`. Items run one after another in the order they were given."""

import json
import os
import re
from datetime import datetime, timezone

import pytest

from agent_crawl_studio import errors, netfetch, outroot, runner, schema, urlpolicy

from test_netfetch import LOOP, Resolver, policy_for, server  # noqa: F401

GiB = 2 ** 30
JOB_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
FAKES = dict(attrs_of=lambda path: None, drive_type_of=lambda path: 3, sync_root_of=lambda path: False,
             free_bytes_of=lambda path: 10 ** 15)
NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path):
    w = {"work": str(tmp_path / "w"), "opcos": str(tmp_path / "opcos"), "home": str(tmp_path / "home")}
    w["env"] = {"CRAWL_STUDIO_WORK": w["work"], "OPCOS_ROOT": w["opcos"], "USERPROFILE": w["home"]}
    return w


def request_of(urls, **extra):
    doc = {"schema_version": 1, "urls": urls}
    doc.update(extra)
    return schema.parse_request(json.dumps(doc).encode("utf-8"))


def go(world, server_, urls, deps=None, **extra):
    all_deps = dict(FAKES, policy=policy_for(server_), sleep=lambda seconds: None)
    all_deps.update(deps or {})
    return runner.run(request_of(urls, **extra), world["env"], all_deps)


def job_dir_of(world, result):
    return os.path.join(world["work"], "jobs", result["job_id"])


def manifest_of(world, result):
    with open(os.path.join(job_dir_of(world, result), "manifest.json"), encoding="utf-8") as handle:
        return json.load(handle)


def files_of(world, result):
    return sorted(os.listdir(os.path.join(job_dir_of(world, result), "files")))


def sha256_of(path):
    import hashlib
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def wrapped_fetch(action):
    """A fetch that calls `action(n, url)` first (n = 1, 2, ...) and then the real one."""
    calls = []

    def fetch(url, expect, limits, sink, policy, **kw):
        calls.append(url)
        action(len(calls), url)
        return netfetch.fetch_item(url, expect, limits, sink, policy, **kw)
    fetch.calls = calls
    return fetch


# ----------------------------------------------------------------------------- the three ends of a batch
def test_a_batch_that_all_succeeds_writes_files_and_a_manifest_equal_to_the_result(world, server):
    urls = [server.url("/file?size=600"), server.url("/ok"), server.url("/report.pdf?size=3000")]
    result, code = go(world, server, urls)
    assert code == 0 and result["status"] == "succeeded" and result["kind"] == "result" and result["schema_version"] == 1
    assert JOB_ID.match(result["job_id"]) and result["started_at"] and result["finished_at"]
    assert schema.validate_result(result) == [] and manifest_of(world, result) == result
    assert result["counts"] == {"total": 3, "succeeded": 3, "failed": 0, "blocked": 0, "cancelled": 0, "skipped": 0}
    assert [i["id"] for i in result["items"]] == ["0001", "0002", "0003"]
    job = job_dir_of(world, result)
    for entry in result["items"]:
        assert entry["status"] == "succeeded" and entry["adapter"] == "direct-http" and entry["http_status"] == 200
        (artifact,) = entry["artifacts"]
        assert artifact["sha256"] == sha256_of(os.path.join(job, *artifact["path"].split("/")))
        assert artifact["path"].startswith("files/" + entry["id"] + "-") and len(os.path.basename(artifact["path"])) <= 32
    assert result["items"][2]["artifacts"][0]["mime"] == "application/pdf" and result["items"][1]["artifacts"][0]["mime"] == "text/plain"
    assert os.listdir(os.path.join(job, ".tmp")) == [] and len(files_of(world, result)) == 3
    assert result["limits_effective"]["max_redirects"] == 5 and result["warnings"].count("concurrency_not_applied") == 1
    json.dumps(result)


def test_one_bad_url_in_three_is_partial_exit_3_and_the_others_still_ran_in_order(world, server):
    result, code = go(world, server, [server.url("/ok"), server.url("/status/404"), server.url("/file?size=10")])
    assert code == 3 and result["status"] == "partial"
    assert [i["status"] for i in result["items"]] == ["succeeded", "failed", "succeeded"]
    bad = result["items"][1]
    assert bad["artifacts"] == [] and bad["http_status"] == 404
    assert bad["error"] == {"code": "NETWORK_ERROR", "message": errors.MESSAGES["NETWORK_ERROR"], "retryable": False,
                            "reason": "http_status", "http_status": 404}


def test_a_batch_where_nothing_succeeds_is_failed_exit_1(world, server):
    result, code = go(world, server, [server.url("/status/404"), server.url("/status/500"), "ftp://files.example.com/x"])
    assert code == 1 and result["status"] == "failed" and [i["status"] for i in result["items"]] == ["failed", "failed", "blocked"]
    assert result["items"][1]["error"]["retryable"] is True and result["items"][2]["error"]["code"] == "URL_BLOCKED"
    assert result["items"][2]["adapter"] is None


def test_the_same_url_twice_is_two_items_and_two_files(world, server):
    url = server.url("/file?size=50")
    result, code = go(world, server, [url, url])
    assert code == 0 and [i["id"] for i in result["items"]] == ["0001", "0002"]
    assert len(files_of(world, result)) == 2


def test_every_error_message_comes_from_the_fixed_table_never_from_the_response(world, server):
    result, _ = go(world, server, [server.url("/status/429"), server.url("/status/403"), server.url("/enc/gzip"),
                                   server.url("/short?cl=1000&send=10")])
    for entry in result["items"]:
        assert entry["error"]["message"] in errors.MESSAGES.values()
    assert result["items"][0]["error"]["code"] == "RATE_LIMITED" and result["items"][0]["untrusted"]["retry_after"] == "30"
    assert result["items"][2]["error"]["reason"] == "content_encoding_unsupported"
    assert result["items"][3]["error"]["reason"] == "truncated_body"


# ----------------------------------------------------------------------------- cancel, unexpected errors, timeouts
def test_ctrl_c_cancels_the_rest_removes_the_part_and_keeps_a_valid_manifest(world, server):
    def interrupt(n, url):
        if n == 2:
            raise KeyboardInterrupt
    result, code = go(world, server, [server.url("/ok"), server.url("/ok"), server.url("/ok")], deps={"fetch": wrapped_fetch(interrupt)})
    assert code == 130 and result["status"] == "cancelled"
    assert [i["status"] for i in result["items"]] == ["succeeded", "cancelled", "cancelled"]
    assert result["items"][1]["error"]["code"] == "CANCELLED" and result["items"][2]["error"]["retryable"] is True
    assert os.listdir(os.path.join(job_dir_of(world, result), ".tmp")) == []
    assert manifest_of(world, result) == result and schema.validate_result(result) == []


def test_an_unexpected_exception_is_tool_failed_for_that_item_only(world, server):
    def explode(n, url):
        if n == 1:
            raise RuntimeError("SECRET detail from inside")
    result, code = go(world, server, [server.url("/ok"), server.url("/ok")], deps={"fetch": wrapped_fetch(explode)})
    assert code == 3 and [i["status"] for i in result["items"]] == ["failed", "succeeded"]
    assert result["items"][0]["error"] == {"code": "TOOL_FAILED", "message": errors.MESSAGES["TOOL_FAILED"], "retryable": False}
    assert "SECRET" not in json.dumps(result)
    assert os.listdir(os.path.join(job_dir_of(world, result), ".tmp")) == []


def test_job_timeout_fails_the_running_item_and_every_later_one_and_the_job_even_after_a_success(world, server, fake_clock):
    fetch = wrapped_fetch(lambda n, url: setattr(fake_clock, "now", fake_clock.now + 100))
    result, code = go(world, server, [server.url("/ok")] * 3, deps={"fetch": fetch, "clock": fake_clock}, limits={"job_timeout": 150})
    assert code == 1 and result["status"] == "failed"
    assert [i["status"] for i in result["items"]] == ["succeeded", "failed", "failed"]
    assert [i["error"]["code"] for i in result["items"][1:]] == ["TIMEOUT", "TIMEOUT"] and len(fetch.calls) == 2
    assert result["items"][1]["error"]["retryable"] is True


def test_gallery_mode_makes_every_item_unsupported_without_any_network(world, server, no_socket):
    result, code = go(world, server, ["https://www.youtube.com/watch?v=abc", "https://vimeo.com/1"], mode="gallery")
    assert code == 1 and result["status"] == "failed"
    for entry in result["items"]:
        assert entry["status"] == "failed" and entry["adapter"] is None and entry["error"]["code"] == "UNSUPPORTED_SOURCE"
        assert entry["error"]["retryable"] is False


def test_the_total_byte_budget_limits_later_items(world, server):
    result, code = go(world, server, [server.url("/file?size=600"), server.url("/file?size=600")], limits={"max_total_bytes": 1000})
    assert code == 3 and [i["status"] for i in result["items"]] == ["succeeded", "failed"]
    assert result["items"][1]["error"]["code"] == "SIZE_LIMIT"


# ----------------------------------------------------------------------------- the job folder
def test_a_taken_job_id_is_never_reused_a_new_one_is_drawn(world, server):
    taken = os.path.join(world["work"], "jobs", "20261113-120000-aaaaaa")
    os.makedirs(taken)
    open(os.path.join(taken, "marker"), "w").close()
    tokens = iter(["aaaaaa", "bbbbbb"])
    result, code = go(world, server, [server.url("/ok")], deps={"now": lambda: NOW, "token_of": lambda: next(tokens)})
    assert code == 0 and result["job_id"] == "20261113-120000-bbbbbb"
    assert os.listdir(taken) == ["marker"]


def test_when_every_job_id_is_taken_the_job_is_refused_exit_2(world, server):
    taken = os.path.join(world["work"], "jobs", "20261113-120000-aaaaaa")
    os.makedirs(taken)
    open(os.path.join(taken, "marker"), "w").close()
    result, code = go(world, server, [server.url("/ok")], deps={"now": lambda: NOW, "token_of": lambda: "aaaaaa"})
    assert code == 2 and result["job_id"] is None and result["error"]["code"] == "OUTPUT_WRITE_FAILED"
    assert result["status"] == "failed" and schema.validate_result(result) == [] and os.listdir(taken) == ["marker"]


def test_an_os_error_when_creating_the_job_is_output_write_failed_exit_2(world, server):
    def refuse(root, job_id, **kw):
        raise outroot.WriteBlocked(0, "os_error")
    result, code = go(world, server, [server.url("/ok")], deps={"create_job": refuse})
    assert code == 2 and result["error"]["code"] == "OUTPUT_WRITE_FAILED" and result["job_id"] is None


def test_a_refused_output_dir_and_an_unknown_work_root_are_exit_2_and_create_nothing(world, server, tmp_path):
    result, code = go(world, server, [server.url("/ok")], output_dir="CON")
    assert code == 2 and result["error"]["code"] == "OUTPUT_PATH_BLOCKED" and result["error"]["hint"] == "device"
    assert result["job_id"] is None and not os.path.exists(world["work"])
    result, code = runner.run(request_of([server.url("/ok")]), {}, dict(FAKES, policy=policy_for(server)))
    assert code == 2 and result["error"]["code"] == "WORK_ROOT_UNKNOWN"


def test_not_enough_free_space_refuses_the_job_before_anything_is_created(world, server):
    result, code = go(world, server, [server.url("/ok")], deps={"free_bytes_of": lambda path: GiB})
    assert code == 2 and result["error"]["code"] == "OUTPUT_WRITE_FAILED" and result["error"]["hint"].startswith("missing_gib=")
    assert not os.path.exists(os.path.join(world["work"], "jobs"))


def test_space_that_runs_out_during_the_job_fails_that_item_only(world, server):
    calls = []

    def free(path):
        calls.append(path)
        return 10 ** 15 if len(calls) == 1 else GiB + 100  # 100 bytes beyond the 1 GiB reserve
    result, code = go(world, server, [server.url("/file?size=50"), server.url("/file?size=1000")], deps={"free_bytes_of": free})
    assert code == 3 and [i["status"] for i in result["items"]] == ["succeeded", "failed"]
    assert result["items"][1]["error"]["code"] == "OUTPUT_WRITE_FAILED"
    assert os.listdir(os.path.join(job_dir_of(world, result), ".tmp")) == []


def test_every_written_file_is_checked_for_junctions_after_it_is_written(world, server):
    seen = []

    def attrs(path):
        seen.append(path)
    result, code = go(world, server, [server.url("/ok")], deps={"attrs_of": attrs})
    assert code == 0
    name = os.path.basename(result["items"][0]["artifacts"][0]["path"])
    assert any(p.endswith(os.path.join("files", name)) for p in seen)


def test_a_reparse_point_found_under_files_blocks_the_item_and_stops_the_job(world, server):
    state = {"reported": False}

    def attrs(path):  # reports the junction ONCE: the second item is stopped by the job, not by a second finding
        if (os.sep + "files" + os.sep) in path and not state["reported"]:
            state["reported"] = True
            return (0x400, 0)
        return None
    fetch = wrapped_fetch(lambda n, url: None)
    result, code = go(world, server, [server.url("/ok"), server.url("/ok")], deps={"attrs_of": attrs, "fetch": fetch})
    assert len(fetch.calls) == 1  # RF146: nothing is fetched after the block
    assert code == 1 and result["status"] == "failed"
    assert [i["status"] for i in result["items"]] == ["blocked", "blocked"]
    assert all(i["error"]["code"] == "OUTPUT_PATH_BLOCKED" for i in result["items"])
    assert files_of(world, result) == []


# ----------------------------------------------------------------------------- manifest failures
def test_a_manifest_that_cannot_be_written_stops_the_job_and_stdout_is_the_memory_copy(world, server):
    calls = []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) >= 2:
            error = PermissionError(13, "in use")
            error.winerror = 32
            raise error
        os.replace(src, dst)
    result, code = go(world, server, [server.url("/ok"), server.url("/ok")], deps={"replace": replace})
    assert code == 1 and result["status"] == "failed" and "manifest_not_updated" in result["warnings"]
    assert result["error"]["code"] == "OUTPUT_WRITE_FAILED" and len(result["items"]) >= 1


def test_a_failing_final_manifest_write_is_reported_the_same_way(world, server):
    calls = []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) >= 3:  # start, one item, then the final write
            raise OSError(5, "gone")
        os.replace(src, dst)
    result, code = go(world, server, [server.url("/ok")], deps={"replace": replace})
    assert code == 1 and result["status"] == "failed" and "manifest_not_updated" in result["warnings"]
    assert result["error"]["code"] == "OUTPUT_WRITE_FAILED" and result["items"][0]["status"] == "succeeded"


# ----------------------------------------------------------------------------- what the source says
def test_source_metadata_lives_only_in_untrusted_and_is_cleaned(world, server):
    result, _ = go(world, server, [server.url("/hdr?x=1"), server.url("/redir?to=/ok&cookie=1")])
    first, second = result["items"]
    untrusted = first["untrusted"]
    assert set(untrusted) <= {"final_url_redacted", "content_type", "content_disposition", "last_modified", "retry_after"}
    assert "‮" not in untrusted["content_disposition"] and "\x85" not in untrusted["content_disposition"]
    assert len(untrusted["content_disposition"]) <= 200 and untrusted["last_modified"].startswith("Wed, 21 Oct 2015")
    assert second["redirects"] == 1 and second["untrusted"]["final_url_redacted"].endswith("/ok")
    assert "attachment" not in json.dumps(first["artifacts"])  # never in a path or a name
    assert "attachment" not in files_of(world, result)[0]


def test_secrets_in_urls_and_cookies_never_reach_the_manifest_or_the_log(world, server):
    url = server.url("/redir?to=/ok&cookie=1&token=SECRET")
    result, _ = go(world, server, [url])
    text = json.dumps(result)
    assert "SECRET" not in text and "hop=1" not in text and "sid=abc" not in text
    log_path = os.path.join(job_dir_of(world, result), "logs", "run.log")
    log = open(log_path, encoding="utf-8").read()
    assert "SECRET" not in log and "hop=1" not in log and "sid=abc" not in log
    assert len(log.splitlines()) == 1 and log.endswith("\n")
    assert result["items"][0]["url_redacted"].endswith("token=REDACTED")

def test_media_route_is_not_wired_into_run_until_tk14(world, server, no_socket):
    # transitional: TK13/TK14 replace this with the real yt-dlp run
    result, code = go(world, server, ["https://www.youtube.com/watch?v=abc"], mode="media", deps={"policy": urlpolicy.Policy()})
    assert code == 1 and result["items"][0]["error"]["code"] == "UNSUPPORTED_SOURCE"

