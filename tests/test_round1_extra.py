"""Round 1 review fixes that do not belong to one existing test module:
PY-RF1 (system_run timeout), PY-RF7 (work root), SE-RF3 (flag allowlist), SE-RF5/6 (probe env, tool paths)."""

import os
import sys
import time

import pytest

from agent_crawl_studio import capabilities, flags

FFMPEG = r"C:\tools\ffmpeg\bin\ffmpeg.exe"
NODE = r"C:\Program Files\nodejs\node.exe"
VERSIONS = {"yt-dlp": "2026.10.26", "httpx": "0.28.1", "trafilatura": "2.3.1"}
ENV = {"OPCOS_ROOT": r"C:\o", "CRAWL_STUDIO_WORK": r"C:\w"}


def version_of(name):
    return VERSIONS[name]


def ok_run(argv, env=None, timeout=None, cwd=None):
    if "ffmpeg" in os.path.basename(argv[0]).lower():
        return (0, "ffmpeg version 9.0.2-x\n", "")
    return (0, "v24.12.0\n", "")


def which_of(table):
    return lambda cmd, path=None: table.get(cmd)


OK_WHICH = which_of({"ffmpeg": FFMPEG, "node": NODE})


# --- PY-RF1 -------------------------------------------------------------------------------------

def test_system_run_timeout_is_reported_not_swallowed_as_missing_binary():
    started = time.monotonic()
    rc, out, err = capabilities.system_run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    assert rc == capabilities.TIMEOUT_RC and rc != 127
    assert time.monotonic() - started < 10


def test_system_run_default_timeout_is_short_and_cwd_is_honoured(tmp_path):
    assert capabilities.PROBE_TIMEOUT <= 30
    rc, out, _ = capabilities.system_run([sys.executable, "-c", "import os; print(os.getcwd())"], cwd=str(tmp_path))
    assert rc == 0 and os.path.samefile(out.strip(), str(tmp_path))


def test_system_run_long_timeout_lets_a_slow_command_finish():
    rc, out, _ = capabilities.system_run([sys.executable, "-c", "import time; time.sleep(0.3); print('done')"], timeout=20)
    assert rc == 0 and out.strip() == "done"


# --- SE-RF5: probes use a rebuilt environment and a short timeout -------------------------------

def test_probes_run_with_clean_env_and_short_timeout():
    calls = []

    def run(argv, env=None, timeout=None, cwd=None):
        calls.append((argv, env, timeout))
        return ok_run(argv)

    base = dict(ENV, ANTHROPIC_API_KEY="leak", SystemRoot=r"C:\Windows")
    capabilities.build_capabilities(base, OK_WHICH, run, version_of=version_of)
    assert len(calls) == 2
    for argv, env, timeout in calls:
        assert env is not None and "ANTHROPIC_API_KEY" not in env and "OPCOS_ROOT" not in env
        assert timeout == capabilities.PROBE_TIMEOUT


def test_probe_timeout_is_missing_not_crash():
    def run(argv, env=None, timeout=None, cwd=None):
        if "ffmpeg" in os.path.basename(argv[0]).lower():
            return (capabilities.TIMEOUT_RC, "", "")
        return ok_run(argv)

    cap = capabilities.build_capabilities(ENV, OK_WHICH, run, version_of=version_of)
    assert cap["ffmpeg"]["status"] == "missing" and cap["node"]["status"] == "ok"


# --- SE-RF6: no relative (cwd) tool path; PATH is passed explicitly -----------------------------

def test_relative_path_from_which_is_not_trusted():
    which = which_of({"ffmpeg": "ffmpeg.exe", "node": r".\node.exe"})
    cap = capabilities.build_capabilities(ENV, which, ok_run, version_of=version_of)
    assert cap["ffmpeg"]["status"] == "missing" and cap["node"]["status"] == "missing"


def test_path_from_env_is_passed_explicitly_to_which():
    seen = []

    def which(cmd, path=None):
        seen.append((cmd, path))
        return {"ffmpeg": FFMPEG, "node": NODE}.get(cmd)

    capabilities.build_capabilities(dict(ENV, PATH=r"C:\p"), which, ok_run, version_of=version_of)
    assert ("node", r"C:\p") in seen and ("ffmpeg", r"C:\p") in seen


# --- PY-RF7 -------------------------------------------------------------------------------------

def test_work_root_unknown_is_clear_error_not_a_relative_path():
    with pytest.raises(capabilities.WorkRootError):
        capabilities.work_root({})
    with pytest.raises(capabilities.WorkRootError):
        capabilities.work_root({"CRAWL_STUDIO_WORK": "relative\\dir"})
    with pytest.raises(capabilities.WorkRootError):
        capabilities.work_root({"OPCOS_ROOT": "relative"})
    cap = capabilities.build_capabilities({}, OK_WHICH, ok_run, version_of=version_of)
    assert cap["work_root"] is None and cap["error"]["code"] == "WORK_ROOT_UNKNOWN"
    assert os.path.isabs(capabilities.work_root({"USERPROFILE": r"C:\u"}))


def test_work_root_error_is_secondary_to_missing_dependency_hint():
    cap = capabilities.build_capabilities({}, which_of({}), ok_run, version_of=version_of)
    assert cap["work_root"] is None
    assert cap["error"]["code"] == "DEPENDENCY_MISSING"


# --- SE-RF3: validate_flags is an allowlist -----------------------------------------------------

FIXED = flags.fixed_ytdlp_flags(r"C:\tools\ffmpeg\bin", r"C:\w\jobs\smoke-1", "50M", 20, NODE)


@pytest.mark.parametrize(
    "bad",
    [
        ["--exec", "calc"],
        ["--exec=calc"],
        ["--cookies", "c.txt"],
        ["--cookies-from", "chrome"],
        ["--no-play"],
        ["--config-locations", "x"],
        ["--plugin-dirs", "x"],
        ["--proxy-x", "y"],
        ["-P", "x"],
        ["-x"],
        ["-oops"],
        ["--paths=home:C:\\x"],
        ["--format", "best"],
        ["--max-downloads", "1"],
        ["--print=%(url)s"],
    ],
)
def test_validate_flags_is_an_allowlist(bad):
    with pytest.raises(ValueError):
        flags.validate_flags(FIXED + bad)


def test_allowlist_is_exactly_the_flags_of_the_fixed_set():
    heads = {item for item in FIXED if item.startswith("-")}
    assert set(flags.ALLOWED_FLAGS) == heads
    flags.validate_flags(FIXED)


# --- regression: shutil.which takes `mode` as 2nd positional, so the dir must be passed by keyword ---

def test_ffmpeg_dir_works_with_the_real_shutil_which(tmp_path):
    import shutil

    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"")
    found = capabilities.locate_ffmpeg({"FFMPEG_DIR": str(tmp_path), "PATH": ""}, shutil.which)
    assert found is not None and os.path.samefile(found, str(fake))
    assert capabilities.locate_ffprobe(str(fake), shutil.which) is None
