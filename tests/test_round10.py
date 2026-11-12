"""The last hardening of Ctrl+C (round 10): a Ctrl+C is injected, deterministically, at EVERY executed line of the
modules that make a job (runner, manifest, artifacts, outroot) during one real `run`, and the invariants hold.

Invariants after each injected interrupt: `run()` does not raise (but for the few lines of `run` itself that come
before anything exists), no `manifest.json.*.tmp`, no `.part`, no job folder when `job_id` is null, exit 130 exactly
when the status is `cancelled`, no finished file the result does not list, and the manifest on disk equals the
result (unless the result says `manifest_not_updated`). Plus targeted cases for the paths the sweep cannot count
for coverage (the sweep replaces the tracer coverage uses)."""

import inspect
import json
import os
import sys

import pytest

from agent_crawl_studio import artifacts, manifest, netfetch, outroot, runner, schema

from test_run_batch import FAKES, files_of, go, job_dir_of, manifest_of, request_of, server, world  # noqa: F401
from test_round6 import interrupt, statuses

SRC = os.path.dirname(inspect.getsourcefile(runner))
WATCH = tuple(os.path.join(SRC, name) for name in ("runner.py", "manifest.py", "artifacts.py", "outroot.py"))


def fake_fetch(url, expect, limits, sink, policy, **kwargs):
    sink(b"hello world")
    return netfetch.FetchOutcome(status="ok", http_status=200, headers_clean={"content-type": "text/plain"}, final_url=url,
                                 final_url_redacted=url, bytes_received=11)


def sweep_once(tmp_path, number, label):
    """One run with a Ctrl+C at the `number`-th executed line of WATCH; returns (state, problems, result)."""
    work = tmp_path / ("w%s_%d" % (label, number))
    env = {"CRAWL_STUDIO_WORK": str(work), "OPCOS_ROOT": str(work / "o"), "USERPROFILE": str(work / "h")}
    request = request_of(["https://a.example/x"])
    state = {"count": 0, "fired": None}

    def tracer(frame, event, arg):
        if event == "call":
            return tracer if frame.f_code.co_filename in WATCH else None
        if event == "line" and state["fired"] is None:
            state["count"] += 1
            if state["count"] == number:
                state["fired"] = (os.path.basename(frame.f_code.co_filename), frame.f_code.co_name, frame.f_lineno)
                raise KeyboardInterrupt
        return tracer

    previous = sys.gettrace()  # coverage's own tracer, put back afterwards
    escaped, result, code = False, None, None
    sys.settrace(tracer)
    try:
        result, code = runner.run(request, env, dict(FAKES, fetch=fake_fetch, sleep=lambda seconds: None))
    except KeyboardInterrupt:
        escaped = True
    finally:
        sys.settrace(previous)
    problems = []
    found = lambda pattern: [str(p) for p in work.rglob(pattern)] if work.exists() else []  # noqa: E731
    if found("manifest.json.*.tmp"):
        problems.append("temp manifest left")
    if found("*.part"):
        problems.append("part left")
    if escaped:
        problems.append("escaped")
    if result is not None:
        jobs = work / "jobs"
        if result["job_id"] is None and jobs.exists() and os.listdir(jobs):
            problems.append("job folder left with a null job id")
        if (code == 130) != (result["status"] == "cancelled"):
            problems.append("exit %s with status %s" % (code, result["status"]))
        if result["job_id"] is not None:
            folder = jobs / result["job_id"]
            listed = {os.path.basename(a["path"]) for i in result["items"] for a in i["artifacts"]}
            present = set(os.listdir(folder / "files")) if (folder / "files").exists() else set()
            if present - listed:
                problems.append("a finished file the result does not list")
            if "manifest_not_updated" not in result["warnings"]:
                path = folder / "manifest.json"
                if not path.exists() or json.loads(path.read_text(encoding="utf-8")) != result:
                    problems.append("manifest on disk differs from the result")
    return state, problems, result


def test_a_ctrl_c_at_every_line_of_a_job_leaves_nothing_behind(tmp_path):
    baseline, problems, _ = sweep_once(tmp_path, 10 ** 9, "base")
    assert problems == [] and baseline["count"] > 500
    failures, escapes = [], []
    for number in range(1, baseline["count"] + 1):
        state, problems, _ = sweep_once(tmp_path, number, "n")
        if "escaped" in problems:
            escapes.append(state["fired"])
            problems.remove("escaped")
        if problems:
            failures.append((state["fired"], problems))
    assert failures == []
    # the only lines where Ctrl+C may leave run(): the first lines of run() itself, before anything exists
    assert escapes and all(fired[1] == "run" for fired in escapes) and len(escapes) <= 8, escapes


# ----------------------------------------------------------------------------- targeted, for coverage and for clarity
def kill_at_line(module, line_text, tmp_run):
    """Run `tmp_run()` with a Ctrl+C raised just before the first execution of the source line that holds `line_text`."""
    source, first = inspect.getsourcelines(module)
    target = (first or 1) + next(index for index, text in enumerate(source) if line_text in text)  # a module starts at 0
    path = inspect.getsourcefile(module)
    state = {"fired": False}

    def tracer(frame, event, arg):
        if event == "call":
            return tracer if frame.f_code.co_filename == path else None
        if event == "line" and frame.f_lineno == target and not state["fired"]:
            state["fired"] = True
            raise KeyboardInterrupt
        return tracer
    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        return tmp_run()
    finally:
        sys.settrace(previous)


def test_ctrl_c_between_the_mkdir_and_the_bookkeeping_of_create_job_takes_the_job_folder_away(world, server):
    result, code = kill_at_line(outroot, "made.append(job)", lambda: go(world, server, [server.url("/ok")]))
    assert code == 130 and result["job_id"] is None and result["status"] == "cancelled"
    jobs = os.path.join(world["work"], "jobs")
    assert not os.path.exists(jobs) or os.listdir(jobs) == []


def test_ctrl_c_just_after_create_job_returned_takes_the_job_folder_away(world, server):
    result, code = kill_at_line(runner, 'state["job_dir"] = job_dir', lambda: go(world, server, [server.url("/ok")]))
    assert code == 130 and result["job_id"] is None
    jobs = os.path.join(world["work"], "jobs")
    assert not os.path.exists(jobs) or os.listdir(jobs) == []


class InterruptingDeps(dict):
    """A deps dict that raises Ctrl+C when a given key is asked for."""

    def __init__(self, base, key):
        super().__init__(base)
        self.key = key

    def get(self, key, default=None):
        if key == self.key:
            raise KeyboardInterrupt
        return super().get(key, default)


def run_with(world, server_, key):
    from test_netfetch import policy_for
    deps = InterruptingDeps(dict(FAKES, policy=policy_for(server_), sleep=lambda seconds: None), key)
    return runner.run(request_of([server_.url("/ok")] * 2), world["env"], deps)


def test_the_safety_net_before_any_job_exists(world, server):
    result, code = run_with(world, server, "create_job")  # asked for after the output place is chosen, before the folder
    assert code == 130 and result["status"] == "cancelled" and result["job_id"] is None and result["error"]["code"] == "CANCELLED"
    assert not os.path.exists(os.path.join(world["work"], "jobs"))


def test_the_safety_net_with_a_job_folder_but_no_manifest(world, server):
    result, code = run_with(world, server, "sleep")  # asked for right after the job folder was made
    assert code == 130 and result["job_id"] is None and result["status"] == "cancelled"
    assert os.listdir(os.path.join(world["work"], "jobs")) == []


def test_the_safety_net_with_a_manifest_lists_every_item_as_cancelled(world, server, monkeypatch):
    class Boom:
        def __init__(self, *args, **kwargs):
            interrupt()
    monkeypatch.setattr(runner, "_Job", Boom)
    result, code = go(world, server, [server.url("/ok")] * 3)
    assert code == 130 and result["status"] == "cancelled" and statuses(result) == ["cancelled"] * 3
    assert manifest_of(world, result) == result and "manifest_not_updated" not in result["warnings"]


def test_the_safety_net_when_even_the_manifest_cannot_be_written_keeps_the_memory_copy(world, server, monkeypatch):
    calls = []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) > 1:
            raise OSError(5, "gone")
        os.replace(src, dst)

    class Boom:
        def __init__(self, *args, **kwargs):
            interrupt()
    monkeypatch.setattr(runner, "_Job", Boom)
    result, code = go(world, server, [server.url("/ok")] * 2, deps={"replace": replace})
    assert code == 130 and result["status"] == "cancelled" and "manifest_not_updated" in result["warnings"]
    assert statuses(result) == ["cancelled"] * 2


def test_a_job_that_had_already_ended_keeps_its_own_answer_when_the_interrupt_comes_last(world, server, monkeypatch):
    real = dict(runner.JOB_EXIT)
    state = {"first": True}

    class Exits(dict):
        def __getitem__(self, key):
            if state["first"]:
                state["first"] = False
                interrupt()
            return real[key]
    monkeypatch.setattr(runner, "JOB_EXIT", Exits())
    result, code = go(world, server, [server.url("/ok")])
    assert code == 0 and result["status"] == "succeeded" and manifest_of(world, result) == result


# ----------------------------------------------------------------------------- RF: no temp file, no open descriptor
def test_a_ctrl_c_between_the_creation_of_the_temp_file_and_its_handle_leaves_nothing(tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    monkeypatch.setattr(os, "fdopen", lambda fd, mode: interrupt())
    writer = manifest.ManifestWriter(str(job))
    with pytest.raises(KeyboardInterrupt):
        writer.start("20261113-120000-abcdef", "t", {}, 1)
    monkeypatch.undo()
    assert os.listdir(job) == []  # the descriptor was closed, so the temp file could be removed


def test_a_ctrl_c_at_every_line_of_the_manifest_write_leaves_nothing(tmp_path):
    source, first = inspect.getsourcelines(manifest.ManifestWriter._write)
    path = inspect.getsourcefile(manifest)
    for number in range(1, 40):
        job = tmp_path / ("job%d" % number)
        job.mkdir()
        writer = manifest.ManifestWriter(str(job))
        writer.doc = {"schema_version": 1, "kind": "result", "job_id": "x", "status": None, "started_at": None,
                      "finished_at": None, "counts": {}, "limits_effective": {}, "items": [], "warnings": []}
        seen = {"count": 0}

        def tracer(frame, event, arg):
            if event == "call":
                return tracer if frame.f_code.co_filename == path and frame.f_code.co_name == "_write" else None
            if event == "line":
                seen["count"] += 1
                if seen["count"] == number:
                    raise KeyboardInterrupt
            return tracer
        previous = sys.gettrace()
        sys.settrace(tracer)
        try:
            writer._write()
            finished = True
        except KeyboardInterrupt:
            finished = False
        finally:
            sys.settrace(previous)
        assert [name for name in os.listdir(job) if name != "manifest.json"] == [], number
        if finished:
            break


# ----------------------------------------------------------------------------- a handle left dangling by an interrupt
def test_a_finished_file_whose_zone_stream_handle_dangles_is_removed_at_the_end(world, server):
    def motw(path, url):
        handle = open(path + ":Zone.Identifier", "w", encoding="utf-8")  # still open when the interrupt arrives
        interrupt()
        handle.write("x")
    result, code = go(world, server, [server.url("/ok")] * 2, deps={"motw": motw})
    assert code == 130 and statuses(result) == ["cancelled"] * 2
    assert files_of(world, result) == [] and manifest_of(world, result) == result


def test_artifact_writer_reconcile_discard_and_sweep(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    writer = artifacts.ArtifactWriter(str(job))
    for name in ("0001-aaaa.pdf", "0002-bbbb.txt", "0003-cccc.bin"):
        (job / "files" / name).write_bytes(b"x")
    (job / ".tmp" / "0004.part").write_bytes(b"y")
    (job / ".tmp" / "keep.txt").write_bytes(b"z")
    writer.sweep()
    assert os.listdir(job / ".tmp") == ["keep.txt"]
    writer.discard(["0002"])
    assert sorted(os.listdir(job / "files")) == ["0001-aaaa.pdf", "0003-cccc.bin"]
    writer.reconcile({"0001-aaaa.pdf"})
    assert os.listdir(job / "files") == ["0001-aaaa.pdf"]
