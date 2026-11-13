"""Review round 3 (RF70-RF80): request paths that must never be opened (UNC, device names), BOM, class name
of an internal error on stderr, mutant killers, query parameter names, `limits: null`."""

import io
import json
import warnings

import pytest

from agent_crawl_studio import cli, errors, runner, schema, urlpolicy

URL = "https://example.com/a.pdf"
GOOD = json.dumps({"schema_version": 1, "urls": [URL]}).encode("utf-8")
BOM = b"\xef\xbb\xbf"

NEVER_OPENED = [
    # UNC (RF70): opening one would talk SMB and leak an NTLM hash
    "\\\\server\\share\\r.json", "//server/share/r.json", "\\\\localhost\\c$\\r.json", "\\/server/share/r.json", "/\\server\\share\\r.json",
    # extended and device prefixes
    "\\\\?\\C:\\x\\r.json", "\\\\.\\CON", "//?/C:/x", "//./CON",
    # device names and streams (RF71)
    "CON", "NUL", "con.txt", "AUX", "COM1", "C:\\x\\PRN", "x.txt:stream", "CON.",
]


def run_main(argv, tmp_path, **kw):
    out = io.StringIO()
    rc = cli.main(argv, out=out, env={"CRAWL_STUDIO_WORK": str(tmp_path / "w")}, **kw)
    return rc, out.getvalue()


@pytest.mark.parametrize("path", NEVER_OPENED, ids=ascii)
def test_these_request_paths_are_refused_and_open_is_never_called(path, tmp_path, monkeypatch):
    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        raise OSError("opened")

    monkeypatch.setattr(cli, "open", spy, raising=False)
    rc, text = run_main(["plan", "--request", path], tmp_path)
    assert calls == [], "the path was opened"
    assert rc == 2 and json.loads(text)["error"]["code"] == "INVALID_REQUEST" and text.count("\n") == 1
    calls.clear()
    rc, text = run_main(["run", "--request", path], tmp_path)
    assert calls == [] and rc == 2


def test_an_ordinary_path_IS_opened_and_a_failure_then_is_told_apart(tmp_path, monkeypatch):
    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        raise OSError("opened and failed")

    monkeypatch.setattr(cli, "open", spy, raising=False)
    rc, text = run_main(["plan", "--request", str(tmp_path / "r.json")], tmp_path)
    assert calls == [(str(tmp_path / "r.json"), "rb")], "an ordinary path must reach open()"
    assert rc == 2 and json.loads(text)["error"]["code"] == "INVALID_REQUEST"


def test_a_relative_and_a_forward_slash_path_are_ordinary(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "open", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(OSError("x")), raising=False)
    for path in ("..\\r.json", "sub/r.json", "C:/x/r.json", "r.json"):
        run_main(["plan", "--request", path], tmp_path)
    assert [c[0] for c in calls] == ["..\\r.json", "sub/r.json", "C:/x/r.json", "r.json"]


# --- RF72: a UTF-8 byte order mark (Windows PowerShell 5.1 writes one) -----------------------------------------------
def test_a_request_file_with_a_bom_is_accepted(tmp_path):
    request = tmp_path / "r.json"
    request.write_bytes(BOM + GOOD)
    seen = []

    def plan_fn(req, env):
        seen.append(req.urls[0].url)
        return {"schema_version": 1, "kind": "plan", "valid": True, "items": [], "warnings": []}

    rc, text = run_main(["plan", "--request", str(request)], tmp_path, plan_fn=plan_fn)
    assert rc == 0 and seen == [URL]


def test_only_one_bom_is_forgiven_and_the_schema_itself_stays_strict(tmp_path):
    request = tmp_path / "r.json"
    request.write_bytes(BOM + BOM + GOOD)
    rc, text = run_main(["plan", "--request", str(request)], tmp_path)
    assert rc == 2 and json.loads(text)["error"]["hint"] == "$ wrong_type"
    with pytest.raises(errors.ContractError):
        schema.parse_request(BOM + GOOD)


def test_the_size_limit_counts_the_bytes_of_the_file_with_its_bom(tmp_path):
    request = tmp_path / "r.json"
    pad = schema.MAX_REQUEST_BYTES - len(BOM) - len(b'{"schema_version":1,"urls":["https://example.com/"],"output_dir":""}')
    ok = BOM + b'{"schema_version":1,"urls":["https://example.com/"],"output_dir":"' + b"a" * pad + b'"}'
    assert len(ok) == schema.MAX_REQUEST_BYTES
    request.write_bytes(ok)
    rc, text = run_main(["plan", "--request", str(request)], tmp_path, plan_fn=lambda r, e: {"kind": "plan", "valid": True})
    assert rc == 0
    request.write_bytes(ok + b"a")
    rc, text = run_main(["plan", "--request", str(request)], tmp_path, plan_fn=lambda r, e: {"kind": "plan", "valid": True})
    assert rc == 2 and json.loads(text)["error"]["hint"] == "$ out_of_range"


# --- RF73: the class name (never the text) of an internal error goes to stderr -----------------------------------------
def test_internal_error_prints_the_exception_class_name_on_stderr_only(tmp_path, capsys):
    request = tmp_path / "r.json"
    request.write_bytes(GOOD)

    def plan_fn(req, env):
        raise RuntimeError("SECRET-TEXT")

    rc = cli.main(["plan", "--request", str(request)], env={"CRAWL_STUDIO_WORK": str(tmp_path / "w")}, plan_fn=plan_fn)
    captured = capsys.readouterr()
    assert rc == 1 and "RuntimeError" in captured.err and "SECRET" not in captured.err + captured.out
    assert captured.out.count("\n") == 1


def test_internal_error_of_capabilities_names_the_class_on_stderr(capsys):
    def build(*args, **kwargs):
        raise KeyError("SECRET-KEY")

    rc = cli.main(["capabilities"], env={}, build=build)
    captured = capsys.readouterr()
    assert rc == 1 and "KeyError" in captured.err and "SECRET" not in captured.err + captured.out


# --- RF80 and RF75: warnings go to stderr, even under -W error ---------------------------------------------------------
def test_library_warnings_become_stderr_text_not_exceptions_even_under_W_error(tmp_path, capsys):
    request = tmp_path / "r.json"
    request.write_bytes(GOOD)

    def plan_fn(req, env):
        warnings.warn("library warning text")
        return {"schema_version": 1, "kind": "plan", "valid": True, "items": [], "warnings": []}

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        rc = cli.main(["plan", "--request", str(request)], env={"CRAWL_STUDIO_WORK": str(tmp_path / "w")}, plan_fn=plan_fn)
    captured = capsys.readouterr()
    assert rc == 0 and "UserWarning" in captured.err and "library warning text" not in captured.err + captured.out  # FR16.6: the class, not the text
    assert captured.out.count("\n") == 1


def test_the_warning_filter_is_restored_after_main(tmp_path):
    before = list(warnings.filters)
    run_main(["plan", "--request", str(tmp_path / "missing.json")], tmp_path)
    assert list(warnings.filters) == before


# --- RF75: mutant killers --------------------------------------------------------------------------------------------------
def test_validate_plan_names_a_missing_decision_and_every_missing_item_key():
    good = {"schema_version": 1, "kind": "plan", "valid": True, "network_checked": False, "output": {}, "limits_effective": {},
            "warnings": [], "items": [{"id": "0001", "url_redacted": "x", "url_sha256": "y", "adapter": None, "decision": "allow"}]}
    assert schema.validate_plan(good) == []
    item = dict(good["items"][0])
    del item["decision"]
    assert schema.validate_plan(dict(good, items=[item])) == ["items[0].decision"]
    assert schema.validate_plan(dict(good, items=[{}])) == ["items[0].%s" % k for k in schema.PLAN_ITEM_KEYS]


def test_the_hint_of_a_blocked_output_dir_is_the_reason_code(tmp_path):
    from agent_crawl_studio import config  # noqa: F401
    request = schema.parse_request(json.dumps({"schema_version": 1, "urls": [URL], "output_dir": "CON"}).encode("utf-8"))
    fakes = dict(attrs_of=lambda p: None, drive_type_of=lambda p: 3, sync_root_of=lambda p: False, free_bytes_of=lambda p: 10 ** 15)
    result = runner.plan(request, {"CRAWL_STUDIO_WORK": str(tmp_path / "w")}, fakes)
    assert result["error"]["hint"] == "device" and result["output"]["error"]["hint"] == "device"


# --- RF76: query parameter names ---------------------------------------------------------------------------------------------
def test_redact_keeps_short_plain_parameter_names_and_hides_odd_or_long_ones():
    assert urlpolicy.redact("https://a.com/p?page=2&utm_source=x&a.b-c=1") == "https://a.com/p?page=REDACTED&utm_source=REDACTED&a.b-c=REDACTED"
    out = urlpolicy.redact("https://a.com/p?" + "s" * 33 + "=1&tok%20en=2&caf\u00e9=3&k=v")
    assert out == "https://a.com/p?REDACTED&REDACTED&REDACTED&k=REDACTED"
    assert urlpolicy.redact("https://a.com/p?" + "s" * 32 + "=1") == "https://a.com/p?" + "s" * 32 + "=REDACTED"


def test_a_path_parameter_is_a_documented_limit_not_hidden():
    assert "jsessionid=ABC" in urlpolicy.redact("https://a.com/p;jsessionid=ABC/x")
    assert "documented limit" in urlpolicy.redact.__doc__ or "path" in urlpolicy.redact.__doc__.lower()


# --- RF78: limits null ------------------------------------------------------------------------------------------------------------
def test_limits_null_is_refused_not_silently_accepted():
    with pytest.raises(errors.ContractError) as info:
        schema.parse_request(b'{"schema_version": 1, "urls": ["https://a.com/"], "limits": null}')
    assert (info.value.path, info.value.why) == ("$.limits", "wrong_type")
