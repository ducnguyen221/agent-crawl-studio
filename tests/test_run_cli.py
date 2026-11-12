"""`run --request` wired into the command line (SPEC FR1.5, FR1.7, FR1.8; PLAN TK16). RED until `cli.main` calls
`runner.run`. `main(..., run_fn=None)`: `run_fn(request, env) -> (result, exit code)`, default `runner.run`."""

import io
import json

import pytest

from agent_crawl_studio import cli, schema

BLOCKED = ["http://127.0.0.1/a", "http://2130706433/b", "https://localhost/c", "ftp://files.example.com/d"]


def request_file(tmp_path, urls=BLOCKED, **extra):
    path = tmp_path / "request.json"
    path.write_bytes(json.dumps(dict({"schema_version": 1, "urls": urls}, **extra)).encode("utf-8"))
    return str(path)


def run_main(tmp_path, **kw):
    out = io.StringIO()
    env = {"CRAWL_STUDIO_WORK": str(tmp_path / "work"), "OPCOS_ROOT": str(tmp_path / "opcos")}
    rc = cli.main(["run", "--request", request_file(tmp_path)], out=out, env=env, **kw)
    return rc, out.getvalue()


def one_object(text):
    assert text.endswith("\n") and text.count("\n") == 1
    return json.loads(text)


@pytest.mark.parametrize("status, code", [("succeeded", 0), ("failed", 1), ("partial", 3), ("cancelled", 130)])
def test_run_prints_the_result_of_the_runner_as_one_line_and_returns_its_exit_code(tmp_path, status, code):
    seen = {}

    def run_fn(request, env):
        seen["request"], seen["env"] = request, env
        return {"schema_version": 1, "kind": "result", "status": status, "items": []}, code
    rc, text = run_main(tmp_path, run_fn=run_fn)
    assert rc == code and one_object(text)["status"] == status
    assert isinstance(seen["request"], schema.Request) and seen["request"].urls[0].url == BLOCKED[0]
    assert seen["env"]["CRAWL_STUDIO_WORK"].endswith("work")


def test_run_of_a_batch_that_is_all_blocked_is_failed_exit_1_without_any_socket(tmp_path, no_socket):
    rc, text = run_main(tmp_path)
    obj = one_object(text)
    assert rc == 1 and obj["kind"] == "result" and obj["status"] == "failed" and obj["job_id"]
    assert [i["status"] for i in obj["items"]] == ["blocked"] * 4
    assert (tmp_path / "work" / "jobs" / obj["job_id"] / "manifest.json").exists()


def test_a_ctrl_c_that_escapes_the_runner_is_interrupted_exit_130(tmp_path):
    def run_fn(request, env):
        raise KeyboardInterrupt
    rc, text = run_main(tmp_path, run_fn=run_fn)
    assert rc == 130 and one_object(text)["error"]["code"] == "INTERRUPTED"


def test_an_unexpected_failure_in_run_is_internal_error_and_only_the_class_name_is_shown(tmp_path, capsys):
    def run_fn(request, env):
        raise RuntimeError("SECRET")
    rc, text = run_main(tmp_path, run_fn=run_fn)
    captured = capsys.readouterr()
    assert rc == 1 and one_object(text)["error"]["code"] == "INTERNAL_ERROR"
    assert "RuntimeError" in captured.err and "SECRET" not in captured.err + text


def test_run_output_never_carries_library_noise(tmp_path, capsys):
    import warnings

    def run_fn(request, env):
        warnings.warn("library chatter")
        print("stray print")
        return {"schema_version": 1, "kind": "result", "status": "succeeded"}, 0
    rc, text = run_main(tmp_path, run_fn=run_fn)
    captured = capsys.readouterr()
    assert rc == 0 and one_object(text)["status"] == "succeeded"
    assert "stray print" in captured.err and "library chatter" in captured.err and captured.out == ""
