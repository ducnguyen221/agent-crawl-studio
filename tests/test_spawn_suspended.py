"""SPR-04 TK5 (FR22.1, FR22.3, SC28, SC30): the child is made suspended, put in the job, limits set, and only then let
run. Real processes (tiny `python -c` children of this test) in real Job Objects; the three steps are watched or made
to fail through a kernel that wraps the real one. A teardown net ends by PID anything still alive."""

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

import pytest

from agent_crawl_studio import jobobject
from fixtures import procs

MiB = 2 ** 20
N = 1 if sys.prefix == sys.base_prefix else 2  # processes the test's own interpreter needs (PLAN TK1, DN18)
FIRST_STATEMENT = "import sys; open(sys.argv[1], 'w').write(str(__import__('os').getpid())); import time; time.sleep(30)"
HOG = ("import sys\n"
       "try:\n"
       "    block = bytearray(int(sys.argv[1]) * 1048576)\n"
       "except MemoryError:\n"
       "    pass\n")
TRY_SPAWN = ("import subprocess, sys\n"
             "open(sys.argv[1], 'w').write('up')\n"
             "try:\n"
             "    subprocess.Popen([sys.executable, '-c', 'pass']).wait()\n"
             "    result = 'spawned'\n"
             "except OSError as exc:\n"
             "    result = 'blocked:%s' % exc.winerror\n"
             "open(sys.argv[1] + '.2', 'w').write(result)\n")

_K32 = ctypes.WinDLL("kernel32", use_last_error=True)
_K32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
_K32.IsProcessInJob.restype = wintypes.BOOL


@pytest.fixture
def pids():
    seen = []
    yield seen
    stragglers = procs.reap(seen)
    assert not stragglers, "processes left alive: %r" % (stragglers,)


class Spy(jobobject._Kernel):
    """The real kernel calls, with a hook before each of the three steps (`before`) that may raise."""

    def __init__(self, before=None):
        super().__init__()
        self.before = before or (lambda step, process: None)
        self.job = None
        self.order = []

    def set_limits(self, job, memory_bytes, active_processes):
        self.order.append("set_limits")
        self.before("set_limits", None)
        return super().set_limits(job, memory_bytes, active_processes)

    def assign(self, job, process):
        self.order.append("assign")
        self.job = job
        self.before("assign", process)
        return super().assign(job, process)

    def resume(self, process):
        self.order.append("resume")
        self.before("resume", process)
        return super().resume(process)


_K32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_K32.OpenProcess.restype = wintypes.HANDLE
_K32.CloseHandle.argtypes = (wintypes.HANDLE,)


def in_job(pid, job):
    """IsProcessInJob needs its own handle: the one the kernel wrapper holds has no query right."""
    handle = _K32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    assert handle
    try:
        answer = wintypes.BOOL()
        assert _K32.IsProcessInJob(handle, job, ctypes.byref(answer))
        return bool(answer.value)
    finally:
        _K32.CloseHandle(handle)


def use(monkeypatch, kernel):
    monkeypatch.setattr(jobobject, "_KERNEL", kernel)


def wait_for(path, seconds=15.0):
    """The file exists and has its content (it is created before it is written)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return True
        time.sleep(0.02)
    return os.path.exists(path) and os.path.getsize(path) > 0


def read(path):
    with open(path, encoding="ascii") as handle:
        return handle.read()


# ------------------------------------------------------------------------------------- suspend -> assign -> run

def test_the_first_statement_runs_only_after_the_process_is_in_the_job_100_times(tmp_path, pids, monkeypatch):
    seen = []

    def before(step, process):
        if step != "resume":
            return
        time.sleep(0.03)  # a process that was already running would have written by now
        seen.append((in_job(started[-1], spy.job), os.path.exists(marker)))

    spy = Spy(before)
    use(monkeypatch, spy)
    started = []
    real = subprocess.Popen

    class Noting(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self.pid)

    monkeypatch.setattr(jobobject.subprocess, "Popen", Noting)
    for turn in range(100):
        marker = str(tmp_path / ("m%03d" % turn))
        proc, job = jobobject.spawn_in_job([sys.executable, "-c", FIRST_STATEMENT, marker], None, None,
                                           subprocess.DEVNULL, subprocess.DEVNULL)
        pids.append(proc.pid)
        try:
            assert wait_for(marker), "the child never ran (turn %d)" % turn
            assert int(read(marker)) == proc.pid
        finally:
            jobobject.kill_tree(job, proc)
            job.close()
    assert len(seen) == 100 and set(seen) == {(True, False)}  # in the job, and not yet run, every time


def test_the_steps_run_in_the_order_suspend_limits_assign_resume(tmp_path, pids, monkeypatch):
    spy = Spy()
    use(monkeypatch, spy)
    flags = []
    real = subprocess.Popen

    def capture(*args, **kwargs):
        flags.append(kwargs["creationflags"])
        return real(*args, **kwargs)

    monkeypatch.setattr(jobobject.subprocess, "Popen", capture)
    marker = str(tmp_path / "m")
    proc, job = jobobject.spawn_in_job([sys.executable, "-c", FIRST_STATEMENT, marker], None, None,
                                       subprocess.DEVNULL, subprocess.DEVNULL,
                                       limits=jobobject.JobLimits(memory_bytes=512 * MiB, active_processes=N))
    pids.append(proc.pid)
    try:
        assert spy.order[-3:] == ["set_limits", "assign", "resume"]  # (the core's own one-time net join may come before)
        assert flags[0] & jobobject.CREATE_SUSPENDED
        assert flags[0] & jobobject.CREATE_NEW_PROCESS_GROUP and flags[0] & jobobject.CREATE_NO_WINDOW
        assert wait_for(marker)
    finally:
        jobobject.kill_tree(job, proc)
        job.close()


@pytest.mark.parametrize("step", ["set_limits", "assign", "resume"])
def test_a_failing_step_ends_the_suspended_process_and_raises(tmp_path, pids, monkeypatch, step):
    def before(name, process):
        if name == step:
            raise jobobject.JobObjectError(5, "boom " + step)

    use(monkeypatch, Spy(before))
    holder = []
    marker = str(tmp_path / "m")
    with pytest.raises(jobobject.JobObjectError):
        jobobject.spawn_in_job([sys.executable, "-c", FIRST_STATEMENT, marker], None, None, subprocess.DEVNULL,
                               subprocess.DEVNULL, holder=holder, limits=jobobject.JobLimits(512 * MiB, N))
    proc = holder[0][0]
    pids.append(proc.pid)
    assert proc.wait(timeout=5) is not None  # ended, not left suspended
    assert not os.path.exists(marker)  # and not one statement of it ran


@pytest.mark.parametrize("step", ["set_limits", "assign", "resume"])
def test_an_interrupt_between_the_steps_ends_the_process_and_is_raised_again(tmp_path, pids, monkeypatch, step):
    def before(name, process):
        if name == step:
            raise KeyboardInterrupt

    use(monkeypatch, Spy(before))
    holder = []
    marker = str(tmp_path / "m")
    with pytest.raises(KeyboardInterrupt):
        jobobject.spawn_in_job([sys.executable, "-c", FIRST_STATEMENT, marker], None, None, subprocess.DEVNULL,
                               subprocess.DEVNULL, holder=holder, limits=jobobject.JobLimits(512 * MiB, N))
    proc = holder[0][0]
    pids.append(proc.pid)
    assert proc.wait(timeout=5) is not None


def test_the_old_call_shape_still_works_with_no_limits(tmp_path, pids):
    marker = str(tmp_path / "m")
    proc, job = jobobject.spawn_in_job([sys.executable, "-c", FIRST_STATEMENT, marker], None, None,
                                       subprocess.DEVNULL, subprocess.DEVNULL)
    pids.append(proc.pid)
    try:
        assert wait_for(marker)
    finally:
        jobobject.kill_tree(job, proc)
        job.close()


# ------------------------------------------------------------------------------------------------- the limits

def run_child(tmp_path, pids, code, arg, limits):
    proc, job = jobobject.spawn_in_job([sys.executable, "-c", code, arg], None, None, subprocess.DEVNULL,
                                       subprocess.DEVNULL, limits=limits)
    pids.append(proc.pid)
    proc.wait(timeout=60)
    return proc, job


def test_the_process_limit_stops_the_n_plus_first_process(tmp_path, pids):
    marker = str(tmp_path / "m")
    proc, job = run_child(tmp_path, pids, TRY_SPAWN, marker, jobobject.JobLimits(active_processes=N))
    with job:
        assert read(marker) == "up"  # N processes are enough for the interpreter itself
        assert read(marker + ".2") == "blocked:1816"  # ERROR_NOT_ENOUGH_QUOTA: the next one is not made
        # `ActiveProcesses` reads N + 1 while `conhost.exe` is in the job (PLAN TK1): no "active <= N" claim here


def test_one_process_more_than_the_limit_is_made(tmp_path, pids):
    marker = str(tmp_path / "m")
    proc, job = run_child(tmp_path, pids, TRY_SPAWN, marker, jobobject.JobLimits(active_processes=N + 1))
    with job:
        assert read(marker + ".2") == "spawned"


def test_limits_keep_the_kill_on_close_flag(tmp_path, pids):
    marker = str(tmp_path / "m")
    proc, job = jobobject.spawn_in_job([sys.executable, "-c", FIRST_STATEMENT, marker], None, None,
                                       subprocess.DEVNULL, subprocess.DEVNULL,
                                       limits=jobobject.JobLimits(memory_bytes=512 * MiB, active_processes=N))
    pids.append(proc.pid)
    assert wait_for(marker)
    job.close()
    assert procs.wait_dead([proc.pid], 5) == []


def test_the_peak_of_a_big_allocation_is_read_and_is_not_a_hit_under_the_limit(tmp_path, pids):
    proc, job = run_child(tmp_path, pids, HOG, "64", jobobject.JobLimits(memory_bytes=512 * MiB, active_processes=N))
    with job:
        assert proc.returncode == 0
        assert 64 * MiB <= job.peak_memory() <= 512 * MiB
        assert job.memory_limit_hit() is False


def test_a_hit_is_seen_even_when_the_child_swallows_the_error_and_exits_zero(tmp_path, pids):
    limit = 128 * MiB
    proc, job = run_child(tmp_path, pids, HOG, "300", jobobject.JobLimits(memory_bytes=limit, active_processes=N))
    with job:
        assert proc.returncode == 0  # `MemoryError` caught: the exit code says nothing (SC28)
        assert job.peak_memory() > limit
        assert job.memory_limit_hit() is True


def test_no_memory_limit_is_never_a_hit(tmp_path, pids):
    proc, job = run_child(tmp_path, pids, HOG, "64", jobobject.JobLimits(active_processes=N))
    with job:
        assert job.peak_memory() >= 64 * MiB and job.memory_limit_hit() is False


# --------------------------------------------------------------------------------------- the default sizes

def test_the_extract_job_limits_are_the_judged_numbers():
    assert jobobject.EXTRACT_JOB_LIMITS == jobobject.JobLimits(memory_bytes=512 * MiB, active_processes=2)


def test_joblimits_default_to_nothing():
    assert jobobject.JobLimits() == jobobject.JobLimits(None, None)
    assert jobobject.JobLimits(memory_bytes=1, active_processes=2)[:] == (1, 2)


def test_a_memory_limit_alone_sets_no_process_limit(tmp_path, pids):
    marker = str(tmp_path / "m")
    proc, job = run_child(tmp_path, pids, TRY_SPAWN, marker, jobobject.JobLimits(memory_bytes=512 * MiB))
    with job:
        assert read(marker + ".2") == "spawned"
