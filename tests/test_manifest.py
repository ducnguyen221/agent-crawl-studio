"""Manifest = result v1, written incrementally and atomically (SPEC FR5.6, FR5.7, FR1.5; PLAN TK14).
RED until TK15 writes `agent_crawl_studio.manifest` and `schema.validate_result`.

Pinned interface:
    manifest.ManifestWriter(job_dir, *, replace=os.replace, sleep=time.sleep)
        .start(job_id, started_at, limits_effective, total, warnings=()) ; .add_item(item) ; .doc (dict)
        .finish(status, finished_at, error=None, warnings=())      # each call rewrites manifest.json atomically
    manifest.ManifestError ; manifest.RETRY_DELAYS == (0.05, 0.1, 0.2, 0.4)
    manifest.untrusted_text(value) -> str ; manifest.clean_untrusted(dict) -> dict
    schema.validate_result(obj) -> list of problems ([] when the shape is the documented one)"""

import json
import os

import pytest

from agent_crawl_studio import schema


@pytest.fixture
def mf():
    import agent_crawl_studio.manifest as module
    return module


@pytest.fixture
def job(tmp_path):
    path = tmp_path / "job"
    path.mkdir()
    return path


LIMITS = {"max_items": 100}


def item(n, status="succeeded", **extra):
    base = {"id": "%04d" % n, "url_redacted": "https://a.example/%d" % n, "url_sha256": "0" * 64, "adapter": "direct-http",
            "status": status, "artifacts": [], "untrusted": {}}
    base.update(extra)
    return base


def on_disk(job):
    return json.loads((job / "manifest.json").read_text(encoding="utf-8"))


class Sleeper:
    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


def writer_of(mf, job, **kw):
    writer = mf.ManifestWriter(str(job), **kw)
    writer.start("20261113-120000-abcdef", "2026-11-13T12:00:00Z", LIMITS, 3)
    return writer


# ----------------------------------------------------------------------------- shape and increments
def test_the_manifest_is_valid_result_v1_from_the_first_moment(mf, job):
    writer = writer_of(mf, job)
    doc = on_disk(job)
    assert schema.validate_result(doc) == []
    assert doc["kind"] == "result" and doc["schema_version"] == 1 and doc["job_id"] == "20261113-120000-abcdef"
    assert doc["status"] is None and doc["finished_at"] is None and doc["started_at"] == "2026-11-13T12:00:00Z"
    assert doc["counts"]["total"] == 3 and doc["items"] == [] and doc["warnings"] == [] and doc["limits_effective"] == LIMITS
    assert writer.doc == doc


def test_after_every_item_the_file_is_valid_and_has_one_more_item(mf, job):
    writer = writer_of(mf, job)
    for n, status in enumerate(("succeeded", "failed", "blocked"), 1):
        writer.add_item(item(n, status))
        doc = on_disk(job)
        assert schema.validate_result(doc) == [] and [i["id"] for i in doc["items"]] == ["%04d" % k for k in range(1, n + 1)]
        assert doc["status"] is None and doc["counts"]["total"] == 3
    assert on_disk(job)["counts"] == {"total": 3, "succeeded": 1, "failed": 1, "blocked": 1, "cancelled": 0, "skipped": 0}


def test_finish_sets_status_finish_time_error_and_warnings(mf, job):
    writer = writer_of(mf, job)
    writer.add_item(item(1))
    writer.finish("partial", "2026-11-13T12:01:00Z", error={"code": "OUTPUT_WRITE_FAILED", "message": "m", "retryable": False},
                  warnings=["manifest_not_updated"])
    doc = on_disk(job)
    assert doc["status"] == "partial" and doc["finished_at"] == "2026-11-13T12:01:00Z"
    assert doc["error"]["code"] == "OUTPUT_WRITE_FAILED" and doc["warnings"] == ["manifest_not_updated"]
    assert writer.doc == doc and schema.validate_result(doc) == []


def test_validate_result_names_what_is_missing(mf):
    assert schema.validate_result("x") == ["$"]
    good = {"schema_version": 1, "kind": "result", "job_id": None, "status": "failed", "started_at": None, "finished_at": None,
            "counts": {}, "limits_effective": {}, "items": [], "warnings": []}
    assert schema.validate_result(good) == []
    bad = dict(good)
    del bad["status"]
    bad["items"] = [{"id": "0001"}, "x"]
    problems = schema.validate_result(bad)
    assert "status" in problems and "items[1]" in problems and "items[0].status" in problems


# ----------------------------------------------------------------------------- atomic write, retries
def test_a_replace_that_fails_midway_leaves_the_previous_manifest_untouched(mf, job):
    calls = []

    def replace(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise OSError(5, "disk went away")
        os.replace(src, dst)
    writer = writer_of(mf, job, replace=replace)
    before = (job / "manifest.json").read_bytes()
    with pytest.raises(mf.ManifestError):
        writer.add_item(item(1))
    assert (job / "manifest.json").read_bytes() == before
    assert sorted(os.listdir(job)) == ["manifest.json"]  # no temp file left behind


def sharing_violation(code=32):
    error = PermissionError(13, "in use")
    error.winerror = code
    return error


def test_a_handle_held_by_someone_else_is_retried_five_times_with_doubling_pauses(mf, job):
    sleeper = Sleeper()
    state = {"fail": False, "calls": 0}

    def replace(src, dst):
        if state["fail"]:
            state["calls"] += 1
            raise sharing_violation()
        os.replace(src, dst)
    writer = writer_of(mf, job, replace=replace, sleep=sleeper)
    state["fail"] = True
    with pytest.raises(mf.ManifestError):
        writer.add_item(item(1))
    assert state["calls"] == 5 and sleeper.delays == [0.05, 0.1, 0.2, 0.4] == list(mf.RETRY_DELAYS)
    assert on_disk(job)["items"] == [] and writer.doc["items"][0]["id"] == "0001"  # memory has it, the file does not


def test_a_short_sharing_violation_succeeds_on_a_later_try(mf, job):
    sleeper = Sleeper()
    state = {"left": 0}

    def replace(src, dst):
        if state["left"]:
            state["left"] -= 1
            raise sharing_violation(5)
        os.replace(src, dst)
    writer = writer_of(mf, job, replace=replace, sleep=sleeper)
    state["left"] = 2
    writer.add_item(item(1))
    assert sleeper.delays == [0.05, 0.1] and len(on_disk(job)["items"]) == 1


def test_a_real_open_handle_on_the_manifest_blocks_the_replace_until_it_is_closed(mf, job):
    sleeper = Sleeper()
    writer = writer_of(mf, job, sleep=sleeper)
    holder = open(job / "manifest.json", "rb")  # a reader, an indexer, an anti-virus scanner
    try:
        with pytest.raises(mf.ManifestError):
            writer.add_item(item(1))
        assert len(sleeper.delays) == 4 and on_disk_via(holder)["items"] == []
    finally:
        holder.close()
    writer.add_item(item(2))
    assert [i["id"] for i in on_disk(job)["items"]] == ["0001", "0002"]  # memory kept item 1


def on_disk_via(handle):
    handle.seek(0)
    return json.loads(handle.read().decode("utf-8"))


# ----------------------------------------------------------------------------- the untrusted block
def test_untrusted_text_is_cut_and_stripped(mf):
    dirty = "a\x00b\x1fc\x7fd\x85e‮f‎g⁦h﻿i\r\nj k" + "x" * 500
    clean = mf.untrusted_text(dirty)
    assert clean.startswith("abcdefghijk") and len(clean) == 200
    for ch in "\x00\x1f\x7f\x85‮‎⁦﻿\r\n ":
        assert ch not in clean
    assert mf.untrusted_text("plain text") == "plain text" and mf.untrusted_text(None) == ""


def test_clean_untrusted_cleans_every_value_and_drops_empty_ones(mf):
    got = mf.clean_untrusted({"content_type": "text/html‮", "retry_after": "30", "final_url_redacted": "", "x": None})
    assert got == {"content_type": "text/html", "retry_after": "30"}
