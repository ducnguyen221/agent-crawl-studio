"""FR3.2, FR3.3, FR3.4: `capabilities` prints exactly one JSON line, reports missing dependencies
without installing anything, and keeps error text out of stdout unless it comes from the fixed table."""

import io
import json
import os
import sys
import time

import pytest

from agent_crawl_studio import capabilities, cli, errors

FFMPEG = r"C:\tools\ffmpeg\bin\ffmpeg.exe"
NODE = r"C:\Program Files\nodejs\node.exe"
VERSIONS = {"yt-dlp": "2026.10.26", "httpx": "0.28.1"}


def fake_which(table):
    def which(cmd, path=None):
        return table.get(cmd)

    return which


def fake_run(table):
    def run(argv, env=None, timeout=None, cwd=None):
        key = os.path.basename(argv[0]).lower()
        if key in table:
            return table[key]
        return (127, "", "not found")

    return run


def version_of(name):
    if name in VERSIONS:
        return VERSIONS[name]
    raise LookupError(name)


FFMPEG_OUT = "ffmpeg version 9.0.2-essentials_build-www.gyan.dev Copyright (c) 2000-2025\n"
OK_RUN = fake_run({"ffmpeg.exe": (0, FFMPEG_OUT, ""), "node.exe": (0, "v24.12.0\n", "")})
OK_WHICH = fake_which({"ffmpeg": FFMPEG, "node": NODE})
ENV = {"OPCOS_ROOT": r"C:\o", "CRAWL_STUDIO_WORK": r"C:\w"}


def test_all_ok():
    cap = capabilities.build_capabilities(ENV, OK_WHICH, OK_RUN, version_of=version_of)
    assert cap["schema_version"] == 1
    assert cap["engines"]["yt-dlp"] == {"version": "2026.10.26", "status": "ok"}
    assert cap["engines"]["httpx"] == {"version": "0.28.1", "status": "ok"}
    assert cap["ffmpeg"] == {"path": FFMPEG, "version": "9.0.2", "status": "ok"}
    assert cap["node"] == {"path": NODE, "version": "24.12.0", "status": "ok"}
    assert cap["work_root"] == r"C:\w"
    assert "error" not in cap


def test_ffmpeg_dir_env_is_looked_up_before_path():
    seen = []

    def which(cmd, path=None):
        seen.append((cmd, path))
        return {"ffmpeg": FFMPEG, "node": NODE}.get(cmd) if (cmd != "ffmpeg" or path) else None

    env = dict(ENV, FFMPEG_DIR=r"C:\tools\ffmpeg\bin")
    cap = capabilities.build_capabilities(env, which, OK_RUN, version_of=version_of)
    assert ("ffmpeg", r"C:\tools\ffmpeg\bin") in seen
    assert cap["ffmpeg"]["status"] == "ok"


def test_ffmpeg_dir_miss_falls_back_to_path():
    def which(cmd, path=None):
        if cmd == "ffmpeg":
            return None if path else FFMPEG
        return NODE

    env = dict(ENV, FFMPEG_DIR=r"C:\nope")
    cap = capabilities.build_capabilities(env, which, OK_RUN, version_of=version_of)
    assert cap["ffmpeg"]["path"] == FFMPEG


def test_work_root_default_uses_opcos_root():
    cap = capabilities.build_capabilities({"OPCOS_ROOT": r"C:\o"}, OK_WHICH, OK_RUN, version_of=version_of)
    assert cap["work_root"] == os.path.join(r"C:\o", "workspaces", "agent-crawl-studio")


def test_work_root_default_uses_userprofile_when_no_opcos_root():
    cap = capabilities.build_capabilities({"USERPROFILE": r"C:\u"}, OK_WHICH, OK_RUN, version_of=version_of)
    assert cap["work_root"] == os.path.join(r"C:\u", "OpcOS", "workspaces", "agent-crawl-studio")


def test_missing_ffmpeg_reports_dependency_missing_with_hint():
    cap = capabilities.build_capabilities(ENV, fake_which({"node": NODE}), OK_RUN, version_of=version_of)
    assert cap["ffmpeg"]["status"] == "missing"
    assert cap["error"]["code"] == "DEPENDENCY_MISSING"
    assert cap["error"]["hint"] == capabilities.HINT_FFMPEG
    assert cap["engines"]["yt-dlp"]["status"] == "ok"


def test_missing_node_and_engine():
    def vo(name):
        raise LookupError(name)

    cap = capabilities.build_capabilities(ENV, fake_which({"ffmpeg": FFMPEG}), OK_RUN, version_of=vo)
    assert cap["node"]["status"] == "missing"
    assert cap["engines"]["yt-dlp"]["status"] == "missing"
    assert cap["engines"]["httpx"]["status"] == "missing"
    assert cap["error"]["code"] == "DEPENDENCY_MISSING"
    assert cap["error"]["hint"] == "; ".join([capabilities.HINT_ENGINE, capabilities.HINT_NODE])
    assert capabilities.HINT_ENGINE == "run: opcos-runtime ensure --repo-dir plugins/agent-crawl-studio"


def test_version_probe_failure_is_missing_not_crash():
    run = fake_run({"ffmpeg.exe": (1, "", "boom secret"), "node.exe": (0, "v24.12.0", "")})
    cap = capabilities.build_capabilities(ENV, OK_WHICH, run, version_of=version_of)
    assert cap["ffmpeg"]["status"] == "missing"
    assert "boom secret" not in json.dumps(cap)


def test_unparseable_version_is_missing():
    run = fake_run({"ffmpeg.exe": (0, "garbage", ""), "node.exe": (0, "also garbage", "")})
    cap = capabilities.build_capabilities(ENV, OK_WHICH, run, version_of=version_of)
    assert cap["ffmpeg"]["status"] == "missing" and cap["node"]["status"] == "missing"


def test_run_raising_oserror_is_missing():
    def run(argv, env=None, timeout=None, cwd=None):
        raise OSError("no such file")

    cap = capabilities.build_capabilities(ENV, OK_WHICH, run, version_of=version_of)
    assert cap["ffmpeg"]["status"] == "missing" and cap["node"]["status"] == "missing"


def test_system_run_real_subprocess():
    rc, out, err = capabilities.system_run([sys.executable, "-c", "print('hi')"])
    assert rc == 0 and out.strip() == "hi"


def test_system_run_missing_binary_returns_nonzero():
    rc, _, _ = capabilities.system_run([r"C:\definitely\not\here.exe"])
    assert rc != 0


def test_default_version_of_handles_unknown_package():
    with pytest.raises(LookupError):
        capabilities.default_version_of("definitely-not-installed-pkg-xyz")


# --- CLI: stdout is exactly one JSON line --------------------------------------------------------

def run_cli(argv, **kw):
    out = io.StringIO()
    rc = cli.main(argv, out=out, **kw)
    return rc, out.getvalue()


def test_cli_capabilities_one_json_line_exit_0():
    rc, text = run_cli(["capabilities"], env=ENV, which=OK_WHICH, run=OK_RUN, version_of=version_of)
    assert rc == 0
    assert text.endswith("\n") and text.count("\n") == 1
    assert json.loads(text)["engines"]["yt-dlp"]["version"] == "2026.10.26"


def test_cli_missing_dependency_still_one_line_exit_0():
    rc, text = run_cli(["capabilities"], env=ENV, which=fake_which({}), run=OK_RUN, version_of=version_of)
    assert rc == 0
    assert text.count("\n") == 1
    assert json.loads(text)["error"]["code"] == "DEPENDENCY_MISSING"


def test_cli_unexpected_error_uses_fixed_table_and_nonzero():
    def boom(*a, **k):
        raise RuntimeError("SECRET raw stderr from engine")

    rc, text = run_cli(["capabilities"], env=ENV, which=OK_WHICH, run=OK_RUN, version_of=version_of, build=boom)
    assert rc != 0
    assert text.count("\n") == 1
    obj = json.loads(text)
    assert obj["error"]["code"] == "INTERNAL_ERROR"
    assert obj["error"]["message"] == errors.message_for("INTERNAL_ERROR")
    assert "SECRET" not in text and "RuntimeError" not in text


def test_cli_keyboard_interrupt_exits_130():
    def stop(*a, **k):
        raise KeyboardInterrupt

    rc, text = run_cli(["capabilities"], env=ENV, which=OK_WHICH, run=OK_RUN, version_of=version_of, build=stop)
    assert rc == 130 and json.loads(text)["error"]["code"] == "INTERRUPTED"


def test_cli_unknown_subcommand_is_usage_error():
    rc, text = run_cli(["nope"])
    assert rc == 2 and json.loads(text)["error"]["code"] == "USAGE_ERROR"


def test_cli_no_subcommand_is_usage_error():
    rc, text = run_cli([])
    assert rc == 2 and json.loads(text)["error"]["code"] == "USAGE_ERROR"


def test_cli_extra_args_rejected():
    rc, text = run_cli(["capabilities", "--cookies-from-browser", "chrome"])
    assert rc == 2 and json.loads(text)["error"]["code"] == "USAGE_ERROR"


def test_cli_default_dependencies_run_for_real(monkeypatch):
    # Real defaults (no injection): must still print exactly one JSON line, whatever the machine has.
    rc, text = run_cli(["capabilities"])
    assert rc == 0 and text.count("\n") == 1
    assert json.loads(text)["schema_version"] == 1


def test_main_module_entry(monkeypatch, capsys):
    from agent_crawl_studio import __main__ as entry

    monkeypatch.setattr(sys, "argv", ["crawl-studio", "nope"])
    with pytest.raises(SystemExit) as exc:
        entry.run()
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "USAGE_ERROR"


# --- errors table ----------------------------------------------------------------------------------

def test_error_table_has_core_codes():
    for code in ("DEPENDENCY_MISSING", "INTERNAL_ERROR", "USAGE_ERROR", "URL_BLOCKED", "OUTPUT_PATH_BLOCKED", "WORK_ROOT_UNKNOWN", "INTERRUPTED"):
        assert errors.message_for(code)
    obj = errors.error_object("DEPENDENCY_MISSING", hint="run ensure")
    assert obj == {"code": "DEPENDENCY_MISSING", "message": errors.message_for("DEPENDENCY_MISSING"), "hint": "run ensure"}


def test_unknown_code_maps_to_internal_error():
    assert errors.error_object("NOT_A_CODE")["code"] == "INTERNAL_ERROR"
