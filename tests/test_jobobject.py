"""SPR-03 TK7 (FR12.1, FR12.2, SC15): the Job Object wrapper, `spawn_in_job` (process made and assigned in a
helper thread) and `kill_tree`. Real processes are tiny `python -c sleep` children of this test, ended by the very
mechanism under test; a teardown net ends by PID (never by name) anything still alive and fails the test."""

import _thread
import subprocess
import sys
import threading

import pytest

from agent_crawl_studio import jobobject
from fixtures import procs

SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def pids():
    """PIDs a test started; at teardown none may be alive (the net ends them and fails the test)."""
    seen = []
    yield seen
    stragglers = procs.reap(seen)
    assert not stragglers, "processes left alive: %r" % (stragglers,)


def spawn(env=None):
    return jobobject.spawn_in_job(SLEEP, env, None, subprocess.DEVNULL, subprocess.DEVNULL)


class FakeKernel:
    """The kernel32 calls of the wrapper, injected; `fail` names the call that raises."""

    def __init__(self, fail=None, active=1):
        self.fail = fail
        self.active_count = active
        self.calls = []

    def _go(self, name, *args):
        self.calls.append((name,) + args)
        if self.fail == name:
            raise jobobject.JobObjectError(5, "boom " + name)

    def create_job(self):
        self._go("create_job")
        return "J"

    def set_kill_on_close(self, job):
        self._go("set_kill_on_close", job)

    def open_process(self, pid):
        self._go("open_process", pid)
        return "P"

    def assign(self, job, process):
        self._go("assign", job, process)

    def active(self, job):
        self._go("active", job)
        return self.active_count

    def terminate(self, job, code):
        self._go("terminate", job, code)

    def close(self, handle):
        self._go("close", handle)

    def names(self):
        return [call[0] for call in self.calls]


# ------------------------------------------------------------------------------------------- real job

def test_job_kill_on_close(pids):
    proc, job = spawn()
    pids.append(proc.pid)
    assert job.active_processes() >= 1
    job.close()  # KILL_ON_JOB_CLOSE: the last handle going away ends the members
    assert procs.wait_dead([proc.pid], 5) == []
    assert proc.wait(timeout=5) is not None
    job.close()  # closing twice is harmless


def test_terminate_brings_the_count_to_zero(pids):
    proc, job = spawn()
    pids.append(proc.pid)
    with job:
        assert job.active_processes() >= 1
        job.terminate(7)
        assert procs.wait_dead([proc.pid], 5) == []
        assert job.active_processes() == 0
        assert proc.wait(timeout=5) == 7


def test_a_closed_job_refuses_calls():
    job = jobobject.JobObject()
    job.close()
    with pytest.raises(jobobject.JobObjectError):
        job.active_processes()
    with pytest.raises(jobobject.JobObjectError):
        job.terminate()
    with pytest.raises(jobobject.JobObjectError):
        job.assign(1)


def test_the_core_joins_a_job_once_and_survives_a_failing_kernel():
    kernel = FakeKernel(fail="assign")
    assert jobobject.ensure_core_job(kernel, force=True) is None  # a net, never a reason to stop
    assert kernel.names().count("create_job") == 1
    other = FakeKernel()
    jobobject.ensure_core_job(other)  # already tried once in this process: it does not try again
    assert other.calls == []


def test_the_job_error_is_an_oserror():
    assert issubclass(jobobject.JobObjectError, OSError)


# ------------------------------------------------------------------------ ctypes failure branches (fake kernel)

@pytest.mark.parametrize("fail", ["create_job", "set_kill_on_close"])
def test_creating_the_job_fails_cleanly(fail):
    kernel = FakeKernel(fail=fail)
    with pytest.raises(jobobject.JobObjectError):
        jobobject.JobObject(kernel=kernel)
    # a handle that was made is closed again; nothing is made when create itself failed
    assert ("close", "J") in kernel.calls if fail == "set_kill_on_close" else "close" not in kernel.names()


@pytest.mark.parametrize("fail", ["open_process", "assign"])
def test_assigning_fails_cleanly(fail):
    kernel = FakeKernel(fail=fail)
    job = jobobject.JobObject(kernel=kernel)
    with pytest.raises(jobobject.JobObjectError):
        job.assign(4242)
    assert ("close", "P") in kernel.calls if fail == "assign" else ("close", "P") not in kernel.calls


def test_assign_active_terminate_close_go_through_the_kernel():
    kernel = FakeKernel(active=3)
    job = jobobject.JobObject(kernel=kernel)
    job.assign(4242)
    assert job.active_processes() == 3
    job.terminate(9)
    job.close()
    assert kernel.names() == ["create_job", "set_kill_on_close", "open_process", "assign", "close", "active",
                              "terminate", "close"]
    assert ("terminate", "J", 9) in kernel.calls


# ------------------------------------------------------------------------------------------- spawn_in_job

def test_assign_failure_kills_the_process(pids, monkeypatch):
    made = []

    class Spy(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)
            pids.append(self.pid)

    def refuse(self, pid):
        raise jobobject.JobObjectError(5, "assign refused")

    monkeypatch.setattr(jobobject.subprocess, "Popen", Spy)
    monkeypatch.setattr(jobobject.JobObject, "assign", refuse)
    with pytest.raises(jobobject.JobObjectError):
        spawn()
    assert len(made) == 1 and made[0].wait(timeout=5) is not None  # the process was ended, not left running


def test_a_failing_popen_propagates_and_closes_the_job(monkeypatch):
    closed = []
    real_close = jobobject.JobObject.close
    monkeypatch.setattr(jobobject.JobObject, "close", lambda self: (closed.append(1), real_close(self)))
    with pytest.raises(OSError):
        jobobject.spawn_in_job([r"C:\no\such\program.exe"], None, None, subprocess.DEVNULL, subprocess.DEVNULL)
    assert closed


def test_spawn_fills_the_holder_before_it_returns(pids):
    holder = []
    proc, job = jobobject.spawn_in_job(SLEEP, None, None, subprocess.DEVNULL, subprocess.DEVNULL, holder=holder)
    pids.append(proc.pid)
    assert holder == [(proc, job)]
    jobobject.kill_tree(job, proc)
    job.close()


def test_an_interrupt_while_popen_is_slow_still_ends_the_process(pids, monkeypatch):
    """Review focus 1: the interrupt lands in `Popen.__init__` (here made slow); the helper thread finishes its
    work, the process is ended and the interrupt is raised again."""
    made = []

    class Slow(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)
            pids.append(self.pid)
            threading.Event().wait(0.6)

    monkeypatch.setattr(jobobject.subprocess, "Popen", Slow)
    timer = threading.Timer(0.2, _thread.interrupt_main)
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            spawn()
    finally:
        timer.cancel()
    assert len(made) == 1
    assert made[0].wait(timeout=5) is not None


# -------------------------------------------------------------------------------------------- kill_tree

class FakeJob:
    def __init__(self, counts, terminate_fails=False):
        self.counts = list(counts)
        self.terminated = []
        self.terminate_fails = terminate_fails

    def terminate(self, exit_code=1):
        self.terminated.append(exit_code)
        if self.terminate_fails:
            raise jobobject.JobObjectError(5, "no")

    def active_processes(self):
        return self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]


class FakeProc:
    def __init__(self, running=True):
        self.running = running
        self.killed = 0

    def poll(self):
        return None if self.running else 0

    def kill(self):
        self.killed += 1
        self.running = False

    def wait(self, timeout=None):
        return 0


def test_kill_tree_waits_for_zero(fake_clock):
    job = FakeJob([3, 1, 0])
    assert jobobject.kill_tree(job, FakeProc(running=False), wait=5.0, clock=fake_clock, sleep=fake_clock.sleep)
    assert job.terminated == [1] and len(fake_clock.sleeps) == 2


def test_kill_tree_gives_up_after_the_wait_and_says_so(fake_clock):
    job = FakeJob([1])
    assert jobobject.kill_tree(job, FakeProc(running=False), wait=5.0, clock=fake_clock, sleep=fake_clock.sleep) is False
    assert fake_clock.now >= 5.0


def test_kill_tree_ends_a_process_that_is_not_in_the_job(fake_clock):
    proc = FakeProc(running=True)
    jobobject.kill_tree(FakeJob([0]), proc, clock=fake_clock, sleep=fake_clock.sleep)
    assert proc.killed == 1


def test_kill_tree_survives_a_job_that_refuses(fake_clock):
    proc = FakeProc(running=True)
    job = FakeJob([0], terminate_fails=True)
    assert jobobject.kill_tree(job, proc, clock=fake_clock, sleep=fake_clock.sleep) is True
    assert proc.killed == 1


def test_kill_tree_real_three_level_tree_dies(pids):
    proc, job = spawn()
    pids.append(proc.pid)
    assert jobobject.kill_tree(job, proc, wait=5.0) is True
    job.close()
    assert procs.wait_dead([proc.pid], 5) == []


# ------------------------------------------------------------- the real kernel wrapper's failure branches (TK8)

class DeadK32:
    """kernel32 stand-in whose every call fails (returns 0/NULL), to reach the `_fail` branches of `_Kernel`."""

    def __getattr__(self, name):
        return lambda *args: 0


@pytest.fixture
def dead_kernel():
    kernel = jobobject._Kernel()
    kernel._k32 = DeadK32()
    return kernel


@pytest.mark.parametrize("call,args", [
    ("create_job", ()), ("set_kill_on_close", (1,)), ("open_process", (1,)), ("assign", (1, 2)),
    ("active", (1,)), ("terminate", (1, 1)),
])
def test_a_failing_kernel_call_raises_job_object_error(dead_kernel, call, args):
    with pytest.raises(jobobject.JobObjectError):
        getattr(dead_kernel, call)(*args)


def test_job_objects_need_windows(monkeypatch):
    monkeypatch.setattr(jobobject.sys, "platform", "linux")
    with pytest.raises(jobobject.JobObjectError):
        jobobject._Kernel()


def test_the_default_kernel_is_made_once():
    assert jobobject._default_kernel() is jobobject._default_kernel()


def test_a_successful_forced_core_job_is_returned_but_not_kept():
    kernel = FakeKernel()
    job = jobobject.ensure_core_job(kernel, force=True)
    assert job is not None and ("assign", "J", "P") in kernel.calls


# ------------------------------------------------------------------------------- error paths of the helpers

class StubbornProc(FakeProc):
    def kill(self):
        raise OSError("already gone")

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("x", timeout)


def test_ending_a_process_that_cannot_be_killed_is_not_fatal():
    jobobject._end(StubbornProc())


def test_abort_survives_a_refusing_job_and_a_process_that_will_not_exit():
    job = FakeJob([0], terminate_fails=True)
    job.close = lambda: None
    jobobject._abort({"proc": StubbornProc()}, job)
    jobobject._abort({}, job)  # nothing was made: only the job is closed


def test_kill_tree_trusts_the_process_when_the_job_is_closed(fake_clock):
    class Closed(FakeJob):
        def active_processes(self):
            raise jobobject.JobObjectError(0, "closed")

    assert jobobject.kill_tree(Closed([0]), FakeProc(running=False), clock=fake_clock, sleep=fake_clock.sleep) is True
    assert jobobject.kill_tree(Closed([0]), StubbornLive(), wait=1.0, clock=fake_clock, sleep=fake_clock.sleep) is False


class StubbornLive(FakeProc):
    def poll(self):
        return None

    def kill(self):
        pass

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("x", timeout)


def test_kill_tree_does_not_wait_forever_for_a_process_to_be_reaped(fake_clock):
    assert jobobject.kill_tree(FakeJob([0]), StubbornProc2(), clock=fake_clock, sleep=fake_clock.sleep) is True


class StubbornProc2(FakeProc):
    def __init__(self):
        super().__init__(running=False)

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("x", timeout)


def test_repeated_interrupts_while_waiting_for_the_helper_do_not_shorten_the_wait(pids, monkeypatch):
    class Edgy(threading.Thread):
        joins = 0

        def join(self, timeout=None):
            Edgy.joins += 1
            if Edgy.joins <= 2:
                raise KeyboardInterrupt  # the first wait and the first retry are both interrupted
            super().join(timeout)

    made = []
    real = subprocess.Popen

    class Spy(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)
            pids.append(self.pid)

    monkeypatch.setattr(jobobject.threading, "Thread", Edgy)
    monkeypatch.setattr(jobobject.subprocess, "Popen", Spy)
    with pytest.raises(KeyboardInterrupt):
        spawn()
    assert made and made[0].wait(timeout=5) is not None


def test_a_core_job_that_cannot_even_be_made_is_only_a_missing_net():
    assert jobobject.ensure_core_job(FakeKernel(fail="create_job"), force=True) is None
