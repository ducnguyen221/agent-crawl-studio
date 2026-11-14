"""SPEC FR1.1, FR1.2, FR1.3, FR1.7, FR1.8: request v1 validation and the one-JSON-line command line."""

import io
import json
import logging
import re
import warnings

import pytest

from agent_crawl_studio import cli, errors, schema
from agent_crawl_studio.errors import ContractError

URL = "https://example.com/a.pdf"


def dumps(obj):
    return json.dumps(obj).encode("utf-8")


def base(**extra):
    doc = {"schema_version": 1, "urls": [URL]}
    doc.update(extra)
    return doc


# --- valid samples ----------------------------------------------------------------------------------
def test_minimal_request_is_normalised_with_defaults():
    req = schema.parse_request(dumps(base()))
    assert req.schema_version == 1
    assert req.urls == [schema.UrlSpec(URL, "auto")]
    assert (req.mode, req.output_dir, req.naming_policy, req.existing_file_policy) == ("auto", None, "id_hash", "rename")
    assert req.limits_effective["max_items"] == 100
    assert "concurrency_not_applied" in req.warnings


def test_full_request_with_object_urls_and_limits():
    req = schema.parse_request(dumps(base(
        urls=[URL, {"url": "https://example.com/p", "expect": "html"}, {"url": "https://example.com/b.zip"}],
        mode="direct", output_dir="batch-1", naming_policy="id_hash", existing_file_policy="rename",
        limits={"max_items": 5, "connect_timeout": 2.5, "max_redirects": 10})))
    assert [(u.url, u.expect) for u in req.urls] == [(URL, "auto"), ("https://example.com/p", "html"), ("https://example.com/b.zip", "auto")]
    assert req.mode == "direct" and req.output_dir == "batch-1"
    assert req.limits_effective["max_items"] == 5 and req.limits_effective["connect_timeout"] == 2.5


@pytest.mark.parametrize("mode", ["auto", "direct", "media", "gallery", "crawl"])
def test_every_documented_mode_is_valid_grammar(mode):
    assert schema.parse_request(dumps(base(mode=mode))).mode == mode


@pytest.mark.parametrize("expect", ["auto", "binary", "html"])
def test_every_expect_value_is_valid(expect):
    assert schema.parse_request(dumps(base(urls=[{"url": URL, "expect": expect}]))).urls[0].expect == expect


def test_one_hundred_urls_are_valid_and_a_utf8_url_survives():
    urls = ["https://example.com/%d" % i for i in range(99)] + ["https://example.com/é"]
    assert len(schema.parse_request(dumps(base(urls=urls))).urls) == 100


# --- invalid samples: (label, raw bytes, why, path of the object the hint points at) ----------------------
BIG = b'{"schema_version":1,"urls":["https://example.com/"],"output_dir":"' + b"a" * (1024 * 1024) + b'"}'
INVALID = [
    ("unknown top key", dumps(base(extra=1)), "unknown_key", "$"),
    ("unknown key in url item", dumps(base(urls=[URL, {"url": URL, "zzz": 1}])), "unknown_key", "$.urls[1]"),
    ("unknown key in limits", dumps(base(limits={"zzz": 1})), "unknown_key", "$.limits"),
    ("auth_profile not supported", dumps(base(auth_profile="x")), "unknown_key", "$"),
    ("crawl not supported", dumps(base(crawl={"depth": 1})), "unknown_key", "$"),
    ("schema_version 2", dumps(base(schema_version=2)), "out_of_range", "$.schema_version"),
    ("schema_version string", dumps(base(schema_version="1")), "wrong_type", "$.schema_version"),
    ("schema_version bool", b'{"schema_version":true,"urls":["https://example.com/"]}', "wrong_type", "$.schema_version"),
    ("schema_version float", b'{"schema_version":1.0,"urls":["https://example.com/"]}', "wrong_type", "$.schema_version"),
    ("missing schema_version", dumps({"urls": [URL]}), "wrong_type", "$"),
    ("missing urls", dumps({"schema_version": 1}), "wrong_type", "$"),
    ("urls empty", dumps(base(urls=[])), "out_of_range", "$.urls"),
    ("urls not a list", dumps(base(urls="https://example.com/")), "wrong_type", "$.urls"),
    ("urls over default max_items", dumps(base(urls=["https://example.com/%d" % i for i in range(101)])), "out_of_range", "$.urls"),
    ("urls over limits.max_items", dumps(base(urls=[URL, URL, URL], limits={"max_items": 2})), "out_of_range", "$.urls"),
    ("url item is a number", dumps(base(urls=[5])), "wrong_type", "$.urls[0]"),
    ("url item object without url", dumps(base(urls=[{"expect": "html"}])), "wrong_type", "$.urls[0]"),
    ("url item url is not a string", dumps(base(urls=[{"url": 5}])), "wrong_type", "$.urls[0]"),
    ("expect unknown value", dumps(base(urls=[{"url": URL, "expect": "weird"}])), "out_of_range", "$.urls[0]"),
    ("expect not a string", dumps(base(urls=[{"url": URL, "expect": 3}])), "wrong_type", "$.urls[0]"),
    ("mode unknown", dumps(base(mode="weird")), "out_of_range", "$.mode"),
    ("mode not a string", dumps(base(mode=5)), "wrong_type", "$.mode"),
    ("output_dir not a string", dumps(base(output_dir=5)), "wrong_type", "$.output_dir"),
    ("output_dir empty", dumps(base(output_dir="")), "out_of_range", "$.output_dir"),
    ("limits is a list", dumps(base(limits=[])), "wrong_type", "$.limits"),
    ("limit zero", dumps(base(limits={"max_items": 0})), "out_of_range", "$.limits.max_items"),
    ("limit over the hard cap", dumps(base(limits={"max_items": 1001})), "out_of_range", "$.limits.max_items"),
    ("limit is a bool", b'{"schema_version":1,"urls":["https://example.com/"],"limits":{"max_bytes_per_file":true}}', "wrong_type", "$.limits.max_bytes_per_file"),
    ("limit negative", dumps(base(limits={"connect_timeout": -1})), "out_of_range", "$.limits.connect_timeout"),
    ("limit fractional count", dumps(base(limits={"max_items": 1.5})), "wrong_type", "$.limits.max_items"),
    ("total bytes over cap", dumps(base(limits={"max_total_bytes": 100 * 2 ** 30 + 1})), "out_of_range", "$.limits.max_total_bytes"),
    ("retries over cap", dumps(base(limits={"retries": 6})), "out_of_range", "$.limits.retries"),
    ("concurrency over cap", dumps(base(limits={"concurrency": 9})), "out_of_range", "$.limits.concurrency"),
    ("naming_policy title", dumps(base(naming_policy="title")), "out_of_range", "$.naming_policy"),
    ("existing_file_policy skip", dumps(base(existing_file_policy="skip")), "out_of_range", "$.existing_file_policy"),
    ("existing_file_policy fail", dumps(base(existing_file_policy="fail")), "out_of_range", "$.existing_file_policy"),
    ("existing_file_policy overwrite", dumps(base(existing_file_policy="overwrite")), "out_of_range", "$.existing_file_policy"),
    ("not JSON", b"{", "wrong_type", "$"),
    ("top level list", b"[]", "wrong_type", "$"),
    ("top level null", b"null", "wrong_type", "$"),
    ("empty body", b"", "wrong_type", "$"),
    ("NaN constant", b'{"schema_version":NaN,"urls":["https://example.com/"]}', "wrong_type", "$"),
    ("Infinity in limits", b'{"schema_version":1,"urls":["https://example.com/"],"limits":{"connect_timeout":Infinity}}', "wrong_type", "$"),
    ("nested 100000 deep", b"[" * 100000 + b"]" * 100000, "wrong_type", "$"),
    ("duplicate key", b'{"schema_version":1,"schema_version":1,"urls":["https://example.com/"]}', "unknown_key", "$"),
    ("UTF-8 BOM", b"\xef\xbb\xbf" + dumps(base()), "wrong_type", "$"),
    ("not UTF-8", b"\xff\xfe{", "wrong_type", "$"),
    ("bigger than 1 MiB", BIG, "out_of_range", "$"),
]


def test_there_are_at_least_25_invalid_samples():
    assert len(INVALID) >= 25


@pytest.mark.parametrize("label,raw,why,path", INVALID, ids=[row[0] for row in INVALID])
def test_invalid_request_is_rejected_with_a_path_and_a_reason(label, raw, why, path):
    with pytest.raises(ContractError) as info:
        schema.parse_request(raw)
    assert (info.value.code, info.value.why, info.value.path) == ("INVALID_REQUEST", why, path), label


def test_a_request_of_exactly_one_mib_is_not_rejected_for_size():
    pad = 1024 * 1024 - len(b'{"schema_version":1,"urls":["https://example.com/"],"output_dir":""}')
    raw = b'{"schema_version":1,"urls":["https://example.com/"],"output_dir":"' + b"a" * pad + b'"}'
    assert len(raw) == 1024 * 1024
    assert schema.parse_request(raw).output_dir == "a" * pad


# --- the command line: one JSON line, exit code, nothing created ---------------------------------------------
def run_cli(argv, tmp_path, **kw):
    out = io.StringIO()
    env = {"CRAWL_STUDIO_WORK": str(tmp_path / "work"), "OPCOS_ROOT": str(tmp_path / "opcos")}
    rc = cli.main(argv, out=out, env=env, **kw)
    return rc, out.getvalue()


def one_object(text):
    assert text.endswith("\n") and text.count("\n") == 1
    return json.loads(text)


# RF72: the command line forgives ONE leading BOM (Windows PowerShell 5.1 writes one); schema.parse_request stays strict
CLI_INVALID = [row for row in INVALID if row[0] != "UTF-8 BOM"]


@pytest.mark.parametrize("label,raw,why,path", CLI_INVALID, ids=[row[0] for row in CLI_INVALID])
def test_cli_plan_rejects_each_invalid_sample_with_one_json_line_exit_2_and_creates_nothing(label, raw, why, path, tmp_path):
    request = tmp_path / "request.json"
    request.write_bytes(raw)
    before = sorted(p.name for p in tmp_path.iterdir())
    rc, text = run_cli(["plan", "--request", str(request)], tmp_path)
    obj = one_object(text)
    assert rc == 2 and obj["error"]["code"] == "INVALID_REQUEST"
    assert obj["error"]["message"] == errors.message_for("INVALID_REQUEST")
    assert obj["error"]["hint"] == path + " " + why
    assert sorted(p.name for p in tmp_path.iterdir()) == before and not (tmp_path / "work").exists()


def test_hint_never_copies_unknown_key_names_or_user_values(tmp_path):
    request = tmp_path / "r.json"
    request.write_bytes(dumps(base(SECRETKEY_top=1, limits={"SECRETKEY_lim": 1, "max_items": "SECRET_VALUE"})))
    rc, text = run_cli(["plan", "--request", str(request)], tmp_path)
    assert rc == 2 and "SECRET" not in text
    request.write_bytes(dumps(base(limits={"max_items": "SECRET_VALUE"})))
    assert "SECRET" not in run_cli(["plan", "--request", str(request)], tmp_path)[1]


def test_missing_request_file_is_an_invalid_request(tmp_path):
    rc, text = run_cli(["plan", "--request", str(tmp_path / "nope.json")], tmp_path)
    assert rc == 2 and one_object(text)["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("argv", [
    [], ["plan"], ["plan", "--request"], ["plan", "x"], ["run"], ["plan", "--request", "a", "b"],
    ["plan", "--request", "a", "--allow-private"], ["plan", "--allow-private", "--request", "a"],
    ["plan", "--insecure", "--request", "a"], ["plan", "--no-ssrf-check", "--request", "a"], ["bogus"],
    ["capabilities", "--x"],
])
def test_bad_command_lines_are_usage_errors_with_one_json_line(argv, tmp_path):
    rc, text = run_cli(argv, tmp_path)
    assert rc == 2 and one_object(text)["error"]["code"] == "USAGE_ERROR"


def test_run_is_wired_to_the_runner_and_is_no_longer_a_usage_error(tmp_path):
    """Slice A answered `run` with USAGE_ERROR ("not available yet"); TK16 replaces that with the real runner."""
    request = tmp_path / "r.json"
    request.write_bytes(dumps(base()))
    rc, text = run_cli(["run", "--request", str(request)], tmp_path,
                       run_fn=lambda req, env: ({"schema_version": 1, "kind": "result", "status": "succeeded"}, 0))
    obj = one_object(text)
    assert rc == 0 and obj["status"] == "succeeded" and "error" not in obj


def test_plan_result_of_the_injected_function_is_printed_as_one_line_exit_0(tmp_path):
    request = tmp_path / "r.json"
    request.write_bytes(dumps(base()))
    seen = {}

    def plan_fn(req, env):
        seen["urls"] = [u.url for u in req.urls]
        return {"schema_version": 1, "kind": "plan", "valid": True, "network_checked": False, "items": [], "warnings": []}

    rc, text = run_cli(["plan", "--request", str(request)], tmp_path, plan_fn=plan_fn)
    assert rc == 0 and one_object(text)["kind"] == "plan" and seen["urls"] == [URL]


def test_plan_result_with_an_error_exits_with_the_table_code(tmp_path):
    request = tmp_path / "r.json"
    request.write_bytes(dumps(base()))

    def plan_fn(req, env):
        return {"schema_version": 1, "kind": "plan", "valid": False, "error": errors.error_object("OUTPUT_PATH_BLOCKED")}

    rc, text = run_cli(["plan", "--request", str(request)], tmp_path, plan_fn=plan_fn)
    assert rc == 2 and one_object(text)["error"]["code"] == "OUTPUT_PATH_BLOCKED"


def test_unexpected_exception_is_internal_error_exit_1_and_prints_no_text(tmp_path):
    request = tmp_path / "r.json"
    request.write_bytes(dumps(base()))

    def plan_fn(req, env):
        raise RuntimeError("SECRET-TRACEBACK-TEXT")

    rc, text = run_cli(["plan", "--request", str(request)], tmp_path, plan_fn=plan_fn)
    obj = one_object(text)
    assert rc == 1 and obj["error"]["code"] == "INTERNAL_ERROR" and "SECRET" not in text


def test_keyboard_interrupt_during_plan_is_interrupted_exit_130(tmp_path):
    request = tmp_path / "r.json"
    request.write_bytes(dumps(base()))

    def plan_fn(req, env):
        raise KeyboardInterrupt

    rc, text = run_cli(["plan", "--request", str(request)], tmp_path, plan_fn=plan_fn)
    assert rc == 130 and one_object(text)["error"]["code"] == "INTERRUPTED"


def test_library_warnings_and_logging_never_reach_stdout(tmp_path, capsys):
    request = tmp_path / "r.json"
    request.write_bytes(dumps(base()))

    def plan_fn(req, env):
        warnings.warn("library warning text")
        logging.getLogger("noisy").warning("log text")
        print("", end="")
        return {"schema_version": 1, "kind": "plan", "valid": True, "items": [], "warnings": []}

    env = {"CRAWL_STUDIO_WORK": str(tmp_path / "work"), "OPCOS_ROOT": str(tmp_path / "opcos")}
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        rc = cli.main(["plan", "--request", str(request)], env=env, plan_fn=plan_fn)
    captured = capsys.readouterr()
    assert rc == 0 and captured.out.count("\n") == 1 and "library warning" not in captured.out and "log text" not in captured.out


def test_capabilities_still_works_and_other_subcommands_are_not_added(tmp_path):
    assert cli.COMMANDS == {"capabilities": (), "plan": ("--request",), "run": ("--request",),
                            "retry": ("--manifest", "--request")}
    rc, text = run_cli(["capabilities"], tmp_path, build=lambda *a, **k: {"schema_version": 1, "ok": True})
    assert rc == 0 and one_object(text)["ok"] is True


def test_job_id_format():
    import datetime
    job_id = schema.new_job_id(datetime.datetime(2026, 11, 13, 9, 5, 7), "a1b2c3")
    assert job_id == "20261113-090507-a1b2c3" and re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", job_id)
    assert re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", schema.new_job_id(datetime.datetime(2026, 1, 2, 3, 4, 5)))


def test_run_with_an_invalid_request_is_rejected_like_plan(tmp_path):
    request = tmp_path / "r.json"
    request.write_bytes(dumps(base(extra=1)))
    rc, text = run_cli(["run", "--request", str(request)], tmp_path)
    obj = one_object(text)
    assert rc == 2 and obj["error"]["code"] == "INVALID_REQUEST" and obj["job_id"] is None and obj["status"] == "failed"


def test_parse_request_refuses_a_non_bytes_argument():
    with pytest.raises(ContractError) as info:
        schema.parse_request("{}")
    assert (info.value.path, info.value.why) == ("$", "wrong_type")


# --- review round: RF40, RF41, RF42 ----------------------------------------------------------------------------
def test_output_is_one_line_even_with_line_separators_and_lone_surrogates(tmp_path):
    request = tmp_path / "r.json"
    request.write_bytes(dumps(base()))

    def plan_fn(req, env):
        return {"kind": "plan", "valid": True, "note": "a b\x85c d\x0be\ud800f\x1cg"}

    rc, text = run_cli(["plan", "--request", str(request)], tmp_path, plan_fn=plan_fn)
    assert rc == 0 and text.count("\n") == 1 and len(text.splitlines()) == 1 and text.isascii()
    assert json.loads(text)["note"] == "a b\x85c d\x0be\ud800f\x1cg"


def test_no_socket_records_every_call_even_when_the_code_swallows_the_exception(no_socket):
    import socket
    try:
        socket.getaddrinfo("example.com", 80)
    except Exception:  # noqa: BLE001 - library code does exactly this
        pass
    assert [call[0] for call in no_socket] == ["getaddrinfo"]
    no_socket.clear()


def test_the_command_line_is_derived_from_COMMANDS(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "COMMANDS", {"capabilities": (), "plan": ("--request", "--other"), "run": ("--request",)})
    request = tmp_path / "r.json"
    request.write_bytes(dumps(base(extra=1)))
    rc, text = run_cli(["plan", "--request", str(request), "--other", "x"], tmp_path)
    assert one_object(text)["error"]["code"] == "INVALID_REQUEST"
    rc, text = run_cli(["plan", "--request", str(request)], tmp_path)
    assert one_object(text)["error"]["code"] == "USAGE_ERROR"
