"""SPR-03 TK13 (FR8, FR9.5, FR10.5, FR11, SC12, SC15, SC20): `runner.run` runs a media item through the yt-dlp
adapter. The engine is the fixture `fake_ytdlp.py` (a real child process in a real Job Object); the tool lookup and
the DNS probe are injected through `deps`. No network."""

import json
import os
import sys

import pytest

from agent_crawl_studio import artifacts, runner, urlpolicy

from test_netfetch import policy_for, server  # noqa: F401
from test_run_batch import FAKES, files_of, go, job_dir_of, manifest_of, world  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "fixtures", "fake_ytdlp.py")
YT = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
FFMPEG = r"C:\tools\ffmpeg\bin\ffmpeg.exe"
NODE = r"C:\Program Files\nodejs\node.exe"


def tools_which(cmd, path=None):
    return {"ffmpeg": FFMPEG, "node": NODE}.get(cmd)


def media_deps(scenario="ok_mp4", dns=urlpolicy.DNS_OK, spawned=None, **extra):
    from agent_crawl_studio import jobobject

    def spawn(argv, env, cwd, out, err, holder=None, limits=None):
        if spawned is not None:
            spawned.append(argv)
        return jobobject.spawn_in_job(argv, env, cwd, out, err, holder=holder, limits=limits)

    deps = {"which": tools_which, "version_of": lambda name: "2026.10.26", "dns_probe": lambda host: dns,
            "engine_prefix": lambda: [sys.executable, "-P", FAKE, scenario], "poll": 0.05, "spawn": spawn,
            "policy": urlpolicy.Policy()}
    deps.update(extra)
    return deps


def run_media(world, server, urls, scenario="ok_mp4", mode="media", **extra):
    return go(world, server, urls, deps=media_deps(scenario, **extra), mode=mode)


def tmp_dirs(world, result):
    return os.listdir(os.path.join(job_dir_of(world, result), ".tmp"))


def test_a_media_item_and_a_direct_item_in_one_batch(world, server):
    result, code = run_media(world, server, [YT, server.url("/report.pdf?size=3000")], mode="auto",
                             policy=policy_for(server, extra_ports=(443,)))
    assert code == 0
    first, second = result["items"]
    assert first["adapter"] == "yt-dlp" and first["status"] == "succeeded"
    assert first["artifacts"][0]["mime"] == "video/mp4" and first["artifacts"][0]["engine"] == "yt-dlp"
    assert first["artifacts"][0]["path"].endswith(".mp4") and first["untrusted"]["title"] == "Me at the zoo"
    assert second["adapter"] == "direct-http" and second["status"] == "succeeded"
    assert tmp_dirs(world, result) == [] and manifest_of(world, result) == result


def test_a_host_outside_the_allowlist_in_media_mode_is_blocked_and_nothing_starts(world, server):
    spawned = []
    result, code = run_media(world, server, ["https://example.com/v.mp4"], spawned=spawned)
    assert result["items"][0]["error"]["code"] == "URL_BLOCKED" and spawned == []


def test_a_missing_tool_fails_the_item_with_a_fixed_hint(world, server):
    spawned = []
    result, code = run_media(world, server, [YT], spawned=spawned, which=lambda cmd, path=None: None)
    item = result["items"][0]
    assert item["status"] == "failed" and item["error"]["code"] == "DEPENDENCY_MISSING" and spawned == []
    assert item["adapter"] == "yt-dlp" and "nothing was installed" in item["error"]["hint"]


def test_a_forbidden_engine_failure_is_not_retryable(world, server):
    result, code = run_media(world, server, [YT], "fail_403")
    error = result["items"][0]["error"]
    assert code == 1 and error["code"] == "NETWORK_ERROR" and error["reason"] == "forbidden"
    assert error["retryable"] is False and tmp_dirs(world, result) == []


def test_untrusted_metadata_is_clean(world, server):
    result, code = run_media(world, server, [YT], "json_evil")
    title = result["items"][0]["untrusted"]["title"]
    assert code == 0 and len(title) <= 200 and "\x1b" not in title and "‮" not in title and "\n" not in title


def test_a_secret_in_the_engine_stderr_reaches_no_output(world, server):
    result, code = run_media(world, server, [YT], "stderr_secret")
    job = job_dir_of(world, result)
    texts = [json.dumps(result), open(os.path.join(job, "manifest.json"), encoding="utf-8").read()]
    logs = os.path.join(job, "logs")
    texts += [open(os.path.join(logs, name), encoding="utf-8").read() for name in os.listdir(logs)]
    joined = "\n".join(texts)
    assert "SECRETVALUE" not in joined and "SECRETPATH" not in joined and "REDACTED" in joined


def test_ctrl_c_while_the_file_is_taken_in_cancels_cleans_up_and_exits_130(world, server, monkeypatch):
    def boom(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(artifacts.ArtifactWriter, "adopt", boom)
    result, code = run_media(world, server, [YT])
    assert code == 130 and result["status"] == "cancelled" and result["items"][0]["status"] == "cancelled"
    assert tmp_dirs(world, result) == [] and files_of(world, result) == []


def test_ctrl_c_in_the_first_of_two_media_items_cancels_both(world, server, monkeypatch):
    def boom(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(artifacts.ArtifactWriter, "adopt", boom)
    result, code = run_media(world, server, [YT, "https://www.youtube.com/watch?v=other"])
    assert code == 130 and [i["status"] for i in result["items"]] == ["cancelled", "cancelled"]
    assert tmp_dirs(world, result) == []


def test_a_file_that_is_not_media_is_a_content_mismatch_and_is_removed(world, server, monkeypatch):
    real = artifacts.ArtifactWriter.adopt

    def html(self, item_id, src, **kwargs):
        with open(src, "wb") as handle:
            handle.write(b"<html>not a video</html>")
        return real(self, item_id, src, **kwargs)

    monkeypatch.setattr(artifacts.ArtifactWriter, "adopt", html)
    result, code = run_media(world, server, [YT])
    assert result["items"][0]["error"]["code"] == "CONTENT_MISMATCH" and files_of(world, result) == []
    assert tmp_dirs(world, result) == []


def test_the_engine_never_runs_for_a_gallery_job(world, server):
    spawned = []
    result, code = run_media(world, server, [YT], mode="gallery", spawned=spawned)
    assert result["items"][0]["error"]["code"] == "UNSUPPORTED_SOURCE" and spawned == []


@pytest.mark.parametrize("scenario,code", [("zero_files", "TOOL_FAILED"), ("ok_two_files", "TOOL_FAILED"),
                                          ("zero_files_big", "SIZE_LIMIT")])
def test_the_file_rules_end_in_a_failed_item_with_the_folder_gone(world, server, scenario, code):
    result, _ = run_media(world, server, [YT], scenario)
    assert result["items"][0]["error"]["code"] == code and tmp_dirs(world, result) == []
    assert runner.schema.validate_result(result) == []


# ---------------------------------------------------------------------------- vòng sửa 1 (review lát A)

def failing(error):
    def boom(*args, **kwargs):
        raise error
    return boom


def test_a_blocked_folder_for_the_engine_stops_the_job(world, server, monkeypatch):
    monkeypatch.setattr(runner.ytdlp, "make_item_dirs", failing(runner.PathBlocked("junction")))
    result, code = run_media(world, server, [YT, YT])
    assert [i["error"]["code"] for i in result["items"]] == ["OUTPUT_PATH_BLOCKED"] * 2


def test_a_disk_error_making_the_folders_fails_only_that_item(world, server, monkeypatch):
    monkeypatch.setattr(runner.ytdlp, "make_item_dirs", failing(OSError("disk")))
    result, code = run_media(world, server, [YT])
    assert result["items"][0]["error"]["code"] == "OUTPUT_WRITE_FAILED"


def test_a_surprise_in_one_media_item_fails_that_item_and_the_next_one_runs(world, server, monkeypatch):
    real = runner.ytdlp.build_command
    calls = []

    def once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("surprise")
        return real(*args, **kwargs)

    monkeypatch.setattr(runner.ytdlp, "build_command", once)
    result, code = run_media(world, server, [YT, "https://www.youtube.com/watch?v=other"])
    first, second = result["items"]
    assert first["error"]["code"] == "TOOL_FAILED" and first["adapter"] == "yt-dlp"
    assert second["status"] == "succeeded" and result["status"] in ("partial", "succeeded")
    assert tmp_dirs(world, result) == []


def test_a_log_that_cannot_be_written_never_fails_the_item(world, server, monkeypatch):
    monkeypatch.setattr(runner.ytdlp, "read_tail", failing(OSError("log")))
    result, code = run_media(world, server, [YT])
    assert code == 0 and result["items"][0]["status"] == "succeeded"


def test_a_junction_in_the_place_of_logs_gets_no_engine_log(world, server):
    spawned = []

    def attrs(path):
        if spawned and path.endswith(os.sep + "logs"):
            return (0x400, 0)
        return None

    result, code = run_media(world, server, [YT], "fail_403", spawned=spawned, attrs_of=attrs)
    logs = os.path.join(job_dir_of(world, result), "logs")
    assert result["items"][0]["error"]["code"] == "NETWORK_ERROR"
    assert not [name for name in os.listdir(logs) if name.endswith("-ytdlp.log")]


def test_a_tree_that_stays_is_a_job_warning_and_no_file_is_taken(world, server):
    from agent_crawl_studio import jobobject

    def stubborn(job, proc, wait=5.0):
        jobobject.kill_tree(job, proc, wait)
        return False

    result, code = run_media(world, server, [YT], kill_tree=stubborn)
    assert "process_tree_not_empty" in result["warnings"]
    assert result["items"][0]["error"]["code"] == "TOOL_FAILED" and files_of(world, result) == []
    assert manifest_of(world, result) == result


def test_ctrl_c_with_a_tree_that_stays_is_a_job_warning(world, server):
    from agent_crawl_studio import jobobject

    def stubborn(job, proc, wait=5.0):
        jobobject.kill_tree(job, proc, wait)
        return False

    def boom(directory):
        raise KeyboardInterrupt

    result, code = run_media(world, server, [YT], kill_tree=stubborn, scan=boom)
    assert code == 130 and "process_tree_not_empty" in result["warnings"]


def test_a_media_item_that_runs_past_the_job_deadline_fails_the_job(world, server, monkeypatch):
    real = runner.ytdlp.run_media_item
    state = {"late": False}

    def late(*args, **kwargs):
        outcome = real(*args, **kwargs)
        state["late"] = True
        return outcome._replace(status="failed", code="TIMEOUT", file_path=None)

    monkeypatch.setattr(runner.ytdlp, "run_media_item", late)
    clock = lambda: 10 ** 9 if state["late"] else 0.0  # noqa: E731
    result, code = run_media(world, server, [YT, YT], clock=clock)
    assert result["status"] == "failed" and result["items"][0]["error"]["code"] == "TIMEOUT"


def test_a_file_that_cannot_be_taken_in_because_of_the_path_stops_the_job(world, server, monkeypatch):
    monkeypatch.setattr(artifacts.ArtifactWriter, "adopt", failing(runner.PathBlocked("junction")))
    result, code = run_media(world, server, [YT])
    assert result["items"][0]["error"]["code"] == "OUTPUT_PATH_BLOCKED" and tmp_dirs(world, result) == []


def test_unreadable_metadata_is_flagged_on_the_artifact(world, server):
    result, code = run_media(world, server, [YT], "json_bad")
    artifact = result["items"][0]["artifacts"][0]
    assert code == 0 and "metadata_unreadable" in artifact["warnings"]


def test_adopt_gets_the_smaller_of_the_per_file_cap_and_the_budget_left(world, server, monkeypatch):
    real, seen = artifacts.ArtifactWriter.adopt, []

    def spy(self, item_id, src, **kwargs):
        seen.append(kwargs["max_bytes"])
        return real(self, item_id, src, **kwargs)

    monkeypatch.setattr(artifacts.ArtifactWriter, "adopt", spy)
    result, code = go(world, server, [YT], deps=media_deps(), mode="media", limits={"max_total_bytes": 10 ** 6})
    assert code == 0 and seen == [min(result["limits_effective"]["max_bytes_per_file"], 10 ** 6)]


# ----------------------------------------------------------- SPR-04 TK5: the core net warning (FR22.2)

def test_a_missing_core_job_is_one_warning_per_job_and_the_items_still_run(world, server, monkeypatch):
    from agent_crawl_studio import jobobject

    monkeypatch.setattr(jobobject, "_CORE_FAILED", True)
    result, code = run_media(world, server, [YT, YT + "x"])
    assert code == 0 and [item["status"] for item in result["items"]] == ["succeeded", "succeeded"]
    assert result["warnings"].count("core_job_unavailable") == 1


def test_a_present_core_job_adds_no_warning(world, server, monkeypatch):
    from agent_crawl_studio import jobobject

    monkeypatch.setattr(jobobject, "_CORE_FAILED", False)
    result, code = run_media(world, server, [YT])
    assert "core_job_unavailable" not in result["warnings"]
