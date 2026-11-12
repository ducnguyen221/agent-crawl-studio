"""Last round (RF220-RF226): a Ctrl+C in the last gap after a file was placed, in the closing steps, and in each
retried step of the cleanup; Host, Accept and Connection of a handed-over client; Ctrl+C in the levels above the job
folder."""

import os

import httpx
import pytest

from agent_crawl_studio import artifacts, manifest, runner, schema
from agent_crawl_studio.urlpolicy import Policy

from test_netfetch import Resolver, Sink, lim, nf  # noqa: F401
from test_run_batch import files_of, go, job_dir_of, manifest_of, server, world  # noqa: F401
from test_round6 import interrupt, statuses


# ----------------------------------------------------------------------------- RF220
def test_ctrl_c_in_the_gap_after_commit_returns_still_removes_the_file(world, server, monkeypatch):
    real = artifacts.ArtifactWriter.commit
    calls = []

    def commit(self, part, **kwargs):
        result = real(self, part, **kwargs)
        calls.append(result)
        if len(calls) == 1:
            interrupt()  # the file is placed and commit has returned: the caller has not even stored the answer
        return result
    monkeypatch.setattr(artifacts.ArtifactWriter, "commit", commit)
    result, code = go(world, server, [server.url("/ok")] * 2)
    assert code == 130 and statuses(result) == ["cancelled"] * 2
    assert files_of(world, result) == [] and manifest_of(world, result) == result


# ----------------------------------------------------------------------------- RF221
def test_ctrl_c_while_the_run_log_is_closed_is_not_the_end_of_the_job(world, server, monkeypatch):
    real = runner.RunLog.close
    calls = []

    def close(self):
        calls.append(1)
        if len(calls) == 1:
            interrupt()
        return real(self)
    monkeypatch.setattr(runner.RunLog, "close", close)
    result, code = go(world, server, [server.url("/ok")])
    assert code == 0 and result["status"] == "succeeded" and manifest_of(world, result) == result


def test_ctrl_c_in_every_closing_step_still_gives_a_status_and_a_code(world, server, monkeypatch):
    def finish(self, *args, **kwargs):
        interrupt()
    monkeypatch.setattr(manifest.ManifestWriter, "finish", finish)
    result, code = go(world, server, [server.url("/ok")])
    assert code == 130 and result["status"] == "cancelled" and result["finished_at"]


# ----------------------------------------------------------------------------- RF223
def test_a_second_ctrl_c_in_the_cleanup_retries_that_step_so_nothing_is_left(world, server, monkeypatch):
    seen = []
    real = os.rmdir

    def rmdir(path, *args, **kwargs):
        seen.append(path)
        if len(seen) == 1:
            interrupt()  # the first folder to be removed is interrupted
        return real(path, *args, **kwargs)

    def replace(src, dst):
        interrupt()  # the first Ctrl+C: while the manifest is being set up
    monkeypatch.setattr(os, "rmdir", rmdir)
    result, code = go(world, server, [server.url("/ok")], deps={"replace": replace})
    monkeypatch.undo()
    assert code == 130 and result["status"] == "cancelled" and result["job_id"] is None
    jobs = os.path.join(world["work"], "jobs")
    assert os.listdir(jobs) == []  # not one folder of the refused job is left


# ----------------------------------------------------------------------------- RF224
def test_a_handed_over_client_cannot_change_host_accept_or_connection(nf, no_socket):
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, headers={"Content-Length": "2"}, stream=httpx.ByteStream(b"ok"))
    client = httpx.Client(transport=httpx.MockTransport(handler), headers={
        "Host": "evil.internal", "Connection": "Upgrade, X-Secret", "Accept": "x/y", "Upgrade": "websocket"})
    out = nf.fetch_item("https://files.example.com/a", "auto", lim(), Sink(), Policy(resolver=Resolver()), client=client)
    assert out.status == "ok" and len(seen) == 1
    assert seen[0]["host"] == "files.example.com" and seen[0]["accept"] == "*/*" and seen[0]["connection"] == "keep-alive"
    assert "upgrade" not in seen[0] and "evil" not in repr(seen[0]) and "X-Secret" not in repr(seen[0])


def test_the_host_header_carries_a_non_default_port(nf, no_socket):
    seen = []

    def handler(request):
        seen.append(request.headers["host"])
        return httpx.Response(200, headers={"Content-Length": "2"}, stream=httpx.ByteStream(b"ok"))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    nf.fetch_item("https://files.example.com:8443/a", "auto", lim(), Sink(), Policy(ports=(8443,), resolver=Resolver()), client=client)
    assert seen == ["files.example.com:8443"]


# ----------------------------------------------------------------------------- RF226
def test_ctrl_c_in_a_level_above_the_job_folder_takes_those_levels_away_again(world, server):
    def attrs(path):
        if path.endswith(os.sep + "jobs") and os.path.isdir(path):  # only while create_job walks down, after the mkdir
            interrupt()
    result, code = go(world, server, [server.url("/ok")], deps={"attrs_of": attrs})
    assert code == 130 and result["status"] == "cancelled" and result["job_id"] is None
    assert not os.path.exists(os.path.join(world["work"], "jobs"))  # the level this call made is gone again


def test_ctrl_c_while_the_output_place_is_chosen_is_cancelled_not_an_escape(world, server):
    calls = []

    def attrs(path):
        calls.append(path)
        interrupt()
    result, code = go(world, server, [server.url("/ok")], deps={"attrs_of": attrs})
    assert code == 130 and result["status"] == "cancelled" and result["job_id"] is None and result["error"]["code"] == "CANCELLED"
    assert schema.validate_result(result) == []


def test_a_second_ctrl_c_in_closing_the_log_is_given_up_on(world, server, monkeypatch):
    monkeypatch.setattr(runner.RunLog, "close", lambda self: interrupt())
    result, code = go(world, server, [server.url("/ok")])
    assert code == 0 and result["status"] == "succeeded"


def test_the_cleanup_gives_up_a_step_after_two_interrupts_and_still_does_the_rest(world, server, monkeypatch):
    seen = []
    real = os.rmdir

    def rmdir(path, *args, **kwargs):
        seen.append(path)
        if len(seen) <= 2:
            interrupt()  # the same step, interrupted twice
        return real(path, *args, **kwargs)

    def replace(src, dst):
        interrupt()
    monkeypatch.setattr(os, "rmdir", rmdir)
    result, code = go(world, server, [server.url("/ok")], deps={"replace": replace})
    monkeypatch.undo()
    assert code == 130 and result["status"] == "cancelled"
    leftovers = os.listdir(os.path.join(world["work"], "jobs", os.listdir(os.path.join(world["work"], "jobs"))[0]))
    assert ".tmp" not in leftovers and "files" not in leftovers
