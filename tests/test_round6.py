"""Review of TK14-TK15 (RF140-RF151): a probe or a vanished file fails one item only, Ctrl+C anywhere ends as
`cancelled`, links under the job folder are checked, unique exclusive manifest temp files, a checked log folder,
every item listed when the manifest cannot be written, text hygiene (log, Mark-of-the-Web, BOM, NBSP), and a refused
job leaves no folder behind."""

import json
import os

import pytest

from agent_crawl_studio import artifacts, manifest, runner, schema, sniff
from agent_crawl_studio.winpath import PathBlocked

from test_run_batch import files_of, go, job_dir_of, manifest_of, server, wrapped_fetch, world  # noqa: F401


def statuses(result):
    return [item["status"] for item in result["items"]]


# ----------------------------------------------------------------------------- RF140
def test_a_free_space_probe_that_raises_fails_that_item_only(world, server):
    calls = []

    def free(path):
        calls.append(path)
        if len(calls) == 3:  # the first call decides the output place, the next two are items 1 and 2
            raise OSError("probe failed")
        return 10 ** 15
    result, code = go(world, server, [server.url("/ok")] * 3, deps={"free_bytes_of": free})
    assert code == 3 and statuses(result) == ["succeeded", "failed", "succeeded"] and result["status"] == "partial"
    assert result["items"][1]["error"]["code"] == "OUTPUT_WRITE_FAILED" and manifest_of(world, result) == result
    assert os.listdir(os.path.join(job_dir_of(world, result), ".tmp")) == []


def test_a_probe_that_fails_in_a_strange_way_is_tool_failed_for_that_item(world, server):
    calls = []

    def free(path):
        calls.append(path)
        if len(calls) == 2:
            raise RuntimeError("SECRET")
        return 10 ** 15
    result, code = go(world, server, [server.url("/ok")] * 2, deps={"free_bytes_of": free})
    assert statuses(result) == ["failed", "succeeded"] and result["items"][0]["error"]["code"] == "TOOL_FAILED"
    assert "SECRET" not in json.dumps(result)


def test_a_file_that_vanishes_after_the_rename_fails_that_item_only(world, server):
    calls = []

    def rename_then_quarantine(src, dst):
        calls.append(dst)
        os.rename(src, dst)
        if len(calls) == 1:
            os.remove(dst)  # an anti-virus product took it away
    result, code = go(world, server, [server.url("/ok")] * 3, deps={"rename": rename_then_quarantine})
    assert code == 3 and statuses(result) == ["failed", "succeeded", "succeeded"]
    assert result["items"][0]["error"]["code"] == "OUTPUT_WRITE_FAILED" and len(files_of(world, result)) == 2
    assert os.listdir(os.path.join(job_dir_of(world, result), ".tmp")) == [] and manifest_of(world, result) == result


# ----------------------------------------------------------------------------- RF141
def interrupt():
    raise KeyboardInterrupt


@pytest.mark.parametrize("where", ["rename", "motw"])
def test_ctrl_c_during_the_placing_of_a_file_cancels_the_job_and_leaves_nothing(world, server, where):
    result, code = go(world, server, [server.url("/ok")] * 3,
                      deps={"rename": lambda src, dst: interrupt()} if where == "rename" else {"motw": lambda path, url: interrupt()})
    assert code == 130 and result["status"] == "cancelled" and statuses(result) == ["cancelled"] * 3
    job = job_dir_of(world, result)
    assert os.listdir(os.path.join(job, ".tmp")) == [] and files_of(world, result) == []
    assert manifest_of(world, result) == result and result["finished_at"] and schema.validate_result(result) == []


def test_ctrl_c_while_the_manifest_is_being_written_still_ends_as_cancelled(world, server):
    calls = []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) == 2:  # the write that records item 1
            raise KeyboardInterrupt
        os.replace(src, dst)
    result, code = go(world, server, [server.url("/ok")] * 3, deps={"replace": replace})
    assert code == 130 and result["status"] == "cancelled" and len(result["items"]) == 3
    assert statuses(result) == ["succeeded", "cancelled", "cancelled"]
    assert manifest_of(world, result) == result


def test_ctrl_c_while_the_run_log_is_written_ends_as_cancelled(world, server, monkeypatch):
    real = runner.RunLog.write
    calls = []

    def write(self, line):
        calls.append(line)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real(self, line)
    monkeypatch.setattr(runner.RunLog, "write", write)
    result, code = go(world, server, [server.url("/ok")] * 2)
    assert code == 130 and statuses(result)[1:] == ["cancelled"] and len(result["items"]) == 2 and manifest_of(world, result) == result


def test_ctrl_c_during_the_final_manifest_write_is_retried_once(world, server):
    calls = []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) == 3:  # start, item 1, then the final write
            raise KeyboardInterrupt
        os.replace(src, dst)
    result, code = go(world, server, [server.url("/ok")], deps={"replace": replace})
    assert code == 130 and result["status"] == "cancelled" and manifest_of(world, result) == result


# ----------------------------------------------------------------------------- RF142
def test_the_writer_checks_its_two_folders_and_each_part(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    seen = []

    def check(path):
        seen.append(os.path.basename(path))
    writer = artifacts.ArtifactWriter(str(job), check=check)
    part = writer.open("0001")
    writer.abort(part)
    assert seen == [".tmp", "files", "0001.part"]


def test_a_blocked_folder_or_part_is_refused_and_the_part_is_removed(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    with pytest.raises(PathBlocked):
        artifacts.ArtifactWriter(str(job), check=lambda path: (_ for _ in ()).throw(PathBlocked("reparse")) if path.endswith(".tmp") else None)

    def part_check(path):
        if path.endswith(".part"):
            raise PathBlocked("outside_root")
    writer = artifacts.ArtifactWriter(str(job), check=part_check)
    with pytest.raises(PathBlocked):
        writer.open("0001")
    assert os.listdir(job / ".tmp") == []


def test_a_junction_in_place_of_the_tmp_folder_refuses_the_job_and_leaves_no_folder(world, server):
    def attrs(path):
        return (0x400, 0) if path.endswith(os.sep + ".tmp") else None
    result, code = go(world, server, [server.url("/ok")], deps={"attrs_of": attrs})
    assert code == 2 and result["job_id"] is None and result["error"]["code"] == "OUTPUT_PATH_BLOCKED"
    assert os.listdir(os.path.join(world["work"], "jobs")) == []  # RF151: the empty job folder is taken away again


def test_a_junction_at_the_part_blocks_the_item_and_stops_the_job(world, server):
    def attrs(path):
        return (0x400, 0) if path.endswith(".part") else None
    fetch = wrapped_fetch(lambda n, url: None)
    result, code = go(world, server, [server.url("/ok")] * 2, deps={"attrs_of": attrs, "fetch": fetch})
    assert code == 1 and statuses(result) == ["blocked", "blocked"] and fetch.calls == []
    assert all(i["error"]["code"] == "OUTPUT_PATH_BLOCKED" for i in result["items"])
    assert os.listdir(os.path.join(job_dir_of(world, result), ".tmp")) == []


# ----------------------------------------------------------------------------- RF143
def test_the_manifest_temp_file_has_a_unique_name_and_never_overwrites_an_existing_one(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "manifest.json.tmp").write_text("someone else's file", encoding="utf-8")  # e.g. a hard link prepared in advance
    writer = manifest.ManifestWriter(str(job))
    writer.start("20261113-120000-abcdef", "t", {}, 1)
    writer.add_item({"id": "0001", "url_redacted": "u", "url_sha256": "0" * 64, "adapter": None, "status": "failed",
                     "artifacts": [], "untrusted": {}})
    assert (job / "manifest.json.tmp").read_text(encoding="utf-8") == "someone else's file"
    assert sorted(os.listdir(job)) == ["manifest.json", "manifest.json.tmp"]


def test_a_temp_name_that_already_exists_is_an_error_not_an_overwrite(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "manifest.json.fixed.tmp").write_text("mine", encoding="utf-8")
    writer = manifest.ManifestWriter(str(job), token_of=lambda: "fixed")
    with pytest.raises(manifest.ManifestError):
        writer.start("20261113-120000-abcdef", "t", {}, 1)
    assert (job / "manifest.json.fixed.tmp").read_text(encoding="utf-8") == "mine" and not (job / "manifest.json").exists()


def test_the_temp_file_is_checked_before_the_replace_and_a_blocked_one_is_removed(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    seen = []

    def check(path):
        seen.append(path)
        if len(seen) == 2:
            raise PathBlocked("reparse")
    writer = manifest.ManifestWriter(str(job), check=check)
    writer.start("20261113-120000-abcdef", "t", {}, 1)
    before = (job / "manifest.json").read_bytes()
    with pytest.raises(manifest.ManifestError):
        writer.finish("failed", "t2")
    assert (job / "manifest.json").read_bytes() == before and os.listdir(job) == ["manifest.json"]
    assert all(path.endswith(".tmp") for path in seen)


# ----------------------------------------------------------------------------- RF144 / RF147
def test_a_junction_in_place_of_the_logs_folder_refuses_the_job(world, server):
    def attrs(path):
        return (0x400, 0) if path.endswith(os.sep + "logs") else None
    result, code = go(world, server, [server.url("/ok")], deps={"attrs_of": attrs})
    assert code == 2 and result["error"]["code"] == "OUTPUT_PATH_BLOCKED"
    assert os.listdir(os.path.join(world["work"], "jobs")) == []


def test_the_run_log_is_created_exclusively_and_never_overwrites(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    log = runner.RunLog(str(job), lambda path: None)
    (job / "logs" / "run.log").write_text("planted", encoding="utf-8")
    log.write("0001 failed TOOL_FAILED u")
    log.write("0002 failed TOOL_FAILED u")
    assert (job / "logs" / "run.log").read_text(encoding="utf-8") == "planted"


def test_the_run_log_file_is_checked_once_it_exists(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    seen = []
    log = runner.RunLog(str(job), seen.append)
    log.write("a")
    log.write("b")
    assert [os.path.basename(p) for p in seen] == ["logs", "run.log"]
    assert (job / "logs" / "run.log").read_text(encoding="utf-8") == "a\nb\n"


def test_a_blocked_run_log_stops_logging_but_not_the_job(tmp_path):
    job = tmp_path / "job"
    job.mkdir()

    def check(path):
        if path.endswith("run.log"):
            raise PathBlocked("reparse")
    log = runner.RunLog(str(job), check)
    log.write("a")
    log.write("b")
    assert (job / "logs" / "run.log").read_text(encoding="utf-8") == "a\n"  # written once, then given up


def test_control_characters_never_reach_a_log_line(world, server, monkeypatch):
    monkeypatch.setattr(runner.urlpolicy, "redact", lambda url: "http://a.example/x\r\nFAKE 9999 ok -‮")
    result, _ = go(world, server, [server.url("/ok")] * 2)
    text = open(os.path.join(job_dir_of(world, result), "logs", "run.log"), encoding="utf-8", newline="").read()
    assert text.count("\n") == 2 and "\r" not in text and "‮" not in text


# ----------------------------------------------------------------------------- RF145
def test_when_the_manifest_cannot_be_written_every_item_is_still_listed_and_the_disk_copy_stays_valid(world, server):
    calls = []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) >= 2:
            error = PermissionError(13, "in use")
            error.winerror = 32
            raise error
        os.replace(src, dst)
    result, code = go(world, server, [server.url("/ok")] * 4, deps={"replace": replace})
    assert code == 1 and result["status"] == "failed" and "manifest_not_updated" in result["warnings"]
    assert [i["id"] for i in result["items"]] == ["0001", "0002", "0003", "0004"]
    assert statuses(result)[1:] == ["skipped"] * 3 and result["counts"]["total"] == 4 and schema.validate_result(result) == []
    disk = manifest_of(world, result)
    assert schema.validate_result(disk) == [] and disk != result and disk["status"] is None


# ----------------------------------------------------------------------------- RF148 / RF150
def test_a_host_url_with_line_breaks_cannot_add_lines_to_the_zone_stream(tmp_path):
    target = tmp_path / "f.bin"
    target.write_bytes(b"x")
    artifacts.set_motw(str(target), "https://a.example/x\r\nZoneId=0\r\n[Other]")
    with open(str(target) + ":Zone.Identifier", encoding="utf-8", newline="") as handle:
        assert handle.read().splitlines() == ["[ZoneTransfer]", "ZoneId=3", "HostUrl=https://a.example/xZoneId=0[Other]"]


@pytest.mark.parametrize("head, mime", [
    (b"\xef\xbb\xbfhello world", "text/plain"),
    ("a b and more".encode("utf-8"), "text/plain"),
    (b"\xef\xbb\xbf{\"a\": 1}", "application/json"),
    (b"\xef\xbb\xbf", "application/octet-stream"),
])
def test_text_with_a_byte_order_mark_or_a_no_break_space_is_still_text(head, mime):
    assert sniff.detect(head) == mime


# ----------------------------------------------------------------------------- RF151
def test_a_job_refused_after_its_folder_was_made_leaves_no_folder(world, server):
    def refuse(src, dst):
        error = OSError(5, "disk")
        raise error
    result, code = go(world, server, [server.url("/ok")], deps={"replace": refuse})
    assert code == 2 and result["job_id"] is None
    assert os.listdir(os.path.join(world["work"], "jobs")) == []
