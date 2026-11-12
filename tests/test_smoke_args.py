"""FR4.1, FR4.2, FR4.3: smoke_download.py classifies results as ok / NOT_RUN / BLOCKED_ENV / FAIL
using injected collaborators only (no network, no real yt-dlp, no real ffmpeg).
Round 1 (review RF): timeouts, output-path confinement, port and DNS checks, unique job names."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_crawl_studio import capabilities as cap
from agent_crawl_studio import config, flags, urlpolicy

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("smoke_download", ROOT / "scripts" / "smoke_download.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
FFMPEG = r"C:\tools\ffmpeg\bin\ffmpeg.exe"
FFPROBE = r"C:\tools\ffmpeg\bin\ffprobe.exe"
NODE = r"C:\Program Files\nodejs\node.exe"
NOW = lambda: __import__("datetime").datetime(2026, 11, 13, 1, 2, 3)  # noqa: E731
RAND = lambda: "abc123"  # noqa: E731
STAMP = "smoke-20261113-010203-abc123"


def which(cmd, path=None):
    return {"ffmpeg": FFMPEG, "node": NODE, "ffprobe": FFPROBE}.get(cmd)


def env_for(tmp_path, **extra):
    env = {"PATH": "x", "ANTHROPIC_API_KEY": "leak", "CRAWL_STUDIO_WORK": str(tmp_path / "root")}
    env.update(extra)
    return env


def work_dir(tmp_path):
    """A legal --work value: a folder under <work_root>/jobs."""
    return tmp_path / "root" / "jobs" / "w"


def job_files(work):
    return Path(work) / STAMP / "files"


class Recorder:
    def __init__(self, ytdlp=(0, "", ""), probe=(0, "19.0\n", ""), write_file=True, size=10):
        self.calls = []
        self.kw = []
        self.ytdlp = ytdlp
        self.probe = probe
        self.write_file = write_file
        self.size = size
        self.work = None

    def __call__(self, argv, env=None, timeout=None, cwd=None):
        self.calls.append((list(argv), env))
        self.kw.append({"timeout": timeout, "cwd": cwd})
        name = os.path.basename(argv[0]).lower()
        if "ffprobe" in name:
            return self.probe
        if "-m" in argv and "yt_dlp" in argv:
            if self.write_file and self.ytdlp[0] == 0:
                files = job_files(self.work)
                (files / "jNQXAC9IVRw.mp4").write_bytes(b"x" * self.size)
            return self.ytdlp
        return (0, "", "")


def go(tmp_path, rec, argv=None, dns=lambda host: smoke.DNS_OK, env=None, rand=RAND, which_=which):
    rec.work = work_dir(tmp_path)
    out = io.StringIO()
    rc = smoke.main(
        argv or ["--url", URL, "--work", str(work_dir(tmp_path))],
        out=out, run=rec, dns_probe=dns, which=which_, env=env if env is not None else env_for(tmp_path),
        now=NOW, rand=rand,
    )
    text = out.getvalue()
    assert text.count("\n") == 1
    return rc, json.loads(text)


def job(tmp_path):
    return work_dir(tmp_path) / STAMP


def test_ok_path(tmp_path):
    rec = Recorder()
    rc, obj = go(tmp_path, rec)
    assert rc == 0 and obj["status"] == "ok"
    assert obj["bytes"] == 10 and obj["file"].endswith("jNQXAC9IVRw.mp4")
    for sub in ("files", ".tmp", "logs"):
        assert (job(tmp_path) / sub).is_dir()
    assert "leak" not in json.dumps(obj)


def test_command_is_exactly_the_fixed_flags_isolated_and_clean_env(tmp_path):
    rec = Recorder()
    go(tmp_path, rec)
    argv, env = rec.calls[0]
    expected_flags = flags.fixed_ytdlp_flags(
        os.path.dirname(FFMPEG), str(job(tmp_path)), smoke.MAX_FILESIZE, smoke.SOCKET_TIMEOUT, NODE
    )
    assert argv == [sys.executable, "-P", "-X", "utf8", "-m", "yt_dlp"] + expected_flags + ["--", URL]
    for forbidden in flags.FORBIDDEN_FLAGS:
        assert forbidden not in argv
    assert "ANTHROPIC_API_KEY" not in env
    assert env["USERPROFILE"] == str(job(tmp_path) / ".tmp")
    log = (job(tmp_path) / "logs" / "command.txt").read_text(encoding="utf-8")
    assert "--no-remote-components" in log and "--js-runtimes" in log


def test_download_runs_in_job_dir_with_item_timeout_and_probe_uses_clean_env(tmp_path):
    rec = Recorder()
    go(tmp_path, rec)
    assert rec.kw[0] == {"timeout": smoke.ITEM_TIMEOUT, "cwd": str(job(tmp_path))}
    assert smoke.ITEM_TIMEOUT == 45 * 60
    probe_argv, probe_env = rec.calls[1]
    assert os.path.basename(probe_argv[0]).lower() == "ffprobe.exe"
    assert "ANTHROPIC_API_KEY" not in probe_env
    assert rec.kw[1]["timeout"] == smoke.PROBE_TIMEOUT and rec.kw[1]["cwd"] == str(job(tmp_path))


def test_dns_blocked_is_blocked_env_not_fail(tmp_path):
    rec = Recorder()
    rc, obj = go(tmp_path, rec, dns=lambda host: smoke.DNS_FAIL)
    assert rc == 1 and obj["status"] == "BLOCKED_ENV" and obj["reason"] == "dns"
    assert rec.calls == []


def test_dns_to_non_public_address_is_url_blocked(tmp_path):
    rec = Recorder()
    rc, obj = go(tmp_path, rec, dns=lambda host: smoke.DNS_BLOCKED)
    assert rc == 1 and obj["status"] == "FAIL" and obj["error"]["code"] == "URL_BLOCKED"
    assert rec.calls == []


def test_name_resolution_error_from_engine_is_blocked_env(tmp_path):
    rec = Recorder(ytdlp=(1, "", "ERROR: [youtube] x: Unable to download webpage: [Errno 11001] getaddrinfo failed"))
    rc, obj = go(tmp_path, rec)
    assert rc == 1 and obj["status"] == "BLOCKED_ENV"


def test_engine_failure_with_network_is_fail_and_hides_stderr(tmp_path):
    rec = Recorder(ytdlp=(1, "", "ERROR: Sign in to confirm you are not a bot SECRETSTDERR"))
    rc, obj = go(tmp_path, rec)
    assert rc == 1 and obj["status"] == "FAIL"
    assert "SECRETSTDERR" not in json.dumps(obj)
    assert "SECRETSTDERR" in (job(tmp_path) / "logs" / "stderr.txt").read_text(encoding="utf-8")


def test_engine_timeout_is_fail_timeout(tmp_path):
    rc, obj = go(tmp_path, Recorder(ytdlp=(cap.TIMEOUT_RC, "", "")))
    assert rc == 1 and obj["status"] == "FAIL" and obj["reason"] == "timeout"


def test_ffprobe_timeout_is_fail_timeout(tmp_path):
    rc, obj = go(tmp_path, Recorder(probe=(cap.TIMEOUT_RC, "", "")))
    assert rc == 1 and obj["status"] == "FAIL" and obj["reason"] == "timeout"


def test_ffprobe_runner_exception_is_fail_without_leak(tmp_path):
    class Boom(Recorder):
        def __call__(self, argv, env=None, timeout=None, cwd=None):
            if "ffprobe" in os.path.basename(argv[0]).lower():
                raise OSError("SECRETPROBE")
            return super().__call__(argv, env=env, timeout=timeout, cwd=cwd)

    rc, obj = go(tmp_path, Boom())
    assert rc == 1 and obj["status"] == "FAIL" and obj["reason"] == "runner_error"
    assert "SECRETPROBE" not in json.dumps(obj)


def test_missing_ffprobe_is_blocked_env_before_any_run(tmp_path):
    rec = Recorder()
    rc, obj = go(tmp_path, rec, which_=lambda c, path=None: None if c == "ffprobe" else which(c, path))
    assert rc == 1 and obj["status"] == "BLOCKED_ENV" and obj["error"]["code"] == "DEPENDENCY_MISSING"
    assert rec.calls == [] and not work_dir(tmp_path).exists()


def test_relative_tool_path_from_which_is_rejected(tmp_path):
    rec = Recorder()
    rc, obj = go(tmp_path, rec, which_=lambda c, path=None: r".\node.exe" if c == "node" else which(c, path))
    assert rc == 1 and obj["status"] == "BLOCKED_ENV"
    assert rec.calls == []


def test_zero_byte_file_is_fail(tmp_path):
    rc, obj = go(tmp_path, Recorder(size=0))
    assert rc == 1 and obj["status"] == "FAIL"


def test_no_file_is_fail(tmp_path):
    rc, obj = go(tmp_path, Recorder(write_file=False))
    assert rc == 1 and obj["status"] == "FAIL"


def test_unreadable_by_ffprobe_is_fail(tmp_path):
    rc, obj = go(tmp_path, Recorder(probe=(1, "", "Invalid data")))
    assert rc == 1 and obj["status"] == "FAIL"


def test_plan_only_is_not_run(tmp_path):
    rec = Recorder()
    rc, obj = go(tmp_path, rec, argv=["--url", URL, "--work", str(work_dir(tmp_path)), "--plan-only"])
    assert rc == 1 and obj["status"] == "NOT_RUN"
    assert rec.calls == []


def test_missing_ffmpeg_or_node_is_blocked_env(tmp_path):
    rec = Recorder()
    rc, obj = go(tmp_path, rec, which_=lambda c, path=None: None)
    assert rc == 1 and obj["status"] == "BLOCKED_ENV" and obj["error"]["code"] == "DEPENDENCY_MISSING"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=x",
        "https://evil.example.com/v.mp4",
        "file:///C:/x",
        "https://user:pw@youtube.com/x",
        "https://www.youtube.com:8443/watch?v=x",
        "https://www.youtube.com:80/watch?v=x",
        "https://www.youtube.com:0/watch?v=x",
        "https://www.youtube.com:abc/watch?v=x",
    ],
)
def test_url_outside_strict_profile_is_blocked(tmp_path, url):
    rec = Recorder()
    rc, obj = go(tmp_path, rec, argv=["--url", url, "--work", str(work_dir(tmp_path))])
    assert rc == 1 and obj["status"] == "FAIL" and obj["error"]["code"] == "URL_BLOCKED"
    assert rec.calls == []


def test_explicit_port_443_is_allowed(tmp_path):
    rc, obj = go(tmp_path, Recorder(), argv=["--url", "https://www.youtube.com:443/watch?v=jNQXAC9IVRw", "--work", str(work_dir(tmp_path))])
    assert rc == 0 and obj["status"] == "ok"


def test_runner_exception_is_fail_without_leak(tmp_path):
    def boom(argv, env=None, timeout=None, cwd=None):
        raise OSError("SECRETEXC")

    out = io.StringIO()
    rc = smoke.main(["--url", URL, "--work", str(work_dir(tmp_path))], out=out, run=boom,
                    dns_probe=lambda h: smoke.DNS_OK, which=which, env=env_for(tmp_path), now=NOW, rand=RAND)
    obj = json.loads(out.getvalue())
    assert rc == 1 and obj["status"] == "FAIL" and "SECRETEXC" not in out.getvalue()


def test_keyboard_interrupt_exits_130_with_fixed_error(tmp_path):
    def stop(argv, env=None, timeout=None, cwd=None):
        raise KeyboardInterrupt

    out = io.StringIO()
    rc = smoke.main(["--url", URL, "--work", str(work_dir(tmp_path))], out=out, run=stop,
                    dns_probe=lambda h: smoke.DNS_OK, which=which, env=env_for(tmp_path), now=NOW, rand=RAND)
    obj = json.loads(out.getvalue())
    assert rc == 130 and obj["error"]["code"] == "INTERRUPTED"


def test_bad_arguments_exit_2_one_json_line():
    out = io.StringIO()
    rc = smoke.main(["--nope"], out=out)
    assert rc == 2 and json.loads(out.getvalue())["error"]["code"] == "USAGE_ERROR"


# --- SE-RF1: --work is confined under <work_root>/jobs ------------------------------------------

def _blocked(tmp_path, work):
    rec = Recorder()
    rc, obj = go(tmp_path, rec, argv=["--url", URL, "--work", str(work)])
    assert rc == 1 and obj["status"] == "FAIL" and obj["error"]["code"] == "OUTPUT_PATH_BLOCKED"
    assert rec.calls == []
    return obj


def test_work_outside_root_is_blocked_and_not_created(tmp_path):
    outside = tmp_path / "elsewhere"
    _blocked(tmp_path, outside)
    assert not outside.exists()


def test_work_equal_to_work_root_or_jobs_parent_is_blocked(tmp_path):
    _blocked(tmp_path, tmp_path / "root")


def test_work_with_dotdot_escape_is_blocked(tmp_path):
    escape = tmp_path / "root" / "jobs" / ".." / ".." / "escaped"
    _blocked(tmp_path, escape)
    assert not (tmp_path / "escaped").exists()


def test_relative_work_is_resolved_then_blocked(tmp_path):
    _blocked(tmp_path, Path("relative-dir"))


def test_work_on_unc_path_is_blocked(tmp_path):
    _blocked(tmp_path, r"\\server\share\jobs\x")


def test_work_on_another_drive_is_blocked(tmp_path):
    other = "Z:\\" if not str(tmp_path).upper().startswith("Z:") else "Y:\\"
    _blocked(tmp_path, other + "jobs\\x")


def test_work_through_junction_that_escapes_is_blocked(tmp_path):
    jobs = tmp_path / "root" / "jobs"
    jobs.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    link = jobs / "link"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")
    _blocked(tmp_path, link / "sub")
    assert list(target.iterdir()) == []


def test_work_component_starting_with_smoke_is_blocked(tmp_path):
    _blocked(tmp_path, work_dir(tmp_path) / "smoke-20261113-010203-abc123")


def test_work_root_unknown_is_fail_with_fixed_code(tmp_path):
    rec = Recorder()
    rc, obj = go(tmp_path, rec, env={"PATH": "x"})
    assert rc == 1 and obj["status"] == "FAIL" and obj["error"]["code"] == "WORK_ROOT_UNKNOWN"
    assert rec.calls == []


def test_work_exactly_jobs_dir_is_allowed(tmp_path):
    rec = Recorder()
    rec.work = tmp_path / "root" / "jobs"
    out = io.StringIO()
    rc = smoke.main(["--url", URL, "--work", str(tmp_path / "root" / "jobs")], out=out, run=rec,
                    dns_probe=lambda h: smoke.DNS_OK, which=which, env=env_for(tmp_path), now=NOW, rand=RAND)
    assert rc == 0 and json.loads(out.getvalue())["status"] == "ok"


# --- PY-RF2: unique job names --------------------------------------------------------------------

def test_two_runs_in_the_same_second_get_different_job_dirs(tmp_path):
    jobs = []
    for _ in range(2):
        rec = Recorder()
        rec.work = work_dir(tmp_path)
        out = io.StringIO()
        # no rand injected: the real random suffix is used; the file name depends on the job dir
        def runner(argv, env=None, timeout=None, cwd=None, rec=rec):
            if "yt_dlp" in argv:
                (Path(cwd) / "files" / "jNQXAC9IVRw.mp4").write_bytes(b"x")
            return (0, "1.0\n", "")

        rc = smoke.main(["--url", URL, "--work", str(work_dir(tmp_path))], out=out, run=runner,
                        dns_probe=lambda h: smoke.DNS_OK, which=which, env=env_for(tmp_path), now=NOW)
        obj = json.loads(out.getvalue())
        assert rc == 0
        jobs.append(obj["job_dir"])
    assert jobs[0] != jobs[1]
    assert all(os.path.basename(j).startswith("smoke-20261113-010203-") and len(os.path.basename(j)) == len("smoke-20261113-010203-") + 6 for j in jobs)


def test_existing_job_dir_is_never_reused(tmp_path):
    (work_dir(tmp_path) / STAMP).mkdir(parents=True)
    marker = work_dir(tmp_path) / STAMP / "keep.txt"
    marker.write_text("x")
    rec = Recorder()
    rc, obj = go(tmp_path, rec)
    assert rc == 1 and obj["status"] == "FAIL" and obj["reason"] == "job_exists"
    assert rec.calls == [] and marker.read_text() == "x"


def test_default_rand_is_six_chars():
    assert len(smoke.default_rand()) == 6


# --- DNS probe: global addresses only, bounded time ----------------------------------------------

def resolver_for(*addrs):
    return lambda host, port, **kw: [(2, 1, 6, "", (a, port)) for a in addrs]


@pytest.mark.parametrize(
    "addrs,expected",
    [
        (["142.250.1.1"], "DNS_OK"),
        (["142.250.1.1", "2607:f8b0:4004::200e"], "DNS_OK"),
        (["10.0.0.5"], "DNS_BLOCKED"),
        (["127.0.0.1"], "DNS_BLOCKED"),
        (["169.254.169.254"], "DNS_BLOCKED"),
        (["192.168.1.1"], "DNS_BLOCKED"),
        (["::1"], "DNS_BLOCKED"),
        (["fe80::1%12"], "DNS_BLOCKED"),
        (["::ffff:127.0.0.1"], "DNS_BLOCKED"),
        (["::ffff:10.1.2.3"], "DNS_BLOCKED"),
        (["142.250.1.1", "10.0.0.5"], "DNS_BLOCKED"),
        (["not-an-ip"], "DNS_BLOCKED"),
    ],
)
def test_dns_probe_classifies_addresses(addrs, expected):
    assert urlpolicy.dns_probe("www.youtube.com", resolver=resolver_for(*addrs)) == getattr(smoke, expected)


def test_dns_probe_empty_host_and_no_answer_and_error():
    assert urlpolicy.dns_probe("") == smoke.DNS_FAIL
    assert urlpolicy.dns_probe("h", resolver=resolver_for()) == smoke.DNS_FAIL

    def boom(host, port, **kw):
        raise OSError("nxdomain")

    assert urlpolicy.dns_probe("h", resolver=boom) == smoke.DNS_FAIL


def test_dns_probe_is_time_bounded():
    def slow(host, port, **kw):
        time.sleep(1.5)
        return [(2, 1, 6, "", ("8.8.8.8", port))]

    started = time.monotonic()
    result = urlpolicy.dns_probe("h", timeout=0.1, resolver=slow)
    assert result == smoke.DNS_FAIL and time.monotonic() - started < 1.0


# --- one source for the allowlist ----------------------------------------------------------------

def test_config_example_allowlist_matches_the_constant():
    cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert tuple(cfg["media_host_allowlist"]) == config.DEFAULT_MEDIA_HOSTS
