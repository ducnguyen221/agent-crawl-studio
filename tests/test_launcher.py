"""FR3.1, FR3.3: when the venv (or the runtime tool) is missing, the launcher prints ONE JSON line
with DEPENDENCY_MISSING and exits 0. Uses a fake OPCOS_ROOT; never touches the real runtime."""

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


def run_launcher(fake_root, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("OPCOS_ROOT",)}
    env["OPCOS_ROOT"] = str(fake_root)
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(LAUNCHER), *args],
        capture_output=True, text=True, env=env, timeout=120, stdin=subprocess.DEVNULL,
    )


def check_one_json(proc):
    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["error"]["code"] == "DEPENDENCY_MISSING"
    assert obj["venv"]["status"] == "missing"
    assert "ensure" in obj["error"]["hint"]
    return obj


def test_runtime_tool_missing(tmp_path):
    check_one_json(run_launcher(tmp_path, "capabilities"))


def test_resolve_fails_means_venv_missing(tmp_path):
    rt = tmp_path / "system" / "operations" / "runtime"
    rt.mkdir(parents=True)
    (rt / "opcos-runtime.ps1").write_text("exit 1\n", encoding="utf-8")
    check_one_json(run_launcher(tmp_path, "capabilities"))


def test_resolve_returns_nonexistent_python(tmp_path):
    rt = tmp_path / "system" / "operations" / "runtime"
    rt.mkdir(parents=True)
    (rt / "opcos-runtime.ps1").write_text("Write-Output 'C:\\definitely\\missing\\python.exe'\nexit 0\n", encoding="utf-8")
    check_one_json(run_launcher(tmp_path, "capabilities"))


def test_resolve_that_writes_to_stderr_but_exits_0_with_a_real_python_runs_the_command(tmp_path):
    """SPR-03 FR16.1: a stderr line from `opcos-runtime resolve` (a native tool's noise) must not turn into
    DEPENDENCY_MISSING under Windows PowerShell 5.1 when the exit code is 0 and the printed Python exists."""
    import sys

    rt = tmp_path / "system" / "operations" / "runtime"
    rt.mkdir(parents=True)
    (rt / "opcos-runtime.ps1").write_text(
        "& cmd.exe /c 'echo noise on stderr 1>&2'\nWrite-Output '%s'\nexit 0\n" % sys.executable, encoding="utf-8")
    proc = run_launcher(tmp_path, "capabilities")
    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    assert len(lines) == 1
    obj = json.loads(lines[0])
    # the core ran (it printed engines); the system python has no trafilatura, so the engine list may carry
    # DEPENDENCY_MISSING, which is not the launcher failing to find a venv
    assert "engines" in obj
    assert obj.get("venv", {}).get("status") != "missing"
