"""SPEC FR1.4 and TK9: `plan` opens no socket, writes nothing, and reports every item and the output place.
Uses runner.plan (planner) and outroot.resolve_output (never config.output_roots directly)."""

import io
import json
import os

import pytest

from agent_crawl_studio import cli, config, errors, limits, runner, schema, urlpolicy

PDF = "https://www.rfc-editor.org/rfc/rfc9309.pdf"
PNG = "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png"
NOT_FOUND = "https://www.rfc-editor.org/rfc/rfc99999.txt"
BLOCKED_URLS = ["http://127.0.0.1/", "http://2130706433/", "https://localhost/", "ftp://example.com/x", "https://user:pw@example.com/"]
GiB = 2 ** 30

FAKES = dict(
    attrs_of=lambda path: None, drive_type_of=lambda path: 3, sync_root_of=lambda path: False,
    free_bytes_of=lambda path: 10 ** 15,
)


@pytest.fixture
def world(tmp_path):
    w = {"work": str(tmp_path / "w"), "opcos": str(tmp_path / "opcos"), "home": str(tmp_path / "home")}
    w["env"] = {"CRAWL_STUDIO_WORK": w["work"], "OPCOS_ROOT": w["opcos"], "USERPROFILE": w["home"]}
    return w


def request_of(urls, **extra):
    doc = {"schema_version": 1, "urls": urls}
    doc.update(extra)
    return schema.parse_request(json.dumps(doc).encode("utf-8"))


def tree(root):
    found = []
    for base, dirs, files in os.walk(str(root)):
        found += [os.path.join(base, name) for name in dirs + files]
    return sorted(found)


def run_plan(world, urls, deps=None, **extra):
    return runner.plan(request_of(urls, **extra), world["env"], dict(FAKES, **(deps or {})))


# --- items ---------------------------------------------------------------------------------------------------------
def test_plan_of_three_real_urls_and_blocked_ones_lists_every_item_in_order(world, tmp_path, no_socket):
    before = tree(tmp_path)
    result = run_plan(world, [PDF, PNG, NOT_FOUND] + BLOCKED_URLS)
    assert tree(tmp_path) == before, "plan must not create anything"
    assert result["schema_version"] == 1 and result["kind"] == "plan" and result["valid"] is True
    assert result["network_checked"] is False and "error" not in result
    items = result["items"]
    assert [i["id"] for i in items] == ["%04d" % n for n in range(1, 9)]
    assert [i["decision"] for i in items] == ["allow"] * 3 + ["blocked"] * 5
    assert [i["adapter"] for i in items] == ["direct-http"] * 3 + [None] * 5
    assert [i["url_redacted"] for i in items[:3]] == [PDF, PNG, NOT_FOUND]
    assert items[0]["url_sha256"] == urlpolicy.url_sha256(PDF) and len(items[0]["url_sha256"]) == 64
    for item in items[3:]:
        assert item["error"] == errors.item_error("URL_BLOCKED")
    assert "error" not in items[0]
    assert all(i["expect"] == "auto" for i in items)


def test_plan_hides_secrets_in_the_item_urls(world):
    result = run_plan(world, ["https://example.com/a?token=SECRET#frag", "https://u:pw@127.0.0.1/x"])
    text = json.dumps(result)
    assert "SECRET" not in text and "pw" not in text.replace("pwd", "")
    assert result["items"][0]["url_redacted"] == "https://example.com/a?token=REDACTED"


def test_plan_reports_expect_and_keeps_duplicates_as_separate_items(world):
    result = run_plan(world, [{"url": PDF, "expect": "binary"}, PDF])
    assert [i["expect"] for i in result["items"]] == ["binary", "auto"]
    assert [i["id"] for i in result["items"]] == ["0001", "0002"]


@pytest.mark.parametrize("mode", ["media", "gallery", "crawl"])
def test_plan_of_an_unsupported_mode_blocks_allowed_urls_with_unsupported_source(world, mode):
    result = run_plan(world, [PDF, "http://127.0.0.1/"], mode=mode)
    first, second = result["items"]
    assert first["decision"] == "blocked" and first["adapter"] is None and first["error"]["code"] == "UNSUPPORTED_SOURCE"
    assert second["error"]["code"] == "URL_BLOCKED", "a blocked URL keeps its own code"


def test_plan_uses_the_injected_policy_only_through_deps(world):
    policy = urlpolicy.Policy(ports=(5555,), ip_ok=lambda address: True)
    assert run_plan(world, ["http://127.0.0.1:5555/x"], deps={"policy": policy})["items"][0]["decision"] == "allow"
    assert run_plan(world, ["http://127.0.0.1:5555/x"])["items"][0]["decision"] == "blocked"


# --- output ----------------------------------------------------------------------------------------------------------
def test_plan_output_for_the_default_place(world):
    result = run_plan(world, [PDF])
    output = result["output"]
    assert output["decision"] == "allow" and output["root"] == world["work"]
    assert output["parent"] == os.path.join(world["work"], "jobs")
    assert output["job_dir_template"] == os.path.join(world["work"], "jobs", "<job_id>")
    assert result["limits_effective"] == limits.DEFAULTS
    assert sorted(result["warnings"]) == ["concurrency_not_applied", "retries_not_applied"]


def test_plan_with_a_relative_output_dir(world):
    assert run_plan(world, [PDF], output_dir="batch1")["output"]["parent"] == os.path.join(world["work"], "jobs", "batch1")


@pytest.mark.parametrize("bad", ["CON", "NUL", "a:b", "C:foo", "..\\x", "x.", "PROGRA~1", "a\x00b", "\\\\srv\\share\\x"])
def test_a_blocked_output_dir_rejects_the_plan_and_creates_nothing(world, tmp_path, bad, no_socket):
    before = tree(tmp_path)
    result = run_plan(world, [PDF], output_dir=bad)
    assert result["valid"] is False and result["error"]["code"] == "OUTPUT_PATH_BLOCKED"
    assert result["error"]["message"] == errors.message_for("OUTPUT_PATH_BLOCKED")
    assert result["output"]["decision"] == "blocked" and result["output"]["error"]["code"] == "OUTPUT_PATH_BLOCKED"
    assert result["items"][0]["decision"] == "allow", "the items are still reported"
    assert tree(tmp_path) == before


def test_plan_asks_outroot_not_config_output_roots(world, tmp_path):
    os.makedirs(world["work"])
    plugins = os.path.join(world["opcos"], "plugins")
    with open(os.path.join(world["work"], "config.json"), "w", encoding="utf-8") as handle:
        json.dump({"output_roots": [plugins]}, handle)
    assert config.load_config(world["env"]).output_roots[1] == os.path.normpath(plugins)  # the config does list it
    result = run_plan(world, [PDF], output_dir=os.path.join(plugins, "x"))
    assert result["valid"] is False and result["error"]["code"] == "OUTPUT_PATH_BLOCKED"
    assert "root_ignored" in run_plan(world, [PDF])["warnings"]


def test_plan_reports_a_broken_config_as_a_warning(world):
    os.makedirs(world["work"])
    with open(os.path.join(world["work"], "config.json"), "w", encoding="utf-8") as handle:
        handle.write("{")
    result = run_plan(world, [PDF])
    assert result["valid"] is True and "config_invalid" in result["warnings"]


def test_plan_checks_free_space_with_the_formula_of_fr3_7(world):
    effective, _ = limits.resolve({})
    need = limits.required_free_bytes(effective, 2)
    assert run_plan(world, [PDF, PNG], deps={"free_bytes_of": lambda path: need})["valid"] is True
    result = run_plan(world, [PDF, PNG], deps={"free_bytes_of": lambda path: need - GiB - 1})
    assert result["valid"] is False and result["error"]["code"] == "OUTPUT_WRITE_FAILED"
    assert "2" in result["error"]["hint"] and result["output"]["error"]["code"] == "OUTPUT_WRITE_FAILED"


def test_plan_without_a_work_root_is_work_root_unknown(tmp_path):
    result = runner.plan(request_of([PDF]), {}, FAKES)
    assert result["valid"] is False and result["error"]["code"] == "WORK_ROOT_UNKNOWN"
    assert result["items"][0]["decision"] == "allow"


def test_plan_output_has_the_documented_shape(world):
    result = run_plan(world, [PDF, "http://127.0.0.1/"])
    assert schema.validate_plan(result) == []
    broken = dict(result)
    del broken["items"]
    assert schema.validate_plan(broken) == ["items"]
    assert schema.validate_plan("x") == ["$"]
    assert "id" in schema.validate_plan(dict(result, items=[{"url_redacted": "x"}]))[0]


# --- the command line, in process -----------------------------------------------------------------------------------------
def run_cli(world, tmp_path, urls, **extra):
    request = tmp_path / "request.json"
    request.write_text(json.dumps(dict({"schema_version": 1, "urls": urls}, **extra)), encoding="utf-8")
    out = io.StringIO()
    rc = cli.main(["plan", "--request", str(request)], out=out, env=world["env"])
    return rc, out.getvalue()


def test_cli_plan_with_the_real_planner_exits_0_even_when_items_are_blocked(world, tmp_path, no_socket):
    before = tree(tmp_path)
    rc, text = run_cli(world, tmp_path, [PDF, "http://127.0.0.1/", "http://2130706433/", "https://localhost/"])
    assert text.count("\n") == 1 and rc == 0
    obj = json.loads(text)
    assert obj["valid"] is True and [i["decision"] for i in obj["items"]] == ["allow", "blocked", "blocked", "blocked"]
    assert not any(path.startswith(world["work"]) for path in tree(tmp_path)), before
    assert not os.path.exists(world["work"])


def test_cli_plan_with_a_blocked_output_dir_exits_2(world, tmp_path):
    rc, text = run_cli(world, tmp_path, [PDF], output_dir="CON")
    assert rc == 2 and json.loads(text)["error"]["code"] == "OUTPUT_PATH_BLOCKED"


def test_cli_plan_without_a_work_root_exits_2_with_work_root_unknown(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"schema_version": 1, "urls": [PDF]}), encoding="utf-8")
    out = io.StringIO()
    rc = cli.main(["plan", "--request", str(request)], out=out, env={})
    assert rc == 2 and json.loads(out.getvalue())["error"]["code"] == "WORK_ROOT_UNKNOWN" and out.getvalue().count("\n") == 1


@pytest.mark.parametrize("path", ["CON", "NUL", "con.txt", "AUX", "COM1", "C:\\x\\PRN", "\\\\.\\CON", "\\\\?\\C:\\x", "//./CON", "x.txt:stream", "CON."])
def test_request_path_with_a_device_name_is_refused_without_opening_it(path, tmp_path):
    out = io.StringIO()
    rc = cli.main(["plan", "--request", path], out=out, env={"CRAWL_STUDIO_WORK": str(tmp_path / "w")})
    obj = json.loads(out.getvalue())
    assert rc == 2 and obj["error"]["code"] == "INVALID_REQUEST" and out.getvalue().count("\n") == 1
