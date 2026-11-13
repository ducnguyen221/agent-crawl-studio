"""Review of round 3 (RF90, RF92, RF93): a password fragment in `redact`, a path-like exception class name on
stderr, and warnings that must not flood stderr."""

import json
import warnings

import pytest

from agent_crawl_studio import cli
from agent_crawl_studio.urlpolicy import redact

GOOD = json.dumps({"schema_version": 1, "urls": ["https://example.com/a.pdf"], "output_dir": "out"}).encode("utf-8")


@pytest.mark.parametrize("raw", [
    "https://u:p@ss/w#rd@example.com/x",
    "https://u:p@ss/w@example.com/x",
    "https://u@ss/w#rd@example.com/x",
    "https://u:p@ss/w?q=1@example.com/x",
])
def test_redact_prints_no_piece_of_a_password_that_holds_an_at_sign_and_a_delimiter(raw):
    """RF90: the part of the password after its own "@" used to come out as if it were the host."""
    assert redact(raw) == "https://REDACTED/"


def test_redact_keeps_what_is_not_a_userinfo():
    assert redact("https://example.com/a@b") == "https://example.com/a@b"
    assert redact("https://u:p@ss@example.com/x") == "https://example.com/x"


def run(tmp_path, plan_fn):
    request = tmp_path / "r.json"
    request.write_bytes(GOOD)
    return cli.main(["plan", "--request", str(request)], env={"CRAWL_STUDIO_WORK": str(tmp_path / "w")}, plan_fn=plan_fn)


@pytest.mark.parametrize("name", ["C:\\Users\\secret\\path", "a b", "x" * 65, "caf\u00e9", ""])
def test_the_class_name_on_stderr_is_only_plain_identifier_characters(tmp_path, capsys, name):
    """RF92: a custom exception class may carry any text as its __name__."""
    boom = type(name or "Boom", (Exception,), {})
    boom.__name__ = name

    def plan_fn(req, env):
        raise boom("SECRET")

    assert run(tmp_path, plan_fn) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "internal error: unknown" and "secret" not in captured.err.lower()
    assert captured.out.count("\n") == 1


def test_a_normal_class_name_still_shows(tmp_path, capsys):
    def plan_fn(req, env):
        raise ValueError("SECRET")

    run(tmp_path, plan_fn)
    assert capsys.readouterr().err.strip() == "internal error: ValueError"


def test_a_warning_repeated_a_thousand_times_prints_once_even_under_W_error(tmp_path, capsys):
    """RF93: the filter was "always", so a library warning inside a loop flooded stderr."""
    def plan_fn(req, env):
        for _ in range(1000):
            warnings.warn("loud library warning")
        return {"schema_version": 1, "kind": "plan", "valid": True, "items": [], "warnings": []}

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert run(tmp_path, plan_fn) == 0
    captured = capsys.readouterr()
    assert captured.err.count("UserWarning") == 1
    assert captured.out.count("\n") == 1
