"""A Windows Job Object around the engine's whole process tree (SPEC FR12.1-FR12.3, DN9).

The engine (python -> python -> node/ffmpeg ...) runs in its own job with KILL_ON_JOB_CLOSE, so ending the job -
or the core dying with no chance to run a handler - ends every process in the tree. The process is made and put
in the job inside a helper thread, so a Ctrl+C that arrives in the middle cannot leave a running process outside
the job; the core also joins a job of its own once (a net: a child made in the instant before its assignment is
born inside the core's job and dies with the core). ctypes only (standard library); no BREAKAWAY flag is ever set,
so nothing the engine starts can leave the job."""

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_SYNCHRONIZE = 0x00100000
_INFO_BASIC_ACCOUNTING = 1
_INFO_EXTENDED_LIMIT = 9
_KILL_ON_JOB_CLOSE = 0x2000


class JobObjectError(OSError):
    """A kernel32 job call failed (or Job Objects are not available); `errno` is the Windows error code."""


class _BasicAccounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


class _BasicLimit(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                                                    "OtherOperationCount", "ReadTransferCount",
                                                    "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _Kernel:
    """The few kernel32 calls, with declared argument and result types and `use_last_error`; each raises
    JobObjectError on failure. Tests inject a stand-in with the same methods."""

    def __init__(self):
        if sys.platform != "win32":
            raise JobObjectError(0, "Job Objects need Windows")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32 = k32
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                  ctypes.POINTER(wintypes.DWORD))
        k32.QueryInformationJobObject.restype = wintypes.BOOL
        k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k32.TerminateJobObject.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL

    def _fail(self, call):
        code = ctypes.get_last_error()
        raise JobObjectError(code, "%s failed (%d)" % (call, code))

    def create_job(self):
        handle = self._k32.CreateJobObjectW(None, None)
        if not handle:
            self._fail("CreateJobObjectW")
        return handle

    def set_kill_on_close(self, job):
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not self._k32.SetInformationJobObject(job, _INFO_EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info)):
            self._fail("SetInformationJobObject")

    def open_process(self, pid):
        handle = self._k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _SYNCHRONIZE, False, pid)
        if not handle:
            self._fail("OpenProcess")
        return handle

    def assign(self, job, process):
        if not self._k32.AssignProcessToJobObject(job, process):
            self._fail("AssignProcessToJobObject")

    def active(self, job):
        info = _BasicAccounting()
        if not self._k32.QueryInformationJobObject(job, _INFO_BASIC_ACCOUNTING, ctypes.byref(info),
                                                   ctypes.sizeof(info), None):
            self._fail("QueryInformationJobObject")
        return int(info.ActiveProcesses)

    def terminate(self, job, exit_code):
        if not self._k32.TerminateJobObject(job, exit_code):
            self._fail("TerminateJobObject")

    def close(self, handle):
        self._k32.CloseHandle(handle)


_KERNEL = None


def _default_kernel():
    global _KERNEL
    if _KERNEL is None:
        _KERNEL = _Kernel()
    return _KERNEL


class JobObject:
    """One job with KILL_ON_JOB_CLOSE. `close()` (also at the end of a `with`) ends every process still in it."""

    def __init__(self, kernel=None):
        self._k = kernel if kernel is not None else _default_kernel()
        self._handle = self._k.create_job()
        try:
            self._k.set_kill_on_close(self._handle)
        except BaseException:
            self._k.close(self._handle)
            self._handle = None
            raise

    def _open(self):
        if self._handle is None:
            raise JobObjectError(0, "the job is closed")
        return self._handle

    def assign(self, pid):
        """Put the process in the job. Nested jobs are fine (Windows 8+); the process keeps no handle of ours."""
        job = self._open()
        process = self._k.open_process(pid)
        try:
            self._k.assign(job, process)
        finally:
            self._k.close(process)

    def active_processes(self):
        return self._k.active(self._open())

    def terminate(self, exit_code=1):
        self._k.terminate(self._open(), exit_code)

    def close(self):
        handle, self._handle = self._handle, None
        if handle is not None:
            self._k.close(handle)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


_CORE = None
_CORE_TRIED = False
_CORE_LOCK = threading.Lock()


def ensure_core_job(kernel=None, force=False):
    """Once per process, put the core itself in a KILL_ON_JOB_CLOSE job. Returns that job, or None when it could
    not be done - which is never a reason to stop: it is only a net behind the per-item job."""
    global _CORE, _CORE_TRIED
    with _CORE_LOCK:
        if _CORE_TRIED and not force:
            return _CORE
        _CORE_TRIED = True
        job = None
        try:
            job = JobObject(kernel)
            job.assign(os.getpid())
        except OSError:
            if job is not None:
                job.close()
            return None
        if not force:
            _CORE = job
        return job


def _end(proc):
    try:
        proc.kill()
    except OSError:
        pass


def _abort(box, job):
    proc = box.get("proc")
    try:
        job.terminate()
    except JobObjectError:
        pass
    if proc is not None:
        _end(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    job.close()


def spawn_in_job(argv, env, cwd, stdout, stderr, holder=None):
    """Start `argv` in a new process group inside a new job; returns (Popen, JobObject). The process is made and
    assigned in a helper thread while this one only waits, so an interrupt cannot fall between the two: it waits
    for the helper to finish, ends what was made and raises again. `holder`, when given, gets `(proc, job)` the
    moment the process exists (before it is returned), so a caller interrupted after this function returned still
    knows what to end. A failed assignment ends the process and raises JobObjectError."""
    ensure_core_job()
    job = JobObject()
    box = {}

    def work():
        try:
            proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                    creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
            box["proc"] = proc
            if holder is not None:
                holder.append((proc, job))
            job.assign(proc.pid)
        except BaseException as exc:  # handed to the waiting thread, which raises it
            box["error"] = exc

    helper = threading.Thread(target=work, name="crawl-spawn", daemon=True)
    helper.start()
    try:
        helper.join()
        if "error" in box:
            raise box["error"]
    except BaseException:
        while helper.is_alive():
            try:
                helper.join()
            except BaseException:  # a second interrupt does not shorten the wait: the helper ends in moments
                continue
        _abort(box, job)
        raise
    return box["proc"], job


def kill_tree(job, proc, wait=5.0, clock=time.monotonic, sleep=time.sleep):
    """End every process of the job and wait up to `wait` seconds for none to be left. True when the tree is
    empty; False when it was not in time (the caller records `process_tree_not_empty` and goes on)."""
    try:
        job.terminate()
    except JobObjectError:
        pass
    if proc.poll() is None:
        _end(proc)  # a process that never got into the job
    deadline = clock() + wait
    while True:
        try:
            left = job.active_processes()
        except JobObjectError:
            left = 0 if proc.poll() is not None else 1  # a closed job has no members; trust the process then
        if left == 0:
            break
        if clock() >= deadline:
            return False
        sleep(0.02)
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    return True
