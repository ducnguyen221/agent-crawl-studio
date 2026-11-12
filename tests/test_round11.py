"""Round 11 (RF300-RF345): no TLS key log, no cleanup through a junction, every layer of the Ctrl+C cleanup proven on
its own (each is switched off in turn), a retried safety net, no item lost, and a validator that counts items."""

import os
import re

import pytest

from agent_crawl_studio import artifacts, manifest, outroot, runner, schema
from agent_crawl_studio.winpath import PathBlocked

from test_netfetch import nf, policy_for, tls_server, trusting, fetch  # noqa: F401
from test_round6 import interrupt, statuses
from test_round10 import kill_at_line
from test_run_batch import files_of, go, job_dir_of, manifest_of, server, world  # noqa: F401

import httpx

VALID_NAME = "0001-0123456789.pdf"


# ----------------------------------------------------------------------------- RF300
def test_the_tls_key_log_is_switched_off_whatever_the_environment_says(nf, tls_server, monkeypatch, tmp_path):
    srv, crt = tls_server
    keylog = tmp_path / "keys.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog))
    seen = []
    real = httpx.create_ssl_context

    def make(*args, **kwargs):
        ctx = real(*args, **kwargs)
        ctx.load_verify_locations(cafile=crt)
        seen.append(ctx)
        return ctx
    monkeypatch.setattr(httpx, "create_ssl_context", make)
    out, sink = fetch(nf, srv.url("/ok"), policy_for(srv))
    assert out.status == "ok" and sink.data == b"hello"
    assert seen and all(ctx.keylog_filename is None for ctx in seen)
    assert not keylog.exists() or keylog.read_text(encoding="utf-8") == ""  # no session secret was ever written


# ----------------------------------------------------------------------------- RF301
def real_check(job):
    root = outroot.Root(str(job), str(job))
    return lambda path: outroot.post_create_check(path, root)


def junction_to(link, target):
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


def test_the_cleanup_never_deletes_through_a_junction_in_place_of_files(tmp_path):
    job, victim = tmp_path / "job", tmp_path / "victim"
    job.mkdir()
    victim.mkdir()
    writer = artifacts.ArtifactWriter(str(job), check=real_check(job))
    for name in (VALID_NAME, "0002-abcdefabcd.txt"):
        (victim / name).write_bytes(b"precious")
    os.rmdir(job / "files")
    junction_to(job / "files", victim)  # the folder was swapped for a link to somebody else's folder
    writer.reconcile(set())
    writer.discard(["0001", "0002"])
    writer.sweep()
    assert sorted(os.listdir(victim)) == ["0001-0123456789.pdf", "0002-abcdefabcd.txt"]


def test_the_cleanup_never_deletes_through_a_junction_in_place_of_tmp(tmp_path):
    job, victim = tmp_path / "job", tmp_path / "victim"
    job.mkdir()
    victim.mkdir()
    writer = artifacts.ArtifactWriter(str(job), check=real_check(job))
    (victim / "0001.part").write_bytes(b"precious")
    os.rmdir(job / ".tmp")
    junction_to(job / ".tmp", victim)
    writer.sweep()
    assert os.listdir(victim) == ["0001.part"]


def test_the_cleanup_only_touches_names_it_could_have_made(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    writer = artifacts.ArtifactWriter(str(job))
    mine = ["0001-0123456789.pdf", "0002-abcdef0123-2.txt"]
    others = ["notes.txt", "0003-short.pdf", "0004-0123456789ABC.pdf", "readme", "0001-0123456789.pdf.bak", "00001-0123456789.pdf"]
    for name in mine + others:
        (job / "files" / name).write_bytes(b"x")
    for name in ("0005.part", "0005.part.keep", "x0005.part", "keep.txt"):
        (job / ".tmp" / name).write_bytes(b"x")
    writer.reconcile(set())
    writer.discard(["0003", "0004"])
    writer.sweep()
    assert sorted(os.listdir(job / "files")) == sorted(others)
    assert sorted(os.listdir(job / ".tmp")) == ["0005.part.keep", "keep.txt", "x0005.part"]


# ----------------------------------------------------------------------------- RF340: every layer works on its own
def only(monkeypatch, *disabled):
    """Switch the named cleanup layers off (the others stay)."""
    for name in disabled:
        monkeypatch.setattr(artifacts.ArtifactWriter, name, lambda self, *args, **kwargs: None)


def second_item_unrecorded(world, server_, monkeypatch):
    real = runner._record
    calls = []

    def record(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            interrupt()
        return real(*args, **kwargs)
    monkeypatch.setattr(runner, "_record", record)
    return go(world, server_, [server_.url("/ok")] * 3)


def test_discard_alone_removes_the_file_of_an_item_that_never_reached_the_manifest(world, server, monkeypatch):
    only(monkeypatch, "reconcile")
    result, code = second_item_unrecorded(world, server, monkeypatch)
    assert code == 130 and len(files_of(world, result)) == 1


def test_reconcile_alone_removes_the_file_of_an_item_that_never_reached_the_manifest(world, server, monkeypatch):
    only(monkeypatch, "discard")
    result, code = second_item_unrecorded(world, server, monkeypatch)
    assert code == 130 and len(files_of(world, result)) == 1


def test_the_placed_path_alone_removes_the_file_when_commit_has_returned(world, server, monkeypatch):
    only(monkeypatch, "discard", "reconcile")
    real = artifacts.ArtifactWriter.commit
    calls = []

    def commit(self, part, **kwargs):
        result = real(self, part, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            interrupt()
        return result
    monkeypatch.setattr(artifacts.ArtifactWriter, "commit", commit)
    result, code = go(world, server, [server.url("/ok")])
    assert code == 130 and files_of(world, result) == []


def test_place_alone_takes_back_a_file_whose_rename_was_interrupted_afterwards(world, server, monkeypatch):
    only(monkeypatch, "discard", "reconcile")

    def rename_then_interrupt(src, dst):
        os.rename(src, dst)
        interrupt()
    result, code = go(world, server, [server.url("/ok")], deps={"rename": rename_then_interrupt})
    assert code == 130 and files_of(world, result) == []


def test_create_job_alone_takes_the_job_folder_away_when_the_interrupt_comes_before_the_bookkeeping(world, server, monkeypatch):
    monkeypatch.setattr(runner, "_undo_job", lambda job_dir: False)  # the runner's own cleanup is switched off
    result, code = kill_at_line(outroot, "made.append(job)", lambda: go(world, server, [server.url("/ok")]))
    assert code == 130 and result["job_id"] is None
    jobs = os.path.join(world["work"], "jobs")
    assert not os.path.exists(jobs) or os.listdir(jobs) == []


def test_an_existing_but_empty_job_folder_is_not_removed_by_an_interrupted_create(world, server):
    from datetime import datetime, timezone
    taken = os.path.join(world["work"], "jobs", "20261113-120000-abcdef")
    os.makedirs(taken)  # EMPTY: os.rmdir would remove it, so only the `existed` test protects it

    def create(root, job_id, **kwargs):
        interrupt()
    result, code = go(world, server, [server.url("/ok")], deps={
        "create_job": create, "token_of": lambda: "abcdef", "now": lambda: datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)})
    assert code == 130 and os.path.isdir(taken)


# ----------------------------------------------------------------------------- RF341: the safety net is retried
@pytest.mark.parametrize("times", [1, 2])
def test_a_ctrl_c_inside_the_safety_net_is_taken_and_the_step_repeated(world, server, monkeypatch, times):
    class Boom:
        def __init__(self, *args, **kwargs):
            interrupt()
    monkeypatch.setattr(runner, "_Job", Boom)
    real = artifacts.ArtifactWriter.sweep
    calls = []

    def sweep(self):
        calls.append(1)
        if len(calls) <= times:
            interrupt()
        return real(self)
    monkeypatch.setattr(artifacts.ArtifactWriter, "sweep", sweep)
    result, code = go(world, server, [server.url("/ok")] * 2)
    assert code == 130 and result["status"] == "cancelled" and statuses(result) == ["cancelled"] * 2
    assert manifest_of(world, result) == result and len(calls) == times + 1


# ----------------------------------------------------------------------------- RF345: no item is lost
def test_two_interrupts_in_a_row_while_an_item_is_added_do_not_lose_the_item(world, server, monkeypatch):
    real = manifest.ManifestWriter.add_item
    calls = []

    def add_item(self, item, write=True):
        calls.append(1)
        if len(calls) in (2, 3):  # item 1: interrupted before it is even appended, twice
            interrupt()
        return real(self, item, write=write)
    monkeypatch.setattr(manifest.ManifestWriter, "add_item", add_item)
    result, code = go(world, server, [server.url("/ok")] * 2)
    assert code == 130 and len(result["items"]) == 2 == result["counts"]["total"]
    assert schema.validate_result(result) == []


def test_a_finished_result_must_list_every_item():
    doc = {"schema_version": 1, "kind": "result", "job_id": "20261113-120000-abcdef", "status": "succeeded", "started_at": "t",
           "finished_at": "t", "counts": {"total": 2}, "limits_effective": {}, "items": [], "warnings": []}
    assert schema.validate_result(doc) == ["counts.total"]
    doc["status"] = None  # still running: items come one by one
    assert schema.validate_result(doc) == []
    doc.update(status="failed", job_id=None)  # refused before it started: no items, the total is what was asked for
    assert schema.validate_result(doc) == []


# ----------------------------------------------------------------------------- RF343: no slot left over by a test
def test_a_slow_resolver_then_the_bound_test_in_either_order(nf):
    import threading
    import time
    from agent_crawl_studio.urlpolicy import Policy
    from test_netfetch import Resolver
    release = threading.Event()
    resolver = Resolver({"slow.example": [lambda: (release.wait(0.8), ["127.0.0.1"])[1]]})
    backend = nf.PinnedBackend(Policy(ports=(80,), ip_ok=lambda a: True, resolver=resolver))
    with pytest.raises(nf.NetError):
        backend.connect_tcp("slow.example", 80, timeout=0.05)  # leaves one lookup running for a while
    release.set()


def test_the_next_test_still_finds_every_resolver_slot_free(nf):
    got = [nf.RESOLVER_SLOTS.acquire(blocking=False) for _ in range(nf.MAX_RESOLVER_THREADS)]
    for ok in got:
        if ok:
            nf.RESOLVER_SLOTS.release()
    assert all(got)
