"""SPEC FR6.2 (run part), TK16: the real launcher runs `run --request` with the Python of the core venv, keeps odd
characters in the path, prints ONE JSON line and gives the exit code of the job. Only blocked URLs here (no
network at all); the downloads themselves are proven with a loopback server by tests/harness_fixture_run.py."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_launcher_plan import LAUNCHER, POWERSHELL, one_line, run_launcher  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
BLOCKED = ["http://127.0.0.1/a", "http://2130706433/b", "https://localhost/c"]

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell not available")


def missing(obj):
    return obj.get("error", {}).get("code") == "DEPENDENCY_MISSING"


@pytest.mark.parametrize("folder", ["plain", "with space", "it's & (odd) ^ ~ ! # %x%"])
def test_launcher_run_of_an_all_blocked_batch_is_failed_exit_1_one_line_no_network(tmp_path, folder):
    holder = tmp_path / folder
    holder.mkdir()
    request = holder / "request file.json"
    request.write_text(json.dumps({"schema_version": 1, "urls": BLOCKED}), encoding="utf-8")
    work = tmp_path / "work"
    proc = run_launcher(request, work, command="run")
    obj = one_line(proc)
    if missing(obj):
        pytest.skip("BLOCKED_ENV: the core venv is not installed on this machine")
    assert proc.returncode == 1, proc.stderr
    assert obj["kind"] == "result" and obj["status"] == "failed" and obj["job_id"]
    assert [i["status"] for i in obj["items"]] == ["blocked"] * 3
    assert [i["error"]["code"] for i in obj["items"]] == ["URL_BLOCKED"] * 3
    manifest = json.loads((work / "jobs" / obj["job_id"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == obj  # the line on stdout is exactly the manifest


def test_launcher_run_of_an_invalid_request_exits_2_without_creating_anything(tmp_path):
    request = tmp_path / "r.json"
    request.write_text('{"schema_version": 1, "urls": [], "extra": 1}', encoding="utf-8")
    proc = run_launcher(request, tmp_path / "work", command="run")
    obj = one_line(proc)
    if missing(obj):
        pytest.skip("BLOCKED_ENV: the core venv is not installed on this machine")
    assert proc.returncode == 2 and obj["error"]["code"] == "INVALID_REQUEST" and obj["job_id"] is None
    assert not (tmp_path / "work").exists()


def test_launcher_run_never_opens_a_device_or_unc_request_path(tmp_path):
    for bad in ("NUL", "\\\\127.0.0.1\\c$\\x.json"):
        proc = run_launcher(bad, tmp_path / "work", command="run")
        obj = one_line(proc)
        if missing(obj):
            pytest.skip("BLOCKED_ENV: the core venv is not installed on this machine")
        assert proc.returncode == 2 and obj["error"]["code"] == "INVALID_REQUEST"


def test_launcher_run_ignores_the_proxy_and_private_environment(tmp_path):
    request = tmp_path / "r.json"
    request.write_text(json.dumps({"schema_version": 1, "urls": BLOCKED}), encoding="utf-8")
    env = dict(os.environ, HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9", CRAWL_STUDIO_ALLOW_PRIVATE="1")
    proc = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(LAUNCHER), "run", "--request", str(request)],
        capture_output=True, text=True, encoding="utf-8", timeout=180, stdin=subprocess.DEVNULL,
        env=dict(env, CRAWL_STUDIO_WORK=str(tmp_path / "work")))
    obj = one_line(proc)
    if missing(obj):
        pytest.skip("BLOCKED_ENV: the core venv is not installed on this machine")
    assert proc.returncode == 1 and [i["status"] for i in obj["items"]] == ["blocked"] * 3
