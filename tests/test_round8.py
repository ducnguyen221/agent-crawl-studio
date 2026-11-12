"""Review of round 7 (RF200-RF208) and of the real-network acceptance (TK17): a Ctrl+C after the file was placed
removes it, the final write cannot leave a stale or empty status, an interrupted job set-up is `cancelled` (130 goes
with `cancelled` only) and is always cleaned up, nothing is left by an interrupt between open and Part, a handed-over
client can neither authenticate nor add headers, `plan` survives an OSError, HTTP 403 is not an authentication
problem and a bad certificate is not worth a retry."""

import json
import os
import threading
import time

import httpx
import pytest

from agent_crawl_studio import artifacts, errors, manifest, outroot, runner, schema
from agent_crawl_studio.urlpolicy import Policy

from test_netfetch import Resolver, Sink, failed, fetch, lim, nf, policy_for, tls_server  # noqa: F401
from test_netfetch import server as netfetch_server  # noqa: F401
from test_run_batch import FAKES, files_of, go, job_dir_of, manifest_of, request_of, server, world  # noqa: F401
from test_round6 import interrupt, statuses

NOW = None


# ----------------------------------------------------------------------------- RF200
def test_ctrl_c_after_the_file_was_placed_removes_the_file(world, server, monkeypatch):
    calls = []
    real = manifest.clean_untrusted

    def clean(values):
        calls.append(values)
        if len(calls) == 1:
            interrupt()
        return real(values)
    monkeypatch.setattr(runner.manifest, "clean_untrusted", clean)
    result, code = go(world, server, [server.url("/ok")] * 2)
    assert code == 130 and statuses(result) == ["cancelled"] * 2
    assert files_of(world, result) == [] and os.listdir(os.path.join(job_dir_of(world, result), ".tmp")) == []
    assert manifest_of(world, result) == result


# ----------------------------------------------------------------------------- RF201
def test_ctrl_c_in_every_try_of_the_final_write_still_ends_with_a_status_and_a_warning(world, server):
    calls = []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) >= 3:  # every write after the item's own
            interrupt()
        os.replace(src, dst)
    result, code = go(world, server, [server.url("/ok")], deps={"replace": replace})
    assert code == 130 and result["status"] == "cancelled" and result["finished_at"]
    assert "manifest_not_updated" in result["warnings"] and schema.validate_result(result) == []


def test_ctrl_c_before_the_status_is_even_set_never_gives_a_key_error(world, server, monkeypatch):
    real = manifest.ManifestWriter.finish
    calls = []

    def finish(self, *args, **kwargs):
        calls.append(1)
        if len(calls) <= 3:
            interrupt()
        return real(self, *args, **kwargs)
    monkeypatch.setattr(manifest.ManifestWriter, "finish", finish)
    result, code = go(world, server, [server.url("/ok")])
    assert code == 130 and result["status"] == "cancelled" and "manifest_not_updated" in result["warnings"]


# ----------------------------------------------------------------------------- RF202 / RF203
def test_ctrl_c_in_the_check_of_the_new_job_folder_leaves_no_folder_and_is_cancelled(world, server):
    job_name = "20261113-120000-abcdef"
    from datetime import datetime, timezone

    def attrs(path):
        if os.path.basename(path) == job_name:
            interrupt()
    result, code = go(world, server, [server.url("/ok")], deps={
        "attrs_of": attrs, "now": lambda: datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc), "token_of": lambda: "abcdef"})
    assert code == 130 and result["status"] == "cancelled" and result["error"]["code"] == "CANCELLED" and result["job_id"] is None
    assert os.listdir(os.path.join(world["work"], "jobs")) == []


def test_a_second_ctrl_c_in_the_cleanup_still_ends_cancelled_and_finishes_the_cleanup(world, server, monkeypatch):
    seen = []
    real = os.rmdir

    def rmdir(path, *args, **kwargs):
        seen.append(path)
        if len(seen) == 1:
            interrupt()
        return real(path, *args, **kwargs)

    def replace(src, dst):
        interrupt()  # the first Ctrl+C: during the set-up of the manifest
    monkeypatch.setattr(os, "rmdir", rmdir)
    result, code = go(world, server, [server.url("/ok")], deps={"replace": replace})
    monkeypatch.undo()
    assert code == 130 and result["status"] == "cancelled" and result["job_id"] is None
    jobs = os.path.join(world["work"], "jobs")
    job = [name for name in os.listdir(jobs)]
    leftovers = os.listdir(os.path.join(jobs, job[0])) if job else []
    assert ".tmp" not in leftovers and "files" not in leftovers  # what could be removed was removed, in spite of the interrupt


# ----------------------------------------------------------------------------- RF204
def test_an_interrupt_between_open_and_part_leaves_no_part_and_no_handle(tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    writer = artifacts.ArtifactWriter(str(job))

    def part(*args, **kwargs):
        interrupt()
    monkeypatch.setattr(artifacts, "Part", part)
    with pytest.raises(KeyboardInterrupt):
        writer.open("0001")
    monkeypatch.undo()
    assert os.listdir(job / ".tmp") == []  # also proves the handle was closed: Windows refuses to delete an open file


# ----------------------------------------------------------------------------- RF205 / RF207
def test_a_handed_over_client_cannot_authenticate_or_add_headers(nf, no_socket):
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, headers={"Content-Length": "2"}, stream=httpx.ByteStream(b"ok"))
    client = httpx.Client(transport=httpx.MockTransport(handler), auth=("user", "secret"), headers={
        "Proxy-Authorization": "Basic leak", "X-Custom": "1", "User-Agent": "evil/1", "Accept-Encoding": "gzip"})
    out = nf.fetch_item("https://files.example.com/a", "auto", lim(), Sink(), Policy(resolver=Resolver()), client=client)
    assert out.status == "ok" and len(seen) == 1
    got = seen[0]
    assert set(got) <= {"host", "user-agent", "accept", "accept-encoding", "connection"}
    assert got["user-agent"] == nf.USER_AGENT and got["accept-encoding"] == "identity"
    assert "secret" not in json.dumps(got) and "leak" not in json.dumps(got)


# ----------------------------------------------------------------------------- RF206
def test_plan_survives_an_os_error_while_choosing_the_place(world, server):
    def free(path):
        raise OSError("probe")
    request = request_of([server.url("/ok")])
    result = runner.plan(request, world["env"], dict(FAKES, free_bytes_of=free, policy=policy_for(server)))
    assert result["valid"] is False and result["error"]["code"] == "OUTPUT_WRITE_FAILED"
    assert errors.EXIT_FOR[result["error"]["code"]] == 2


# ----------------------------------------------------------------------------- real-network findings: 403 and certificates
def test_http_403_is_a_forbidden_network_error_not_an_authentication_problem(nf, netfetch_server):
    out, _ = fetch(nf, netfetch_server.url("/status/403"), policy_for(netfetch_server))
    failed(out, "NETWORK_ERROR", "forbidden", retryable=False, http_status=403)


@pytest.mark.parametrize("code", [401, 407])
def test_401_and_407_stay_authentication_required(nf, netfetch_server, code):
    out, _ = fetch(nf, netfetch_server.url("/status/%d" % code), policy_for(netfetch_server))
    failed(out, "AUTH_REQUIRED", retryable=False, http_status=code)


def test_a_403_in_a_batch_is_listed_with_its_reason(world, server):
    result, _ = go(world, server, [server.url("/status/403")])
    assert result["items"][0]["error"] == {"code": "NETWORK_ERROR", "message": errors.MESSAGES["NETWORK_ERROR"],
                                           "retryable": False, "reason": "forbidden", "http_status": 403}


def test_a_certificate_that_does_not_verify_is_not_retryable(nf, tls_server):
    srv, _crt = tls_server
    out, _ = fetch(nf, srv.url("/ok"), policy_for(srv))
    failed(out, "NETWORK_ERROR", "tls_verify_failed", retryable=False)
