"""SPR-03 TK26 (FR15.1-FR15.6, SC18): `crawl-studio retry --manifest <file> --request <file>`. RED until TK27.

    cli.COMMANDS["retry"] = ("--manifest", "--request")
    retrycmd.MAX_MANIFEST_BYTES = 16 * 2**20
    retrycmd.load_manifest(path, env, deps) -> dict              (FR15.2, raises ContractError)
    retrycmd.select_items(old, request, policy) -> list[int]     (FR15.3, FR15.4: the 1-based ids to run)
    retrycmd.retry(manifest_path, request, env, deps=None) -> (result, exit code)
    runner.run(request, env, deps=None, *, only=None, retry_of=None)

The old manifest is data from a file anybody could have written: every shape rule is checked before one connection
or one folder is made. The "Internet" is a scripted `fetch` that counts what it is asked for."""

import dataclasses
import hashlib
import io
import json
import os
import sys

import pytest

from agent_crawl_studio import cli, errors, schema, urlpolicy
from agent_crawl_studio.errors import ContractError
from agent_crawl_studio.netfetch import FetchOutcome

from test_netfetch import policy_for, server  # noqa: F401
from test_retries import GOOD, bad
from test_run_batch import FAKES, files_of, go, job_dir_of, manifest_of, request_of, world  # noqa: F401

OLD_ID = "20261113-100000-aaaaaa"
STATUS_OF = {"ok": "succeeded", "retryable": "failed", "cancelled": "cancelled", "blocked": "blocked", "forbidden": "failed"}


class _Lazy:
    """`retrycmd` imported on first use, so that every test is red on its own (not one collection error)."""

    def __getattr__(self, name):
        import agent_crawl_studio.retrycmd as module
        return getattr(module, name)


retrycmd = _Lazy()


def urls_of(server, n=4):
    return [server.url("/f%d" % i) for i in range(1, n + 1)]


def item_of(policy, urls, number, kind):
    error = {"ok": None,
             "retryable": {"code": "NETWORK_ERROR", "retryable": True, "reason": "http_status", "http_status": 503},
             "cancelled": {"code": "CANCELLED", "retryable": True},
             "blocked": {"code": "URL_BLOCKED", "retryable": False},
             "forbidden": {"code": "NETWORK_ERROR", "retryable": False, "reason": "forbidden", "http_status": 403}}[kind]
    item = {"id": "%04d" % number, "url_redacted": urls[number - 1].split("?")[0], "url_sha256": urlpolicy.url_sha256(urls[number - 1], policy),
            "adapter": None if kind in ("blocked", "cancelled") else "direct-http", "status": STATUS_OF[kind], "artifacts": [],
            "untrusted": {}}
    if error:
        item["error"] = dict(error, message="x")
    return item


def write_manifest(world, server, kinds, numbers=None, retry_of=None, job_id=OLD_ID, total=None, edit=None, name="manifest.json"):
    """A finished manifest of the job `job_id`, as a run would have left it; `kinds` per item, `numbers` their ids."""
    policy = policy_for(server)
    urls = urls_of(server, max(numbers or [len(kinds)]))
    numbers = numbers or list(range(1, len(kinds) + 1))
    items = [item_of(policy, urls, n, k) for n, k in zip(numbers, kinds)]
    counts = {"total": len(items) if total is None else total}
    counts.update({status: sum(1 for i in items if i["status"] == status) for status in
                   ("succeeded", "failed", "blocked", "cancelled", "skipped")})
    doc = {"schema_version": 1, "kind": "result", "job_id": job_id, "status": "partial", "started_at": "2026-11-13T10:00:00Z",
           "finished_at": "2026-11-13T10:01:00Z", "counts": counts, "limits_effective": {}, "items": items, "warnings": []}
    if retry_of:
        doc["retry_of"] = retry_of
    if edit:
        edit(doc)
    folder = os.path.join(world["work"], "jobs", job_id)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    return path


class ByUrl:
    """A `fetch` dep that answers per URL (a list of outcomes, the last one repeats) and records every call."""

    def __init__(self, answers=None, default=GOOD):
        self.answers, self.default, self.calls = answers or {}, default, []

    def __call__(self, url, expect, limits, sink, policy, **kw):
        self.calls.append(url)
        seen = self.calls.count(url)
        steps = self.answers.get(url, [self.default])
        step = steps[min(seen, len(steps)) - 1]
        if isinstance(step, BaseException):
            raise step
        if step.status == "ok":
            sink(b"hello " + url.encode()[-6:])
        return dataclasses.replace(step, final_url=url, final_url_redacted=url)


def deps_of(server, fetch, **extra):
    deps = dict(FAKES, policy=policy_for(server), sleep=lambda seconds: None, fetch=fetch)
    deps.update(extra)
    return deps


def retry(world, server, manifest, urls, fetch, limits=None, **extra):
    request = request_of(urls, limits=limits or {"retries": 0})
    return retrycmd.retry(manifest, request, world["env"], deps_of(server, fetch, **extra))


def jobs(world):
    return sorted(os.listdir(os.path.join(world["work"], "jobs")))


def sha256_file(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# ----------------------------------------------------------------------------- the surface (FR15.1)
def test_the_command_line_has_four_commands_and_the_usage_names_retry():
    assert cli.COMMANDS == {"capabilities": (), "plan": ("--request",), "run": ("--request",),
                            "retry": ("--manifest", "--request")}
    assert cli.USAGE == ("usage: crawl-studio capabilities | plan --request <file> | run --request <file> | "
                         "retry --manifest <file> --request <file>")
    assert retrycmd.MAX_MANIFEST_BYTES == 16 * 2 ** 20


def run_main(argv, retry_fn=None, env=None):
    out = io.StringIO()
    rc = cli.main(argv, out=out, env=env if env is not None else {}, retry_fn=retry_fn)
    return rc, out.getvalue()


@pytest.mark.parametrize("argv", [["retry"], ["retry", "--manifest", "m"], ["retry", "--request", "r", "--manifest", "m"],
                                  ["retry", "--manifest", "m", "--request", "r", "--x", "1"], ["retry", "--manifest", "m", "--other", "r"]])
def test_any_other_form_of_retry_is_a_usage_error(argv):
    rc, text = run_main(argv)
    assert rc == 2 and json.loads(text)["error"]["code"] == "USAGE_ERROR" and "retry --manifest <file> --request <file>" in json.loads(text)["error"]["hint"]


def request_file(tmp_path, urls):
    path = tmp_path / "request.json"
    path.write_bytes(json.dumps({"schema_version": 1, "urls": urls}).encode("utf-8"))
    return str(path)


def test_the_command_reads_both_files_and_prints_the_result_as_one_line(tmp_path):
    seen = {}

    def retry_fn(manifest_path, request, env):
        seen.update(manifest=manifest_path, urls=[spec.url for spec in request.urls])
        return {"kind": "result", "status": "succeeded", "retry_of": OLD_ID}, 0

    rc, text = run_main(["retry", "--manifest", "M", "--request", request_file(tmp_path, ["https://a.example/x"])], retry_fn)
    assert rc == 0 and text.count("\n") == 1 and json.loads(text)["retry_of"] == OLD_ID
    assert seen == {"manifest": "M", "urls": ["https://a.example/x"]}


def test_a_bad_request_file_is_exit_2_before_the_manifest_is_touched(tmp_path):
    def retry_fn(*args):
        raise AssertionError("the manifest must not be read")

    bad_file = tmp_path / "request.json"
    bad_file.write_bytes(b"{not json")
    rc, text = run_main(["retry", "--manifest", "M", "--request", str(bad_file)], retry_fn)
    obj = json.loads(text)
    assert rc == 2 and obj["kind"] == "result" and obj["job_id"] is None and obj["error"]["code"] == "INVALID_REQUEST"


def test_a_contract_error_of_the_manifest_is_a_result_with_exit_2(tmp_path):
    def retry_fn(manifest_path, request, env):
        raise ContractError("INVALID_REQUEST", "$", "out_of_range")

    rc, text = run_main(["retry", "--manifest", "M", "--request", request_file(tmp_path, ["https://a.example/x"])], retry_fn)
    obj = json.loads(text)
    assert rc == 2 and obj["status"] == "failed" and obj["job_id"] is None and obj["error"]["hint"] == "$ out_of_range"


def test_nothing_to_retry_is_a_hint_code():
    assert "nothing_to_retry" in errors.HINT_CODES


def test_ctrl_c_in_the_retry_command_is_interrupted_exit_130(tmp_path):
    def retry_fn(*args):
        raise KeyboardInterrupt

    rc, text = run_main(["retry", "--manifest", "M", "--request", request_file(tmp_path, ["https://a.example/x"])], retry_fn)
    assert rc == 130 and json.loads(text)["error"]["code"] == "INTERRUPTED"


# ----------------------------------------------------------------------------- which items run (FR15.4, FR15.5)
def test_only_the_failed_retryable_and_the_cancelled_items_run(world, server):
    path = write_manifest(world, server, ["ok", "retryable", "cancelled", "blocked", "forbidden"])
    before = sha256_file(path)
    fetch = ByUrl()
    urls = urls_of(server, 5)
    result, code = retry(world, server, path, urls, fetch)
    assert code == 0 and result["status"] == "succeeded" and result["retry_of"] == OLD_ID
    assert fetch.calls == [urls[1], urls[2]]  # items 1, 4 and 5 are not asked for: no connection at all
    assert [i["id"] for i in result["items"]] == ["0002", "0003"] and result["counts"]["total"] == 2
    assert [i["url_sha256"] for i in result["items"]] == [urlpolicy.url_sha256(u, policy_for(server)) for u in urls[1:3]]
    assert [name[:5] for name in files_of(world, result)] == ["0002-", "0003-"]
    assert schema.validate_result(result) == [] and manifest_of(world, result) == result
    assert sha256_file(path) == before  # FR15.5: the old manifest is not changed by one byte
    assert len(jobs(world)) == 2 and result["job_id"] != OLD_ID


def test_the_result_of_a_retry_that_fails_again_is_partial_or_failed_over_the_items_it_ran(world, server):
    path = write_manifest(world, server, ["ok", "retryable", "cancelled"])
    urls = urls_of(server, 3)
    result, code = retry(world, server, path, urls, ByUrl({urls[1]: [bad()]}))
    assert code == 3 and result["status"] == "partial"
    assert [i["status"] for i in result["items"]] == ["failed", "succeeded"] and result["counts"]["failed"] == 1
    result, code = retry(world, server, path, urls, ByUrl(default=bad()))
    assert code == 1 and result["status"] == "failed"


def test_a_retry_of_a_retry_manifest_works_with_the_original_request(world, server):
    path = write_manifest(world, server, ["ok", "retryable", "cancelled", "ok"])
    urls = urls_of(server, 4)
    first, code = retry(world, server, path, urls, ByUrl({urls[1]: [bad()]}))
    assert code == 3 and [i["id"] for i in first["items"]] == ["0002", "0003"]
    second_path = os.path.join(job_dir_of(world, first), "manifest.json")  # 2 items, `retry_of`, a request of 4
    fetch = ByUrl()
    second, code = retry(world, server, second_path, urls, fetch)
    assert code == 0 and second["retry_of"] == first["job_id"] and [i["id"] for i in second["items"]] == ["0002"]
    assert fetch.calls == [urls[1]]


def test_a_cancelled_run_is_finished_by_retry(world, server):
    urls = urls_of(server, 3)
    first, code = go(world, server, urls, deps={"fetch": ByUrl({urls[1]: [KeyboardInterrupt()]})}, limits={"retries": 0})
    assert code == 130 and [i["status"] for i in first["items"]] == ["succeeded", "cancelled", "cancelled"]
    path = os.path.join(job_dir_of(world, first), "manifest.json")
    fetch = ByUrl()
    result, code = retry(world, server, path, urls, fetch)
    assert code == 0 and fetch.calls == urls[1:] and [i["id"] for i in result["items"]] == ["0002", "0003"]
    assert len(files_of(world, first)) == 1 and len(files_of(world, result)) == 2


def test_a_ctrl_c_in_a_retry_cancels_the_items_it_was_to_run_under_their_own_ids(world, server):
    path = write_manifest(world, server, ["ok", "retryable", "cancelled", "ok"])
    urls = urls_of(server, 4)
    result, code = retry(world, server, path, urls, ByUrl({urls[1]: [KeyboardInterrupt()]}))
    assert code == 130 and result["status"] == "cancelled" and result["retry_of"] == OLD_ID
    assert [(i["id"], i["status"]) for i in result["items"]] == [("0002", "cancelled"), ("0003", "cancelled")]
    assert result["counts"]["total"] == 2 and manifest_of(world, result) == result


def test_the_attempts_inside_the_retry_still_count(world, server):
    path = write_manifest(world, server, ["retryable"])
    urls = urls_of(server, 1)
    fetch = ByUrl({urls[0]: [bad(), GOOD]})
    result, code = retry(world, server, path, urls, fetch, limits={"retries": 2})
    assert code == 0 and result["items"][0]["attempts"] == 2


def test_nothing_to_retry_is_exit_2_with_its_hint_and_no_folder(world, server):
    path = write_manifest(world, server, ["ok", "blocked", "forbidden"])
    fetch = ByUrl()
    with pytest.raises(ContractError) as caught:
        retry(world, server, path, urls_of(server, 3), fetch)
    assert (caught.value.code, caught.value.hint) == ("INVALID_REQUEST", "$ nothing_to_retry")
    assert fetch.calls == [] and jobs(world) == [OLD_ID]


def test_the_command_reports_nothing_to_retry_in_the_result(world, server):
    urls = ["https://files.example.com/f1", "https://files.example.com/f2"]

    def edit(doc):  # the real command hashes with the default policy
        for item, url in zip(doc["items"], urls):
            item["url_sha256"] = urlpolicy.url_sha256(url)

    path = write_manifest(world, server, ["ok", "ok"], edit=edit)
    rc, text = run_main(["retry", "--manifest", path, "--request", request_file(tmp_of(world), urls)], env=world["env"])
    obj = json.loads(text)
    assert rc == 2 and obj["error"]["hint"] == "$ nothing_to_retry" and obj["job_id"] is None and jobs(world) == [OLD_ID]


def tmp_of(world):
    from pathlib import Path
    return Path(world["home"]).parent


# ----------------------------------------------------------------------------- the request must be the same one (FR15.3)
def test_a_changed_url_is_exit_2_and_makes_no_folder(world, server):
    path = write_manifest(world, server, ["ok", "retryable", "cancelled", "ok"])
    urls = urls_of(server, 4)
    urls[1] = server.url("/other")
    fetch = ByUrl()
    with pytest.raises(ContractError) as caught:
        retry(world, server, path, urls, fetch)
    assert caught.value.code == "INVALID_REQUEST" and fetch.calls == [] and jobs(world) == [OLD_ID]


@pytest.mark.parametrize("count", [3, 5])
def test_an_original_manifest_needs_a_request_with_as_many_items_as_its_total(world, server, count):
    path = write_manifest(world, server, ["ok", "retryable", "cancelled", "ok"])
    fetch = ByUrl()
    with pytest.raises(ContractError):
        retry(world, server, path, urls_of(server, count), fetch)
    assert fetch.calls == [] and jobs(world) == [OLD_ID]


def test_an_item_id_past_the_request_is_exit_2(world, server):
    path = write_manifest(world, server, ["ok", "retryable", "cancelled", "ok"])
    with pytest.raises(ContractError):
        retry(world, server, path, urls_of(server, 2), ByUrl())  # also a 4-item total against 2 items


def test_a_retry_manifest_may_be_paired_with_a_request_that_has_more_items_than_it_lists(world, server):
    path = write_manifest(world, server, ["retryable"], numbers=[3], retry_of=OLD_ID, job_id="20261113-110000-bbbbbb")
    result, code = retry(world, server, path, urls_of(server, 4), ByUrl())
    assert code == 0 and [i["id"] for i in result["items"]] == ["0003"] and result["retry_of"] == "20261113-110000-bbbbbb"


# ----------------------------------------------------------------------------- the manifest is not trusted (FR15.2)
def broken(edit):
    return pytest.param(edit, id=edit.__name__)


def status_null(doc):
    doc["status"] = None


def status_unknown(doc):
    doc["status"] = "done"


def status_not_text(doc):
    doc["status"] = ["failed"]


def duplicate_id(doc):
    doc["items"][2]["id"] = doc["items"][1]["id"]


def id_not_four_digits(doc):
    doc["items"][1]["id"] = "2"


def id_zero(doc):
    doc["items"][1]["id"] = "0000"


def total_off(doc):
    doc["counts"]["total"] = 9


def counts_a_string(doc):
    doc["counts"] = "four"


def job_id_with_dots(doc):
    doc["job_id"] = "../x"


def job_id_null(doc):
    doc["job_id"] = None


def retry_of_with_dots(doc):
    doc["retry_of"] = "../x"


def sha_not_hex(doc):
    doc["items"][1]["url_sha256"] = "z" * 64


def sha_short(doc):
    doc["items"][1]["url_sha256"] = "ab"


def sha_upper(doc):
    doc["items"][1]["url_sha256"] = doc["items"][1]["url_sha256"].upper()


def retryable_a_string(doc):
    doc["items"][1]["error"]["retryable"] = "yes"


def failed_without_error(doc):
    del doc["items"][1]["error"]


def item_status_unknown(doc):
    doc["items"][1]["status"] = "maybe"


def kind_plan(doc):
    doc["kind"] = "plan"


def version_two(doc):
    doc["schema_version"] = 2


def items_not_a_list(doc):
    doc["items"] = {"0001": {}}


def item_not_an_object(doc):
    doc["items"][0] = "x"


BROKEN = [broken(edit) for edit in (
    status_null, status_unknown, status_not_text, duplicate_id, id_not_four_digits, id_zero, total_off, counts_a_string,
    job_id_with_dots, job_id_null, retry_of_with_dots, sha_not_hex, sha_short, sha_upper, retryable_a_string,
    failed_without_error, item_status_unknown, kind_plan, version_two, items_not_a_list, item_not_an_object)]


@pytest.mark.parametrize("edit", BROKEN)
def test_a_manifest_that_breaks_a_shape_rule_is_exit_2_with_no_connection_and_no_folder(world, server, edit):
    path = write_manifest(world, server, ["ok", "retryable", "cancelled", "blocked"], edit=edit)
    fetch = ByUrl()
    with pytest.raises(ContractError) as caught:
        retry(world, server, path, urls_of(server, 4), fetch)
    assert caught.value.code == "INVALID_REQUEST" and fetch.calls == [] and jobs(world) == [OLD_ID]


def test_a_manifest_that_is_not_json_or_has_a_duplicate_key_is_refused(world, server):
    for raw in (b"{not json", b"", b"[]", b'{"kind":"result","kind":"result"}', b"\xff\xfe"):
        path = write_manifest(world, server, ["retryable"])
        with open(path, "wb") as handle:
            handle.write(raw)
        with pytest.raises(ContractError) as caught:
            retry(world, server, path, urls_of(server, 1), ByUrl())
        assert caught.value.code == "INVALID_REQUEST"


def test_a_manifest_over_16_mib_is_refused_without_reading_it_all(world, server):
    path = write_manifest(world, server, ["retryable"])
    with open(path, "wb") as handle:
        handle.write(b" " * (16 * 2 ** 20) + b"{}")
    with pytest.raises(ContractError) as caught:
        retry(world, server, path, urls_of(server, 1), ByUrl())
    assert (caught.value.code, caught.value.why) == ("INVALID_REQUEST", "out_of_range")


def test_a_manifest_of_exactly_16_mib_is_read(world, server):
    path = write_manifest(world, server, ["retryable"])
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    padded = text + " " * (16 * 2 ** 20 - len(text.encode("utf-8")))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(padded)
    assert os.path.getsize(path) == 16 * 2 ** 20
    assert retrycmd.load_manifest(path, world["env"], deps_of(server, None))["job_id"] == OLD_ID


def test_the_file_must_be_called_manifest_json_and_be_a_file(world, server):
    path = write_manifest(world, server, ["retryable"], name="other.json")
    with pytest.raises(ContractError):
        retry(world, server, path, urls_of(server, 1), ByUrl())
    folder = os.path.join(world["work"], "jobs", OLD_ID)
    os.makedirs(os.path.join(folder, "manifest.json"))
    with pytest.raises(ContractError):
        retry(world, server, os.path.join(folder, "manifest.json"), urls_of(server, 1), ByUrl())
    with pytest.raises(ContractError):
        retry(world, server, os.path.join(folder, "nothing", "manifest.json"), urls_of(server, 1), ByUrl())


@pytest.mark.parametrize("path", [r"C:\Windows\Temp\job\manifest.json", r"\\server\share\job\manifest.json",
                                  "\\\\?\\C:\\x\\manifest.json", r"C:\x\CON\manifest.json", r"C:\x\manifest.json:stream",
                                  "C:\\x\\..\\y\\manifest.json"])
def test_a_manifest_path_that_is_special_or_outside_every_root_is_exit_2(world, server, path):
    fetch = ByUrl()
    with pytest.raises(ContractError) as caught:
        retry(world, server, path, urls_of(server, 1), fetch)
    assert caught.value.code == "INVALID_REQUEST" and fetch.calls == [] and not os.path.exists(os.path.join(world["work"], "jobs"))


def test_a_manifest_outside_the_allowed_roots_is_exit_2_even_when_it_is_valid(world, server, tmp_path):
    elsewhere = tmp_path / "elsewhere" / OLD_ID
    elsewhere.mkdir(parents=True)
    source = write_manifest(world, server, ["retryable"])
    (elsewhere / "manifest.json").write_bytes(open(source, "rb").read())
    with pytest.raises(ContractError):
        retry(world, server, str(elsewhere / "manifest.json"), urls_of(server, 1), ByUrl())


@pytest.mark.skipif(sys.platform != "win32", reason="a junction is a Windows reparse point")
def test_a_path_that_goes_through_a_junction_is_exit_2(world, server, tmp_path):
    import _winapi
    source = write_manifest(world, server, ["retryable"], job_id="20261113-120000-cccccc")
    real = os.path.dirname(source)
    link = os.path.join(world["work"], "jobs", "linked")
    _winapi.CreateJunction(real, link)
    try:
        deps = {key: value for key, value in deps_of(server, ByUrl()).items() if key != "attrs_of"}  # the real lstat
        request = request_of(urls_of(server, 1), limits={"retries": 0})
        with pytest.raises(ContractError) as caught:
            retrycmd.retry(os.path.join(link, "manifest.json"), request, world["env"], deps)
        assert caught.value.code == "INVALID_REQUEST"
        assert retrycmd.load_manifest(source, world["env"], deps)["job_id"] == "20261113-120000-cccccc"  # the real path is fine
    finally:
        os.rmdir(link)  # the link only: never what it points at
    assert os.path.exists(source)


# ----------------------------------------------------------------------------- the pieces
def test_select_items_gives_the_ids_of_the_failed_retryable_and_the_cancelled(world, server):
    path = write_manifest(world, server, ["ok", "retryable", "cancelled", "blocked", "forbidden"])
    old = retrycmd.load_manifest(path, world["env"], deps_of(server, None))
    request = request_of(urls_of(server, 5))
    assert retrycmd.select_items(old, request, policy_for(server)) == [2, 3]


def test_the_runner_runs_only_the_ids_it_is_given_and_names_the_old_job(world, server):
    urls = urls_of(server, 3)
    fetch = ByUrl()
    from agent_crawl_studio import runner
    result, code = runner.run(request_of(urls), world["env"], deps_of(server, fetch), only=[3], retry_of=OLD_ID)
    assert code == 0 and fetch.calls == [urls[2]] and result["retry_of"] == OLD_ID
    assert [i["id"] for i in result["items"]] == ["0003"] and result["counts"]["total"] == 1
    plain, _ = runner.run(request_of(urls), world["env"], deps_of(server, ByUrl()))
    assert "retry_of" not in plain and len(plain["items"]) == 3


def test_a_result_with_a_retry_of_that_is_not_a_job_id_is_reported(world, server):
    path = write_manifest(world, server, ["ok"])
    doc = json.load(open(path, encoding="utf-8"))
    assert schema.validate_result(doc) == []
    doc["retry_of"] = "../x"
    assert "retry_of" in schema.validate_result(doc)
    doc["retry_of"] = OLD_ID
    assert schema.validate_result(doc) == []
