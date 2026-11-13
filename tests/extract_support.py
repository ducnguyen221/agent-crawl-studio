"""Shared helpers of the SPR-04 TK9 tests (`test_extract_run.py`, `test_extract_limits.py`, the extract part of
`test_round10.py`): fake worker scripts that run as real child processes, and stand-ins for the process and the job
(for the cases that need a precise exit code or a precise peak of memory, which a real child cannot give on demand)."""

import json
import os
import sys
import textwrap

from agent_crawl_studio import jobobject

MiB = 2 ** 20
N = 1 if sys.prefix == sys.base_prefix else 2  # processes the interpreter of the test itself needs (PLAN TK1, DN18)
GOOD_TEXT = ("A fake page said something worth reading. " * 8).strip()  # over 200 characters: no `extract_short`

# A worker that reads `params.json` the way the real one does and writes one result file.
WORKER_TEMPLATE = textwrap.dedent('''
    import json, sys
    params = json.load(open(sys.argv[1], encoding="utf-8"))
    doc = {"v": 1, "ok": %(ok)s, "text": %(text)r, "meta": {"title": "T"}, "charset": "utf-8", "guessed": %(guessed)s}
    with open(params["out"], "w", encoding="utf-8") as handle:
        handle.write(json.dumps(doc))
''')


def worker_file(tmp_path, body=None, name="fake_worker.py", **fields):
    """A fake worker script in `tmp_path`: `body` is its whole text, else a template that writes a good result
    (`ok`, `text`, `guessed` can be changed)."""
    values = {"ok": True, "text": GOOD_TEXT, "guessed": False}
    values.update(fields)
    path = tmp_path / name
    path.write_text(body if body is not None else WORKER_TEMPLATE % values, encoding="utf-8")
    return str(path)


def fake_deps(tmp_path, **fields):
    """The deps that run the fake worker in a real child, in a real job, with the Python of the test."""
    return {"extract_python": sys.executable, "extract_worker": worker_file(tmp_path, **fields),
            "extract_poll": 0.02}


class FakeProc:
    def __init__(self, returncode=0):
        self.returncode, self.pid = returncode, 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


class FakeJob:
    def __init__(self, hit=False, active=0):
        self.hit, self.active, self.closed, self.terminated = hit, active, False, False

    def terminate(self, exit_code=1):
        self.terminated = True

    def active_processes(self):
        return self.active

    def memory_limit_hit(self):
        return self.hit

    def peak_memory(self):
        return 10 * MiB

    def close(self):
        self.closed = True


class FakeSpawn:
    """A `spawn` that makes no process: it calls `effect(argv, params)` (to write a result file, or to play a trick on
    the folder) and returns a finished fake process and a fake job."""

    def __init__(self, effect=None, returncode=0, hit=False, active=0):
        self.effect, self.returncode, self.hit, self.active = effect, returncode, hit, active
        self.calls, self.jobs = [], []

    def __call__(self, argv, env, cwd, stdout, stderr, holder=None, limits=None):
        self.calls.append({"argv": list(argv), "env": env, "cwd": cwd, "limits": limits})
        if self.effect is not None:
            with open(argv[-1], encoding="utf-8") as handle:
                params = json.load(handle)
            self.effect(argv, params)  # the params file is closed: the effect may remove it
        proc, job = FakeProc(self.returncode), FakeJob(self.hit, self.active)
        self.jobs.append(job)
        if holder is not None:
            holder.append((proc, job))
        return proc, job


def write_result(params, **over):
    doc = {"v": 1, "ok": True, "text": GOOD_TEXT, "meta": {}, "charset": "utf-8", "guessed": False}
    doc.update(over)
    with open(params["out"], "w", encoding="utf-8") as handle:
        handle.write(json.dumps(doc))


def good_effect(argv, params):
    write_result(params)


def real_spawn_with_core_python():
    """The real `spawn_in_job` (for a test that wraps it)."""
    return jobobject.spawn_in_job


def x_dir_of(item_dir):
    return os.path.join(item_dir, "x")
