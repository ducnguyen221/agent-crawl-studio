"""Test setup: make `src/` importable. Tests use the system Python with PYTHONPATH=src semantics,
only the standard library, httpx (same pinned version as the core venv) and pytest; they never
create a virtual environment in the checkout.

Three-state network rule (SPEC FR6.4): a test marked `network` needs the real Internet. Without
`--run-network` it is NOT_RUN (reported as a skip whose reason starts with "NOT_RUN", never a pass);
with the option but a broken probe it is BLOCKED_ENV; running and wrong is FAIL."""

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def pytest_addoption(parser):
    parser.addoption("--run-network", action="store_true", default=False,
                     help="run tests marked `network` (real Internet); otherwise they are NOT_RUN")


def pytest_configure(config):
    config.addinivalue_line("markers", "network: needs the real Internet; NOT_RUN without --run-network")


def network_status(run_network, probe_ok):
    """The three states of a network step: NOT_RUN, BLOCKED_ENV or RUN (a RUN that goes wrong is FAIL)."""
    if not run_network:
        return "NOT_RUN"
    return "RUN" if probe_ok else "BLOCKED_ENV"


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-network"):
        return
    marker = pytest.mark.skip(reason="NOT_RUN: network test needs --run-network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(marker)


class FakeClock:
    """Injectable clock: `clock()` is the time, `clock.sleep(s)` advances it without waiting."""

    def __init__(self, start=0.0):
        self.now = float(start)
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_clock():
    return FakeClock()


@pytest.fixture
def no_socket(monkeypatch):
    """Any attempt to open a socket or resolve a name is RECORDED (code under test may swallow the error
    it gets) and the test fails at teardown if the list is not empty. A test that provokes a call on
    purpose must `clear()` the list."""
    calls = []

    def recorder(name):
        def refuse(*args, **kwargs):
            calls.append((name, args))
            raise OSError("a socket was used where none is allowed")
        return refuse

    for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, recorder(name))
    yield calls
    assert not calls, "socket use recorded: %r" % (calls,)


def wait_for_resolver_slots(slots, count, timeout=5.0, clock=None, sleep=None):
    """Wait, at most `timeout` seconds, until all `count` places of `slots` can be taken at once; then fail the test
    that is about to start (FR16.5: a slot that never comes back is a leak, not something to run on top of)."""
    import time
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    deadline = clock() + timeout
    while True:
        got = [slots.acquire(blocking=False) for _ in range(count)]
        for taken in got:
            if taken:
                slots.release()
        if all(got):
            return
        if clock() >= deadline:
            pytest.fail("resolver slots busy: %d of %d never came back within %g s" % (got.count(False), count, timeout),
                        pytrace=False)
        sleep(0.05)


@pytest.fixture(autouse=True)
def _every_resolver_slot_is_free():
    """RESOLVER_SLOTS is a process-wide bound; a test that leaves a slow lookup running must not make the next one
    see fewer slots (RF343): wait, at most a few seconds, until all of them are back, else fail."""
    from agent_crawl_studio import netfetch
    wait_for_resolver_slots(netfetch.RESOLVER_SLOTS, netfetch.MAX_RESOLVER_THREADS)
    yield


def remove_junctions(paths):
    """Take away the junctions and links in `paths` that are still there, deepest first: the link only, never what it
    points at (`os.rmdir` does not follow it). A path that is gone, or is a plain folder, is left alone."""
    import os
    for path in sorted(set(paths), key=len, reverse=True):
        try:
            if getattr(os.lstat(path), "st_file_attributes", 0) & 0x400:
                os.rmdir(path)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _no_junction_is_left_behind(monkeypatch):
    """Every junction a test makes with `_winapi.CreateJunction` is removed (the link only) when the test ends,
    whether or not the test cleans up itself (RQ-013: three were left in the temp folder)."""
    if sys.platform != "win32":
        yield
        return
    import _winapi
    made, real = [], _winapi.CreateJunction

    def create(target, link):
        real(target, link)
        made.append(link)

    monkeypatch.setattr(_winapi, "CreateJunction", create)
    yield
    remove_junctions(made)
