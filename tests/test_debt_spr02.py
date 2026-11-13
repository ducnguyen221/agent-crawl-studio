"""SPR-03 TK11 (FR16.2, FR16.3, FR16.6, SC21): debts left by SPR-02.

* the Ctrl+C sweep (`sys.settrace`, one injected KeyboardInterrupt per executed line) now also covers a batch of
  two items, the real `netfetch` code and the media path (FR16.3). The media scenario sweeps the building blocks
  `run_media_item` + `clear_item_dir` with a fake spawner and a fake clock: the runner is wired to the media path in
  TK14, where `tests/test_run_media.py` covers the whole item;
* `url_redacted` uses the normalised host (FR16.2); `cli._quiet` prints the warning class only and
  `validate_result` reports an unknown `status` (FR16.6).
"""

import gc
import inspect
import json
import os
import sys
import warnings

from agent_crawl_studio import cli, errors, limits, runner, schema, ytdlp

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


def sweep_job(tmp_path, scenario, number, server_=None):
    """One real `run` with a Ctrl+C at the `number`-th executed line of the watched files; (state, problems)."""
    work = tmp_path / ("w_%s_%d" % (scenario, number))
    env = {"CRAWL_STUDIO_WORK": str(work), "OPCOS_ROOT": str(work / "o"), "USERPROFILE": str(work / "h")}
    state = {"count": 0, "fired": None}
    if scenario == "netfetch":
        request = request_of([server_.url("/ok")])
        deps = dict(FAKES, policy=policy_for(server_), sleep=lambda seconds: None)
        watch = NET_FILES
    else:
        urls = ["https://a.example/x"] + (["https://b.example/y"] if scenario == "two_items" else [])
        request = request_of(urls)
        deps = dict(FAKES, fetch=fake_fetch, sleep=lambda seconds: None)
        watch = JOB_FILES
    previous = sys.gettrace()  # coverage's own tracer, put back afterwards
    escaped, result, code = False, None, None
    sys.settrace(tracer_for(watch, number, state))
    try:
        result, code = runner.run(request, env, deps)
    except KeyboardInterrupt:
        escaped = True
    finally:
        sys.settrace(previous)
    problems = leftovers(work)
    if escaped:
        problems.append("escaped")
    if result is not None:
        problems += job_problems(work, result, code)
    return state, problems


def sweep_all(tmp_path, scenario, server_=None):
    baseline, problems = sweep_job(tmp_path, scenario, 10 ** 9, server_)
    assert problems == [] and baseline["count"] > 100, (problems, baseline)
    failures, escapes = [], []
    for number in range(1, baseline["count"] + 1):
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
    sweep_all(tmp_path, "netfetch", server)


# ----------------------------------------------------------------------------- the media path (fake process, fake clock)

class FakeProc:
    returncode = 0

    def poll(self):
        return 0


class FakeJob:
    closed = False

    def close(self):
        self.closed = True


def media_once(tmp_path, number):
    """`run_media_item` + the cleanup the runner owes it, with a Ctrl+C at the `number`-th executed line."""
    job_dir = tmp_path / ("m%d" % number)
    job_dir.mkdir()
    state = {"count": 0, "fired": None}
    spawned, killed = [], []

    def spawn(argv, env, cwd, out, err, holder=None):
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
        sys.settrace(previous)
    # Python 3.12 gives the implicit `__exit__` of a `with` the line of the `with` statement, so the sweep can land
    # between the body and the close: the file object then lives in the traceback until it is let go (a known limit of
    # the sweep, RQ-011), so the cleanup of the runner is simulated after the traceback is gone.
    gc.collect()
    ytdlp.clear_item_dir(dirs.root, None)
    problems = leftovers(job_dir)
    if any(proc not in killed for proc in spawned):
        problems.append("a spawned process was not ended")
    return state, problems, escaped


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
