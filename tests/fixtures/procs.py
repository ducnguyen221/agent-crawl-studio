"""Helpers for the process-tree tests (SPR-03): read the PIDs a fake engine wrote, ask Windows whether a PID is
alive, wait for a set of PIDs to die and - as the last resort of a failed test - end exactly those PIDs (never by
image name)."""

import ctypes
import os
import time
from ctypes import wintypes

SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
WAIT_TIMEOUT = 0x102

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
_k32.TerminateProcess.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = (wintypes.HANDLE,)
_k32.CloseHandle.restype = wintypes.BOOL


def alive(pid):
    handle = _k32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False  # no such process (or not ours to see): it is gone
    try:
        return _k32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        _k32.CloseHandle(handle)


def kill_pid(pid):
    handle = _k32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    if handle:
        try:
            _k32.TerminateProcess(handle, 1)
        finally:
            _k32.CloseHandle(handle)


def read_pids(io_dir):
    path = os.path.join(io_dir, "pids.txt")
    if not os.path.exists(path):
        return []
    with open(path, encoding="ascii") as handle:
        return [int(line) for line in handle.read().split()]


def wait_for_pids(io_dir, count, seconds=15.0):
    """Wait until the fake tree has written `count` PIDs; returns them."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pids = read_pids(io_dir)
        if len(pids) >= count:
            return pids
        time.sleep(0.05)
    return read_pids(io_dir)


def wait_dead(pids, seconds=5.0):
    """The PIDs that are still alive after `seconds` (empty list = all dead)."""
    end = time.monotonic() + seconds
    left = list(pids)
    while left and time.monotonic() < end:
        left = [pid for pid in left if alive(pid)]
        if left:
            time.sleep(0.05)
    return [pid for pid in left if alive(pid)]


def reap(pids):
    """Teardown net: end any listed PID still alive after a short grace (a terminated process disappears a moment
    later); returns the ones it had to end."""
    stragglers = wait_dead(pids, 5.0)
    for pid in stragglers:
        kill_pid(pid)
    return stragglers
