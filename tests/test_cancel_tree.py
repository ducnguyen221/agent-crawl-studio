"""SPR-03 TK7 (FR12.2, FR12.3, FR12.6, SC15): a three-level fake process tree dies with the item. Real processes
(the fixture `fake_ytdlp.py`, scenario `tree3`), real Job Object; every PID the tree wrote is checked dead, and a
teardown net ends by PID anything left (never by image name)."""

import _thread
import os
import subprocess
import sys
import threading
import time

import pytest

from agent_crawl_studio import jobobject, limits, ytdlp
from fixtures import procs

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "fixtures", "fake_ytdlp.py")
DRIVER = os.path.join(HERE, "fixtures", "media_driver.py")
SRC = os.path.dirname(os.path.dirname(os.path.abspath(ytdlp.__file__)))
URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
TOOLS = ytdlp.Tools(r"C:\tools\ffmpeg\bin", r"C:\Program Files\nodejs\node.exe", "2026.10.26")
LIMITS = limits.resolve({})[0]


@pytest.fixture
def item(tmp_path):
    dirs = ytdlp.make_item_dirs(str(tmp_path), "0001", None)
    yield dirs
    stragglers = procs.reap(procs.read_pids(dirs.io))
    assert not stragglers, "processes left alive: %r" % (stragglers,)


def run_tree(dirs, deps_extra=None, limits_in=None, clock=time.monotonic):
    deps = {"engine_prefix": lambda: [sys.executable, "-P", FAKE, "tree3"], "poll": 0.05}
    deps.update(deps_extra or {})
    return ytdlp.run_media_item(URL, "0001", dirs, limits_in or LIMITS, TOOLS, budget=10 ** 12, room=10 ** 12,
                                clock=clock, deps=deps)


def interrupt_when_tree_is_up(dirs):
    def go():
        procs.wait_for_pids(dirs.io, 3)
        _thread.interrupt_main()

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread


def test_interrupt_kills_tree(item):
    seen = []

    def spy(job, proc, wait=5.0):
        ok = jobobject.kill_tree(job, proc, wait)
        seen.append((ok, job.active_processes()))
        return ok

    waiter = interrupt_when_tree_is_up(item)
    with pytest.raises(KeyboardInterrupt):
        run_tree(item, {"kill_tree": spy})
    waiter.join(5)
    pids = procs.read_pids(item.io)
    assert len(pids) == 3  # the engine, a child and a grandchild all ran
    assert seen == [(True, 0)]  # the tree was empty before the interrupt left the function
    assert procs.wait_dead(pids, 5) == []


def test_hard_kill_core_kills_tree(tmp_path):
    job_dir = str(tmp_path)
    io = os.path.join(job_dir, ".tmp", "0001", "io")
    driver = subprocess.Popen([sys.executable, "-P", DRIVER, SRC, job_dir, FAKE, "tree3"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids = []
    try:
        pids = procs.wait_for_pids(io, 3)
        assert len(pids) == 3
        driver.kill()  # no handler runs: only the Job Object can end the tree
        driver.wait(timeout=10)
        assert procs.wait_dead(pids, 5) == []
    finally:
        if driver.poll() is None:
            driver.kill()
        stragglers = procs.reap(pids)
    assert not stragglers


def test_assign_failure_kills_process(item, monkeypatch):
    holder_seen = []

    def refuse(self, pid):
        raise jobobject.JobObjectError(5, "assign refused")

    def spy(argv, env, cwd, out, err, holder=None):
        holder_seen.append(holder)
        return jobobject.spawn_in_job(argv, env, cwd, out, err, holder=holder)

    monkeypatch.setattr(jobobject.JobObject, "assign", refuse)
    outcome = run_tree(item, {"spawn": spy})
    assert (outcome.status, outcome.code, outcome.reason) == ("failed", "TOOL_FAILED", "job_object_unavailable")
    assert outcome.retryable is False and outcome.file_path is None
    proc = holder_seen[0][0][0]  # the process the helper thread made before the assignment failed
    assert proc.wait(timeout=5) is not None
    assert procs.wait_dead(procs.read_pids(item.io), 5) == []


def test_a_hard_deadline_ends_the_tree_and_is_not_retryable(item, fake_clock):
    def sleep(seconds):
        time.sleep(0.05)
        fake_clock.sleep(0.2)  # the injected clock runs faster than the wall clock

    small = dict(LIMITS, media_item_timeout=6)
    outcome = run_tree(item, {"sleep": sleep}, small, clock=fake_clock)
    assert (outcome.status, outcome.code, outcome.retryable) == ("failed", "TIMEOUT", False)
    assert procs.wait_dead(procs.read_pids(item.io), 5) == []


def test_a_tree_that_stays_after_the_wait_is_reported(item, fake_clock):
    def sleep(seconds):
        time.sleep(0.05)
        fake_clock.sleep(0.2)

    def stubborn(job, proc, wait=5.0):
        jobobject.kill_tree(job, proc, wait)  # really ended, but this says it was not empty in time
        return False

    outcome = run_tree(item, {"sleep": sleep, "kill_tree": stubborn}, dict(LIMITS, media_item_timeout=6), clock=fake_clock)
    assert outcome.code == "TIMEOUT" and "process_tree_not_empty" in outcome.warnings


def test_the_child_does_not_get_ctrl_c_from_the_console(item, monkeypatch):
    """FR12.6: the engine runs in its own process group; the core stops it by itself (see the interrupt test)."""
    seen = []

    def spy(argv, env, cwd, out, err, holder=None):
        seen.append(argv)
        return jobobject.spawn_in_job(argv, env, cwd, out, err, holder=holder)

    flags_seen = []
    real = subprocess.Popen

    def capture(*args, **kwargs):
        flags_seen.append(kwargs.get("creationflags"))
        return real(*args, **kwargs)

    monkeypatch.setattr(jobobject.subprocess, "Popen", capture)
    waiter = interrupt_when_tree_is_up(item)
    with pytest.raises(KeyboardInterrupt):
        run_tree(item, {"spawn": spy})
    waiter.join(5)
    assert seen and seen[0][:4] == [sys.executable, "-P", FAKE, "tree3"]
    assert flags_seen and flags_seen[0] & jobobject.CREATE_NEW_PROCESS_GROUP
    assert flags_seen[0] & jobobject.CREATE_NO_WINDOW


def test_an_interrupt_with_a_tree_that_stays_marks_the_exception(item):
    """FR12.2: the Ctrl+C path reports a tree that was not empty in time, for the runner to turn into a warning."""
    def stubborn(job, proc, wait=5.0):
        jobobject.kill_tree(job, proc, wait)
        return False

    waiter = interrupt_when_tree_is_up(item)
    with pytest.raises(KeyboardInterrupt) as caught:
        run_tree(item, {"kill_tree": stubborn})
    waiter.join(5)
    assert caught.value.process_tree_not_empty is True


def test_a_tree_that_is_not_empty_after_a_clean_exit_takes_no_file(item):
    """FR10.1: the file is picked only after the tree is dead; otherwise the item fails and nothing is taken."""
    def stubborn(job, proc, wait=5.0):
        jobobject.kill_tree(job, proc, wait)
        return False

    deps = {"engine_prefix": lambda: [sys.executable, "-P", FAKE, "ok_mp4"], "kill_tree": stubborn}
    outcome = ytdlp.run_media_item(URL, "0001", item, LIMITS, TOOLS, budget=10 ** 12, room=10 ** 12,
                                   clock=time.monotonic, deps=dict(deps, poll=0.05))
    assert (outcome.status, outcome.code, outcome.file_path) == ("failed", "TOOL_FAILED", None)
    assert "process_tree_not_empty" in outcome.warnings
