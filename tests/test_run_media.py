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

    def spawn(argv, env, cwd, out, err, holder=None):
        if spawned is not None:
            spawned.append(argv)
        return jobobject.spawn_in_job(argv, env, cwd, out, err, holder=holder)

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


def test_a_dns_answer_that_is_not_public_is_blocked_and_nothing_starts(world, server):
    spawned = []
    result, code = run_media(world, server, [YT], dns=urlpolicy.DNS_BLOCKED, spawned=spawned)
    assert result["items"][0]["error"]["code"] == "URL_BLOCKED" and spawned == [] and tmp_dirs(world, result) == []


def test_a_name_that_does_not_resolve_is_a_retryable_network_error(world, server):
    result, code = run_media(world, server, [YT], dns=urlpolicy.DNS_FAIL)
    error = result["items"][0]["error"]
    assert error["code"] == "NETWORK_ERROR" and error["reason"] == "dns_failed" and error["retryable"] is True


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
