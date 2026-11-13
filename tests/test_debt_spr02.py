"""SPR-03 TK11 (FR16.2, FR16.3, FR16.6, SC21): debts left by SPR-02.

* the Ctrl+C sweep (`sys.settrace`, one injected KeyboardInterrupt per executed line) now also covers a batch of
  two items, the real `netfetch` code and the media path (FR16.3): the building blocks `run_media_item` +
  `clear_item_dir` with a fake spawner, and (TK14) the whole runner with a media item (`media_run`);
* `url_redacted` uses the normalised host (FR16.2); `cli._quiet` prints the warning class only and
  `validate_result` reports an unknown `status` (FR16.6).
"""

import gc
import inspect
import json
import os
import sys
import time
import warnings

from agent_crawl_studio import cli, errors, limits, netfetch, runner, schema, urlpolicy, ytdlp

from test_netfetch import policy_for, server  # noqa: F401
from test_run_batch import FAKES, request_of
from test_round10 import fake_fetch

SRC = os.path.dirname(inspect.getsourcefile(runner))
JOB_FILES = tuple(os.path.join(SRC, name) for name in ("runner.py", "manifest.py", "artifacts.py", "outroot.py"))
NET_FILES = (os.path.join(SRC, "netfetch.py"),)
MEDIA_FILES = (os.path.join(SRC, "ytdlp.py"), os.path.join(SRC, "jobobject.py"))
NOT_COUNTED = ("clear_item_dir",)  # the cleanup that runs after the interrupt: an interrupt inside it is RQ-011's open part


def tracer_for(watch, number, state, skip=()):
    def tracer(frame, event, arg):
        if event == "call":
            return tracer if frame.f_code.co_filename in watch and frame.f_code.co_name not in skip else None
        if event == "line" and state["fired"] is None:
            state["count"] += 1
            if state["count"] == number:
                state["fired"] = (os.path.basename(frame.f_code.co_filename), frame.f_code.co_name, frame.f_lineno)
                raise KeyboardInterrupt
        return tracer

    return tracer


def restore_trace(previous):
    """Put back the tracer that was active before the sweep. coverage's C tracer must be restarted, not handed to
    `sys.settrace` again: that would run it through the slow Python callback for the rest of the session."""
    if hasattr(previous, "start"):
        previous.start()
    else:
        sys.settrace(previous)


def found(root, pattern):
    return [str(p) for p in root.rglob(pattern)] if root.exists() else []


def leftovers(work):
    problems = []
    if found(work, "manifest.json.*.tmp"):
        problems.append("temp manifest left")
    if found(work, "*.part"):
        problems.append("part left")
    for tmp in found(work, ".tmp"):
        if os.path.isdir(tmp) and [n for n in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, n))]:
            problems.append("item folder left in .tmp")
    return problems


def job_problems(work, result, code):
    problems = []
    jobs = work / "jobs"
    if result["job_id"] is None and jobs.exists() and os.listdir(jobs):
        problems.append("job folder left with a null job id")
    if (code == 130) != (result["status"] == "cancelled"):
        problems.append("exit %s with status %s" % (code, result["status"]))
    if schema.validate_result(result):
        problems.append("result not valid: %r" % (schema.validate_result(result),))
    if result["job_id"] is not None:
        folder = jobs / result["job_id"]
        listed = {os.path.basename(a["path"]) for i in result["items"] for a in i["artifacts"]}
        present = set(os.listdir(folder / "files")) if (folder / "files").exists() else set()
        if present - listed:
            problems.append("a finished file the result does not list")
        if "manifest_not_updated" not in result["warnings"]:
            path = folder / "manifest.json"
            if not path.exists():
                problems.append("manifest missing")
            else:
                on_disk = json.loads(path.read_text(encoding="utf-8"))
                if on_disk != result or schema.validate_result(on_disk):
                    problems.append("manifest on disk differs from the result or is not valid")
    return problems


def slots_free():
    """True when every resolver slot is back within two seconds (a lookup thread may still be finishing)."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        got = [netfetch.RESOLVER_SLOTS.acquire(blocking=False) for _ in range(netfetch.MAX_RESOLVER_THREADS)]
        for taken in got:
            if taken:
                netfetch.RESOLVER_SLOTS.release()
        if all(got):
            return True
        time.sleep(0.05)
    return False


def sweep_job(tmp_path, scenario, number, server_=None):
    """One real `run` with a Ctrl+C at the `number`-th executed line of the watched files; (state, problems)."""
    work = tmp_path / ("w_%s_%d" % (scenario, number))
    env = {"CRAWL_STUDIO_WORK": str(work), "OPCOS_ROOT": str(work / "o"), "USERPROFILE": str(work / "h")}
    state = {"count": 0, "fired": None}
    if scenario == "media_run":  # the whole runner with the media path: fake engine process, fake tools and DNS
        request = request_of(["https://www.youtube.com/watch?v=jNQXAC9IVRw"], mode="media")
        deps = dict(FAKES, sleep=lambda seconds: None, which=lambda cmd, path=None: "C:\\tools\\%s.exe" % cmd,
                    version_of=lambda name: "1", dns_probe=lambda host: urlpolicy.DNS_OK, spawn=fake_media_spawn,
                    kill_tree=lambda job, proc: True, engine_prefix=lambda: ["fake-engine"], poll=0, environ={})
        watch = JOB_FILES[:1] + MEDIA_FILES[:1]
    elif scenario == "netfetch":
        request = request_of([server_.url("/ok")])
        deps = dict(FAKES, policy=policy_for(server_), sleep=lambda seconds: None)
        watch = NET_FILES
    else:
        urls = ["https://a.example/x"] + (["https://b.example/y"] if scenario == "two_items" else [])
        request = request_of(urls)
        deps = dict(FAKES, fetch=fake_fetch, sleep=lambda seconds: None)
        watch = JOB_FILES[:1]  # runner.py: the item loop and its boundaries; the other modules are swept by test_round10
    previous = sys.gettrace()  # coverage's own tracer, put back afterwards
    escaped, result, code = False, None, None
    sys.settrace(tracer_for(watch, number, state))
    try:
        result, code = runner.run(request, env, deps)
    except KeyboardInterrupt:
        escaped = True
    finally:
        restore_trace(previous)
    problems = leftovers(work)
    if scenario == "netfetch" and not slots_free():
        problems.append("resolver slot leaked")
    if escaped:
        problems.append("escaped")
    if result is not None:
        problems += job_problems(work, result, code)
    return state, problems


def sweep_all(tmp_path, scenario, server_=None, stride=1):
    baseline, problems = sweep_job(tmp_path, scenario, 10 ** 9, server_)
    assert problems == [] and baseline["count"] > 100, (problems, baseline)
    failures, escapes = [], []
    for number in range(1, baseline["count"] + 1, stride):
        state, problems = sweep_job(tmp_path, scenario, number, server_)
        if "escaped" in problems:
            escapes.append(state["fired"])
            problems.remove("escaped")
        if problems:
            failures.append((state["fired"], problems))
    assert failures == []
    # an interrupt may leave run() only from its own first lines, before anything exists
    assert all(fired[1] == "run" for fired in escapes) and len(escapes) <= 8, escapes


def test_sweep_two_items_leaves_nothing_behind(tmp_path):
    sweep_all(tmp_path, "two_items")


def test_sweep_netfetch_leaves_nothing_behind(tmp_path, server):
    sweep_all(tmp_path, "netfetch", server, stride=2)  # real sockets: every second line keeps the run short


# ----------------------------------------------------------------------------- the media path (fake process, fake clock)

class FakeProc:
    returncode = 0

    def poll(self):
        return 0


class FakeJob:
    closed = False

    def close(self):
        self.closed = True


def fake_media_spawn(argv, env, cwd, out, err, holder=None, limits=None):
    dl = [a for a in argv if a.startswith("home:")][0][5:]
    with open(os.path.join(dl, "media.mp4"), "wb") as handle:
        handle.write(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    out.write(b'{"title":"t","duration":1}\n')
    proc, job = FakeProc(), FakeJob()
    holder.append((proc, job))
    return proc, job


def media_once(tmp_path, number):
    """`run_media_item` + the cleanup the runner owes it, with a Ctrl+C at the `number`-th executed line."""
    job_dir = tmp_path / ("m%d" % number)
    job_dir.mkdir()
    state = {"count": 0, "fired": None}
    spawned, killed = [], []

    def spawn(argv, env, cwd, out, err, holder=None, limits=None):
        dl = [a for a in argv if a.startswith("home:")][0][5:]
        with open(os.path.join(dl, "media.mp4"), "wb") as handle:
            handle.write(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        out.write(b'{"title":"t","duration":1}\n')
        proc, job = FakeProc(), FakeJob()
        holder.append((proc, job))
        spawned.append(proc)
        return proc, job

    def kill(job, proc):
        killed.append(proc)
        return True

    dirs = ytdlp.make_item_dirs(str(job_dir), "0001", None)
    deps = {"engine_prefix": lambda: ["fake-engine"], "spawn": spawn, "kill_tree": kill, "sleep": lambda s: None,
            "poll": 0, "environ": {}}
    tools = ytdlp.Tools(r"C:\tools\ffmpeg\bin", r"C:\Program Files\nodejs\node.exe", "0")
    previous = sys.gettrace()
    escaped = False
    sys.settrace(tracer_for(MEDIA_FILES, number, state, skip=NOT_COUNTED))
    try:
        ytdlp.run_media_item("https://www.youtube.com/watch?v=jNQXAC9IVRw", "0001", dirs, limits.resolve({})[0],
                             tools, budget=10 ** 12, room=10 ** 12, clock=lambda: 0.0, deps=deps)
    except KeyboardInterrupt:
        escaped = True
    finally:
        restore_trace(previous)
    # Python 3.12 gives the implicit `__exit__` of a `with` the line of the `with` statement, so the sweep can land
    # between the body and the close: the file object then lives in the traceback until it is let go (a known limit of
    # the sweep, RQ-011), so the cleanup of the runner is simulated after the traceback is gone.
    gc.collect()
    ytdlp.clear_item_dir(dirs.root, None)
    problems = leftovers(job_dir)
    if any(proc not in killed for proc in spawned):
        problems.append("a spawned process was not ended")
    return state, problems, escaped


def test_sweep_media_through_the_runner_leaves_nothing_behind(tmp_path):
    sweep_all(tmp_path, "media_run", stride=3)  # every third line keeps the run short


def test_sweep_media_path_ends_the_tree_and_leaves_no_folder(tmp_path):
    baseline, problems, escaped = media_once(tmp_path, 10 ** 9)
    assert problems == [] and not escaped and baseline["count"] > 40, (problems, baseline)
    failures = []
    for number in range(1, baseline["count"] + 1):
        state, problems, _escaped = media_once(tmp_path, number)
        if problems:
            failures.append((state["fired"], problems))
    assert failures == []


# ----------------------------------------------------------------------------- FR16.2, FR16.6

def test_url_redacted_in_a_manifest_uses_the_normalised_host(tmp_path):
    work = tmp_path / "w"
    env = {"CRAWL_STUDIO_WORK": str(work), "OPCOS_ROOT": str(work / "o"), "USERPROFILE": str(work / "h")}
    request = request_of(["https://WWW.Example.COM./a?t=1"])
    result, _ = runner.run(request, env, dict(FAKES, fetch=fake_fetch, sleep=lambda seconds: None))
    assert result["items"][0]["url_redacted"] == "https://www.example.com/a?t=REDACTED"
    planned = runner.plan(request, env, dict(FAKES))
    assert planned["items"][0]["url_redacted"] == "https://www.example.com/a?t=REDACTED"


def test_a_url_refused_before_normalising_keeps_the_old_redaction(tmp_path):
    work = tmp_path / "w"
    env = {"CRAWL_STUDIO_WORK": str(work), "OPCOS_ROOT": str(work / "o"), "USERPROFILE": str(work / "h")}
    result, _ = runner.run(request_of(["https://user:pw@A.com/x?k=v"]), env, dict(FAKES, fetch=fake_fetch))
    assert result["items"][0]["status"] == "blocked"
    assert result["items"][0]["url_redacted"] == "https://A.com/x?k=REDACTED"


def test_quiet_prints_the_class_of_a_warning_never_its_text(capsys):
    with cli._quiet():
        warnings.warn("secret text")
    seen = capsys.readouterr()
    assert "UserWarning" in seen.err and "secret text" not in seen.err and seen.out == ""


def test_validate_result_reports_a_status_outside_the_set():
    doc = {key: None for key in schema.RESULT_KEYS}
    doc.update(items=[], counts={"total": 0}, warnings=[])
    assert schema.validate_result(dict(doc, status="weird")) == ["status"]
    for fine in [None] + list(errors.JOB_EXIT):
        assert schema.validate_result(dict(doc, status=fine)) == []


def test_resolve_gives_the_slot_back_when_the_thread_never_starts(monkeypatch):
    """A Ctrl+C between the taking of a resolver slot and the start of the lookup thread (found by the sweep)."""
    import threading

    class Boom(threading.Thread):
        def start(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(netfetch.threading, "Thread", Boom)
    backend = netfetch.PinnedBackend(netfetch.urlpolicy.Policy())
    try:
        backend._resolve("a.example", 443, 1.0)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("the interrupt must go on")
    assert slots_free()


# ----------------------------------------------------------------------------- SPR-03 TK28 (FR16.5, RQ-013 rest)
import io  # noqa: E402

import pytest  # noqa: E402

from conftest import remove_junctions, wait_for_resolver_slots  # noqa: E402
from test_run_batch import go, manifest_of, world  # noqa: E402,F401


class _Slots:
    """A stand-in for RESOLVER_SLOTS: `busy` of its `size` places are held and never come back."""

    def __init__(self, size, busy):
        self.free = size - busy

    def acquire(self, blocking=False):
        if self.free > 0:
            self.free -= 1
            return True
        return False

    def release(self):
        self.free += 1


def test_the_resolver_fixture_fails_the_test_when_a_slot_never_comes_back():
    ticks = iter(range(1000))
    with pytest.raises(pytest.fail.Exception, match="resolver slots busy"):
        wait_for_resolver_slots(_Slots(8, busy=1), 8, timeout=3, clock=lambda: next(ticks), sleep=lambda seconds: None)


def test_the_resolver_fixture_is_quiet_when_every_slot_is_free_or_comes_back():
    wait_for_resolver_slots(_Slots(8, busy=0), 8, timeout=3)
    late = _Slots(8, busy=1)
    seen = []

    def sleep(seconds):
        seen.append(seconds)
        late.release()  # the stuck lookup ends while the fixture waits

    wait_for_resolver_slots(late, 8, timeout=3, sleep=sleep)
    assert seen and late.free == 8


def test_validate_result_reports_more_items_than_the_count_and_fewer():
    doc = {key: None for key in schema.RESULT_KEYS}
    item = {key: None for key in schema.RESULT_ITEM_KEYS}
    doc.update(job_id="20261113-100000-aaaaaa", status="succeeded", counts={"total": 2}, warnings=[],
               items=[dict(item), dict(item), dict(item)])
    assert schema.validate_result(doc) == ["counts.total"]  # one item too many
    doc["items"] = [dict(item)]
    assert schema.validate_result(doc) == ["counts.total"]  # one too few, in a job that says it is finished
    doc["items"] = [dict(item), dict(item)]
    assert schema.validate_result(doc) == []
    doc["status"] = None  # a job still running lists the items it has so far
    doc["items"] = [dict(item)]
    assert schema.validate_result(doc) == []


@pytest.mark.parametrize("counts", ["four", [4], 4, True])
def test_validate_result_reports_counts_that_is_not_a_table(counts):
    doc = {key: None for key in schema.RESULT_KEYS}
    doc.update(job_id="20261113-100000-aaaaaa", status="succeeded", counts=counts, warnings=[], items=[])
    assert schema.validate_result(doc) == ["counts"]


def test_validate_result_does_not_trip_on_counts_without_a_total():
    doc = {key: None for key in schema.RESULT_KEYS}
    doc.update(job_id="20261113-100000-aaaaaa", status="succeeded", counts={}, warnings=[], items=[])
    assert schema.validate_result(doc) == []


def _interrupt_the_final_write(monkeypatch, times):
    """The first `times` writes of `finish` are cut by a Ctrl+C after the memory copy was set (a Ctrl+C in the write)."""
    from agent_crawl_studio import manifest
    real = manifest.ManifestWriter.finish
    seen = []

    def finish(self, status, finished_at, error=None, warnings=(), write=True):
        if write and len(seen) < times:
            seen.append(1)
            real(self, status, finished_at, error, warnings, write=False)
            raise KeyboardInterrupt
        return real(self, status, finished_at, error, warnings, write=write)

    monkeypatch.setattr(manifest.ManifestWriter, "finish", finish)


def test_a_third_ctrl_c_in_the_final_write_answers_cancelled_and_says_the_manifest_is_not_updated(world, server, monkeypatch, tmp_path):
    _interrupt_the_final_write(monkeypatch, 3)
    request = tmp_path / "request.json"
    request.write_bytes(json.dumps({"schema_version": 1, "urls": [server.url("/ok")]}).encode("utf-8"))
    out = io.StringIO()
    rc = cli.main(["run", "--request", str(request)], out=out, env=world["env"],
                  run_fn=lambda req, env: runner.run(req, env, dict(FAKES, policy=policy_for(server), sleep=lambda s: None)))
    assert out.getvalue().count("\n") == 1
    result = json.loads(out.getvalue())
    assert rc == 130 and result["status"] == "cancelled" and "manifest_not_updated" in result["warnings"]
    assert result["items"][0]["status"] == "succeeded" and manifest_of(world, result)["status"] is None  # the file is behind


def test_two_ctrl_c_in_the_final_write_still_end_with_the_file_written(world, server, monkeypatch):
    _interrupt_the_final_write(monkeypatch, 2)
    result, code = go(world, server, [server.url("/ok")])
    assert code == 130 and result["status"] == "cancelled" and "manifest_not_updated" not in result["warnings"]
    assert manifest_of(world, result) == result


# --- no junction is left behind by a test (RQ-013: three were) -----------------------------------------------------
def test_remove_junctions_takes_the_link_and_keeps_what_it_points_at(tmp_path):
    import _winapi
    target, plain = tmp_path / "target", tmp_path / "plain"
    target.mkdir()
    plain.mkdir()
    (target / "keep.txt").write_text("mine")
    link = tmp_path / "link"
    _winapi.CreateJunction(str(target), str(link))
    assert os.path.lexists(link)
    remove_junctions([str(link), str(plain), str(tmp_path / "missing")])
    assert not os.path.lexists(link) and (target / "keep.txt").read_text() == "mine"
    assert plain.is_dir()  # a folder that is not a junction is never touched


def _body_of(fixture):
    """The generator function behind a fixture, called by hand so that its teardown can be seen inside one test."""
    if hasattr(fixture, "_get_wrapped_function"):
        return fixture._get_wrapped_function()
    return getattr(fixture, "__wrapped__", fixture)


def test_a_test_that_forgets_its_junction_is_cleaned_up_by_the_fixture(tmp_path, monkeypatch):
    import _winapi
    import conftest
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("mine")
    link = tmp_path / "link"
    fixture = _body_of(conftest._no_junction_is_left_behind)(monkeypatch)
    next(fixture)
    _winapi.CreateJunction(str(target), str(link))  # no cleanup here on purpose
    assert os.path.lexists(link)
    with pytest.raises(StopIteration):
        next(fixture)  # the end of the test: the autouse fixture takes the link away
    assert not os.path.lexists(link)
    assert (target / "keep.txt").read_text() == "mine"  # and only the link
