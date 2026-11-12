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
