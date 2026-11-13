"""SC7 harness (PLAN TK16 step 4): `tests/harness_fixture_run.py` runs a real batch against the loopback fixture server
with an injected policy, using the Python it is given: the system Python and, when the core venv exists, the
core venv Python (the one the launcher uses; it has no pytest). One JSON line, `status: succeeded`, exit 0."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "harness_fixture_run.py"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def venv_python():
    """The core venv Python as `opcos-runtime resolve` reports it (what scripts/crawl-studio.ps1 does), or None."""
    if POWERSHELL is None:
        return None
    opcos = os.environ.get("OPCOS_ROOT") or str(Path.home() / "OpcOS")
    tool = Path(opcos) / "system" / "operations" / "runtime" / "opcos-runtime.ps1"
    if not tool.exists():
        return None
    done = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tool), "resolve", "venvs",
                           "agent-crawl-studio", "core", "--python"], capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)
    last = [line for line in done.stdout.splitlines() if line.strip()][-1:] or [""]
    return last[0] if done.returncode == 0 and os.path.exists(last[0]) else None


def run_harness(python, work):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([python, "-X", "utf8", str(HARNESS), "--work", str(work)], capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=180, stdin=subprocess.DEVNULL)


def check(proc, work):
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["status"] == "succeeded" and result["counts"]["succeeded"] == 2 and result["kind"] == "result"
    job = Path(work) / "jobs" / result["job_id"]
    assert len(list((job / "files").iterdir())) == 2 and json.loads((job / "manifest.json").read_text(encoding="utf-8")) == result


def test_the_harness_runs_a_batch_with_the_current_python(tmp_path):
    check(run_harness(sys.executable, tmp_path / "work"), tmp_path / "work")


def test_the_harness_runs_a_batch_with_the_core_venv_python(tmp_path):
    python = venv_python()
    if python is None:
        pytest.skip("BLOCKED_ENV: the core venv is not installed on this machine")
    check(run_harness(python, tmp_path / "work"), tmp_path / "work")
