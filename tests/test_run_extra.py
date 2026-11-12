"""The error paths of the artifact manager, manifest and `run` that test_artifacts / test_manifest / test_run_batch
do not reach (TK15 coverage)."""

import os

import pytest

from agent_crawl_studio import artifacts, schema

from test_run_batch import go, job_dir_of, server, world  # noqa: F401


def test_a_job_folder_that_cannot_get_its_subfolders_is_a_write_failure(tmp_path):
    (tmp_path / ".tmp").write_bytes(b"a file where the folder should be")
    with pytest.raises(artifacts.ArtifactError) as caught:
        artifacts.ArtifactWriter(str(tmp_path))
    assert caught.value.code == "OUTPUT_WRITE_FAILED"


def test_a_permission_error_that_is_not_a_sharing_violation_is_not_retried():
    pauses = []

    def deny():
        error = PermissionError(13, "denied")
        error.winerror = 3
        raise error
    with pytest.raises(PermissionError):
        artifacts.retry_io(deny, pauses.append)
    assert pauses == []


def test_validate_result_survives_items_that_are_not_a_list():
    good = {key: None for key in schema.RESULT_KEYS}
    good["items"] = "not a list"
    assert schema.validate_result(good) == []


def test_a_part_that_cannot_be_opened_fails_the_item_only(world, server, monkeypatch):
    real = artifacts.ArtifactWriter.open
    calls = []

    def open_part(self, item_id):
        calls.append(item_id)
        if len(calls) == 1:
            raise artifacts.ArtifactError("part")
        return real(self, item_id)
    monkeypatch.setattr(artifacts.ArtifactWriter, "open", open_part)
    result, code = go(world, server, [server.url("/ok"), server.url("/ok")])
    assert code == 3 and [i["status"] for i in result["items"]] == ["failed", "succeeded"]
    assert result["items"][0]["error"]["code"] == "OUTPUT_WRITE_FAILED"


def test_a_rename_that_fails_for_good_fails_the_item_and_leaves_no_part(world, server):
    def refuse(src, dst):
        raise OSError(5, "disk")
    result, code = go(world, server, [server.url("/ok")], deps={"rename": refuse})
    assert code == 1 and result["items"][0]["error"]["code"] == "OUTPUT_WRITE_FAILED"
    assert os.listdir(os.path.join(job_dir_of(world, result), ".tmp")) == []


def test_a_manifest_that_cannot_even_be_started_refuses_the_job(world, server):
    def refuse(src, dst):
        raise OSError(5, "disk")
    result, code = go(world, server, [server.url("/ok")], deps={"replace": refuse})
    assert code == 2 and result["job_id"] is None and result["error"]["code"] == "OUTPUT_WRITE_FAILED"


def test_the_run_log_is_best_effort(tmp_path):
    from agent_crawl_studio import runner
    (tmp_path / "job").mkdir()
    log = runner.RunLog(str(tmp_path / "job"), lambda path: None)
    os.rmdir(tmp_path / "job" / "logs")  # the folder disappears under it: writing is silently given up
    log.write("0001 succeeded - u")
    log.write("0002 succeeded - u")
    assert not os.path.exists(log.path)
    log.close()
