"""SPEC FR6.2 (plan part), TK9: the real launcher scripts\crawl-studio.ps1 runs `plan --request` with the
Python of the core venv, keeps odd characters in the request path, prints one JSON line, never opens a
socket (the plan is checked offline) and creates nothing."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "crawl-studio.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell not available")

URLS = ["https://www.rfc-editor.org/rfc/rfc9309.pdf", "http://127.0.0.1/", "http://2130706433/", "https://localhost/"]


def run_launcher(request_path, work, fake_root=None, *words):
    env = {k: v for k, v in os.environ.items() if k != "OPCOS_ROOT"}
    env["CRAWL_STUDIO_WORK"] = str(work)
    if fake_root is not None:
        env["OPCOS_ROOT"] = str(fake_root)
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(LAUNCHER), "plan", "--request", str(request_path), *words],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=180, stdin=subprocess.DEVNULL,
    )


def one_line(proc):
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, (proc.stdout, proc.stderr)
    return json.loads(lines[0])


@pytest.mark.parametrize("folder", ["plain", "with space", "it's & (odd) ^ ~ ! # %x%", "d\u00e9j\u00e0 vu"])
def test_launcher_plan_keeps_the_request_path_and_prints_one_json_line(tmp_path, folder):
    holder = tmp_path / folder
    holder.mkdir()
    request = holder / "request file.json"
    request.write_text(json.dumps({"schema_version": 1, "urls": URLS}), encoding="utf-8")
    work = tmp_path / "work"
    proc = run_launcher(request, work)
    obj = one_line(proc)
    if obj.get("error", {}).get("code") == "DEPENDENCY_MISSING":
        pytest.skip("BLOCKED_ENV: the core venv is not installed on this machine")
    assert proc.returncode == 0, proc.stderr
    assert obj["kind"] == "plan" and obj["valid"] is True and obj["network_checked"] is False
    assert [i["decision"] for i in obj["items"]] == ["allow", "blocked", "blocked", "blocked"]
    assert not work.exists(), "plan must not create the work folder"


def test_launcher_plan_of_an_invalid_request_exits_2_with_one_line(tmp_path):
    request = tmp_path / "r.json"
    request.write_text('{"schema_version": 1, "urls": [], "extra": 1}', encoding="utf-8")
    proc = run_launcher(request, tmp_path / "work")
    obj = one_line(proc)
    if obj.get("error", {}).get("code") == "DEPENDENCY_MISSING":
        pytest.skip("BLOCKED_ENV: the core venv is not installed on this machine")
    assert proc.returncode == 2 and obj["error"]["code"] == "INVALID_REQUEST"


def test_launcher_plan_without_the_venv_still_prints_dependency_missing(tmp_path):
    request = tmp_path / "r.json"
    request.write_text(json.dumps({"schema_version": 1, "urls": URLS}), encoding="utf-8")
    proc = run_launcher(request, tmp_path / "work", fake_root=tmp_path / "no-opcos")
    obj = one_line(proc)
    assert proc.returncode == 0 and obj["error"]["code"] == "DEPENDENCY_MISSING"
