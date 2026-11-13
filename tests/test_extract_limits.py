"""SPR-04 TK9 (FR19.1-FR19.4, FR19.6, FR22.4; SC28): `extract.run_extract_item`, the one function that runs the child
process of the extract mode: its job, its limits, its clock, the order of the codes when it fails, and the reading of
its result. Real children (a fake worker script, or the real engine in the core venv) run in real Job Objects; the
cases that need an exact exit code or an exact peak of memory use a stand-in process and job."""

import json
import os
import sys
import time

import pytest

from agent_crawl_studio import envbuild, errors, extract, jobobject, limits as limits_mod, ytdlp
from fixtures import procs

import extract_support as sup
from extract_support import FakeSpawn, MiB, N, good_effect, write_result
from test_extract_worker import FIXTURES, engine_python

HTML = b"<!DOCTYPE html><html><head><title>x</title></head><body><p>hello</p></body></html>"
LIMITS = {"extract_timeout": 30, "max_extract_chars": 200_000}


@pytest.fixture
def pids():
    seen = []
    yield seen
    assert not procs.reap(seen), "processes left alive"


def call(tmp_path, html=HTML, limits=None, deps=None, deadline=1e9, clock=time.monotonic, content_type="text/html",
         check=None):
    """`run_extract_item` on a page in `tmp_path`. Returns (outcome, item_dir)."""
    item_dir = str(tmp_path / "item")
    os.makedirs(item_dir, exist_ok=True)  # also makes `tmp_path` itself
    page = str(tmp_path / "page.html")
    with open(page, "wb") as handle:
        handle.write(html)
    outcome = extract.run_extract_item(page, item_dir, content_type, dict(LIMITS, **(limits or {})),
                                       envbuild.process_env(), deps or {}, clock, deadline, check)
    return outcome, item_dir


def failed(outcome, code, reason=None):
    assert outcome.status == errors.ITEM_STATUS_FOR[code] and outcome.code == code and outcome.reason == reason
    assert outcome.text is None and outcome.meta == {}


def tool_failed(outcome):
    failed(outcome, "TOOL_FAILED")
    assert outcome.hint == errors.HINTS["extract_failed"]


# ----------------------------------------------------------------------------------------------------- the contract
def test_the_outcome_has_the_fields_of_the_contract():
    assert extract.ExtractOutcome._fields[:9] == ("status", "code", "reason", "hint", "text", "meta", "truncated",
                                                  "guessed", "warnings")


def test_the_job_limits_of_the_child_are_the_judged_ones(tmp_path):
    spawn = FakeSpawn(good_effect)
    outcome, _ = call(tmp_path, deps={"spawn": spawn})
    assert outcome.status == "succeeded" and spawn.calls[0]["limits"] == extract.EXTRACT_JOB_LIMITS
    assert extract.EXTRACT_JOB_LIMITS == jobobject.EXTRACT_JOB_LIMITS == jobobject.JobLimits(512 * MiB, 2)


def test_the_child_command_folder_and_params(tmp_path):
    spawn = FakeSpawn(good_effect)
    outcome, item_dir = call(tmp_path, deps={"spawn": spawn, "extract_python": "PY"},
                             content_type="text/html; charset=latin-1", limits={"max_extract_chars": 1234})
    call_ = spawn.calls[0]
    worker = os.path.join(os.path.dirname(extract.__file__), "extract_worker.py")
    params_path = os.path.join(item_dir, "x", "params.json")
    assert call_["argv"] == ["PY", "-I", "-X", "utf8", worker, params_path] and call_["cwd"] == item_dir
    with open(params_path, encoding="utf-8") as handle:
        assert json.load(handle) == {"v": 1, "html": str(tmp_path / "page.html"),
                                     "out": os.path.join(item_dir, "x", "result.json"),
                                     "content_type": "text/html; charset=latin-1", "max_chars": 1234}
    assert all(job.closed for job in spawn.jobs)


def test_the_default_python_is_the_one_that_runs_the_core(tmp_path):
    spawn = FakeSpawn(good_effect)
    call(tmp_path, deps={"spawn": spawn})
    assert spawn.calls[0]["argv"][0] == sys.executable


def test_the_child_environment_holds_only_allowed_keys_and_no_proxy(tmp_path):
    spawn = FakeSpawn(good_effect)
    base = dict(os.environ, HTTP_PROXY="http://x:y@127.0.0.1:1", HTTPS_PROXY="http://a", PYTHONPATH="C:/elsewhere")
    page = tmp_path / "page.html"
    page.write_bytes(HTML)
    (tmp_path / "item").mkdir()
    extract.run_extract_item(str(page), str(tmp_path / "item"), None, LIMITS, base, {"spawn": spawn},
                             time.monotonic, 1e9)
    env = spawn.calls[0]["env"]
    assert set(env) <= set(envbuild.ALLOWED_KEYS) and "HTTP_PROXY" not in env and "PYTHONPATH" not in env
    assert env["TEMP"] == env["TMP"] == env["USERPROFILE"] == os.path.join(str(tmp_path / "item"), "home")


def test_the_real_child_sees_only_allowed_keys(tmp_path):
    seen = tmp_path / "env.json"
    body = ("import json, os, sys\n"
            "params = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            "open(%r, 'w').write(json.dumps(sorted(os.environ)))\n"
            "open(params['out'], 'w').write(json.dumps({'v': 1, 'ok': False, 'text': '', 'meta': {}, "
            "'charset': 'utf-8', 'guessed': False}))\n" % str(seen))
    outcome, _ = call(tmp_path, deps=sup.fake_deps(tmp_path, body=body))
    assert outcome.code == "CONTENT_MISMATCH"
    names = {name.upper() for name in json.loads(seen.read_text())}
    assert names <= {key.upper() for key in envbuild.ALLOWED_KEYS}, names


# ------------------------------------------------------------------------------------------------ what is a page
@pytest.mark.parametrize("data", [b"%PDF-1.7\n" + b"0" * 100, b"\x89PNG\r\n\x1a\n", b"plain words, no tag", b""])
def test_bytes_that_are_not_html_are_not_html_and_no_process_is_made(tmp_path, data):
    spawn = FakeSpawn(good_effect)
    outcome, _ = call(tmp_path, html=data, deps={"spawn": spawn})
    failed(outcome, "CONTENT_MISMATCH", "not_html")
    assert spawn.calls == []


@pytest.mark.parametrize("data", [
    b"\xff\xfe" + "<html><body>x</body></html>".encode("utf-16-le"),
    "<html><body>x</body></html>".encode("utf-16-le"),
    b'<?xml version="1.0"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml"><body>x</body></html>'])
def test_utf16_and_xhtml_pages_go_to_the_child(tmp_path, data):
    spawn = FakeSpawn(good_effect)
    outcome, _ = call(tmp_path, html=data, deps={"spawn": spawn})
    assert outcome.status == "succeeded" and len(spawn.calls) == 1


# ------------------------------------------------------------------------------------------------ the good outcome
def test_a_good_result_is_cleaned_and_reported(tmp_path):
    outcome, _ = call(tmp_path, deps=sup.fake_deps(tmp_path, guessed=True))
    assert outcome.status == "succeeded" and outcome.code is None and outcome.hint is None
    assert outcome.text == sup.GOOD_TEXT and outcome.meta == {"title": "T"}
    assert outcome.truncated is False and outcome.guessed is True and outcome.warnings == ["encoding_guessed"]


def test_short_text_is_a_warning_and_still_a_success(tmp_path):
    outcome, _ = call(tmp_path, deps=sup.fake_deps(tmp_path, text="Just a few words."))
    assert outcome.status == "succeeded" and outcome.warnings == ["extract_short"] and outcome.guessed is False


def test_a_cut_text_is_reported_as_truncated(tmp_path):
    text = "\n\n".join(["A paragraph that is long enough to count for something."] * 20)
    outcome, _ = call(tmp_path, deps=sup.fake_deps(tmp_path, text=text), limits={"max_extract_chars": 300})
    assert outcome.status == "succeeded" and outcome.truncated is True and len(outcome.text) <= 300
    assert outcome.warnings == ["extract_truncated"]


@pytest.mark.parametrize("fields", [{"ok": False, "text": ""}, {"ok": True, "text": " \n\t \u200b\u2060 "}])
def test_no_text_is_a_content_mismatch(tmp_path, fields):
    outcome, _ = call(tmp_path, deps=sup.fake_deps(tmp_path, **fields))
    failed(outcome, "CONTENT_MISMATCH", "no_text")


# ------------------------------------------------------------------------------------- the child that goes wrong
@pytest.mark.parametrize("code", [1, 2, 3, 259])
def test_a_child_that_exits_non_zero_is_tool_failed(tmp_path, code):
    spawn = FakeSpawn(good_effect, returncode=code)  # the result is fine, the exit code is not: it fails
    outcome, _ = call(tmp_path, deps={"spawn": spawn})
    tool_failed(outcome)


def test_a_child_that_writes_nothing_is_tool_failed(tmp_path):
    tool_failed(call(tmp_path, deps={"spawn": FakeSpawn(None)})[0])


@pytest.mark.parametrize("data", [b"", b"{}", b"[", b"[]", b"\xff\xfe\x00", b"1", b'{"v": 1}',
                                  json.dumps({"v": 1, "ok": True, "text": 5, "meta": {}, "charset": "x",
                                              "guessed": False}).encode(),
                                  json.dumps({"v": 1, "ok": True, "text": "x", "meta": {}, "charset": "x",
                                              "guessed": False, "extra": 1}).encode(),
                                  b"[" * 100_000, b"{\"a\":" * 100_000],
                         ids=lambda data: "%d-bytes-%s" % (len(data), data[:4].hex()))
def test_a_result_of_the_wrong_shape_is_tool_failed_and_the_core_does_not_throw(tmp_path, data):
    def effect(argv, params):
        with open(params["out"], "wb") as handle:
            handle.write(data)
    tool_failed(call(tmp_path, deps={"spawn": FakeSpawn(effect)})[0])


def test_a_result_file_of_16_mb_of_brackets_is_tool_failed(tmp_path):
    def effect(argv, params):
        with open(params["out"], "wb") as handle:
            handle.write(b"[" * (16 * MiB))
    started = time.monotonic()
    tool_failed(call(tmp_path, deps={"spawn": FakeSpawn(effect)})[0])
    assert time.monotonic() - started < 5


def test_a_result_that_is_not_a_regular_file_is_tool_failed(tmp_path):
    def effect(argv, params):
        os.mkdir(params["out"])
    tool_failed(call(tmp_path, deps={"spawn": FakeSpawn(effect)})[0])


def test_the_stderr_of_the_child_is_kept_in_the_item_folder(tmp_path):
    body = "import sys\nsys.stderr.write('Traceback: something broke\\n')\nsys.exit(1)\n"
    outcome, item_dir = call(tmp_path, deps=sup.fake_deps(tmp_path, body=body))
    tool_failed(outcome)
    with open(os.path.join(item_dir, "io", "stderr.bin"), "rb") as handle:
        assert b"something broke" in handle.read()
    assert "something" not in repr(outcome)  # nothing of it travels in the outcome (FR19.4)


def test_the_stderr_tail_is_at_most_64_kib_and_starts_on_a_line(tmp_path):
    path = tmp_path / "stderr.bin"
    path.write_bytes(b"first line\n" + b"x" * 70_000 + b"\nlast line\n")
    text = extract.stderr_tail(str(path))
    assert len(text.encode("utf-8")) <= 64 * 1024 and text.endswith("last line\n") and "first line" not in text
    path.write_bytes(b"short\n")
    assert extract.stderr_tail(str(path)) == "short\n"
    assert extract.stderr_tail(str(tmp_path / "missing")) == ""
    path.write_bytes(b"y" * 70_000)  # cut in the middle of one long line: nothing is left, nothing is wrong
    assert extract.stderr_tail(str(path)) == ""


def test_a_spawn_that_fails_is_tool_failed(tmp_path):
    def boom(*args, **kwargs):
        raise jobobject.JobObjectError(5, "no")
    outcome, _ = call(tmp_path, deps={"spawn": boom})
    tool_failed_with(outcome, errors.HINTS["job_object_unavailable"])

    def cannot(*args, **kwargs):
        raise FileNotFoundError("no python")
    tool_failed(call(tmp_path / "second", deps={"spawn": cannot})[0])


def tool_failed_with(outcome, hint):
    failed(outcome, "TOOL_FAILED")
    assert outcome.hint == hint


# ---------------------------------------------------------------------------------------------- the memory limit
def test_a_hit_of_the_memory_limit_is_tool_failed_whatever_the_child_says_and_the_file_is_not_read(tmp_path, monkeypatch):
    reads = []
    real = extract.read_result
    monkeypatch.setattr(extract, "read_result", lambda *args: reads.append(args) or real(*args))
    spawn = FakeSpawn(good_effect, returncode=0, hit=True)  # exit 0, ok: true, a long text: and still a failure
    tool_failed(call(tmp_path, deps={"spawn": spawn})[0])
    assert reads == []


def test_a_peak_that_cannot_be_read_is_a_failure(tmp_path):
    class Blind(sup.FakeJob):
        def memory_limit_hit(self):
            raise jobobject.JobObjectError(6, "no")

    def spawn(argv, env, cwd, stdout, stderr, holder=None, limits=None):
        with open(argv[-1], encoding="utf-8") as handle:
            good_effect(argv, json.load(handle))
        pair = (sup.FakeProc(0), Blind())
        holder.append(pair)
        return pair
    tool_failed(call(tmp_path, deps={"spawn": spawn})[0])


def test_the_default_limit_is_judged_before_the_exit_code_and_the_text(tmp_path):
    spawn = FakeSpawn(None, returncode=1, hit=True)
    tool_failed(call(tmp_path, deps={"spawn": spawn})[0])


def test_the_real_engine_over_a_lowered_limit_is_tool_failed_never_text(tmp_path):
    python = engine_python()
    if python is None:
        pytest.skip("NO_ENGINE_PYTHON: no Python with trafilatura found; the real-engine memory case did NOT run")
    page = (FIXTURES / "long-200k.html").read_bytes()
    seen = []
    real_kill = jobobject.kill_tree

    def kill(job, proc):
        clean = real_kill(job, proc)
        seen.append((job.peak_memory(), proc.returncode))
        return clean
    base = {"extract_python": python, "kill_tree": kill}
    outcome, _ = call(tmp_path / "a", html=page, deps=dict(base, extract_job_limits=jobobject.JobLimits(None, 2)))
    assert outcome.status == "succeeded" and outcome.text
    peak = seen[-1][0]
    assert peak > 20 * MiB
    for fraction in (0.99, 0.97, 0.85, 0.5):  # just under the need the engine often swallows the refusal and exits 0
        seen.clear()
        limit = int(peak * fraction)
        outcome, _ = call(tmp_path / ("f%d" % int(fraction * 100)), html=page,
                          deps=dict(base, extract_job_limits=jobobject.JobLimits(limit, 2)))
        tool_failed(outcome)
        assert seen[-1][0] > limit, (fraction, seen)  # the core read a peak over the limit


def test_a_child_that_swallows_the_refusal_and_exits_zero_is_still_tool_failed(tmp_path):
    """A real job with a real limit and a child that catches the `MemoryError` (as the engine may): exit 0, a good result
    file, and a peak over the limit. The core says `TOOL_FAILED`."""
    body = ("import json, sys\n"
            "params = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            "try:\n"
            "    block = bytearray(300 * 1048576)\n"
            "except MemoryError:\n"
            "    pass\n"
            "open(params['out'], 'w').write(json.dumps({'v': 1, 'ok': True, 'text': %r, 'meta': {}, "
            "'charset': 'utf-8', 'guessed': False}))\n" % sup.GOOD_TEXT)
    seen = []
    real_kill = jobobject.kill_tree

    def kill(job, proc):
        clean = real_kill(job, proc)
        seen.append(proc.returncode)
        return clean
    deps = sup.fake_deps(tmp_path, body=body)
    deps.update(extract_job_limits=jobobject.JobLimits(128 * MiB, N), kill_tree=kill)
    tool_failed(call(tmp_path, deps=deps)[0])
    assert seen == [0]  # the child itself said all was well


# ---------------------------------------------------------------------------------------------- the process limit
def test_the_n_plus_first_process_cannot_be_made(tmp_path):
    marker = tmp_path / "m"
    body = ("import json, subprocess, sys\n"
            "params = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            "try:\n"
            "    subprocess.Popen([sys.executable, '-c', 'pass']).wait()\n"
            "    result = 'spawned'\n"
            "except OSError as exc:\n"
            "    result = 'blocked:%%s' %% exc.winerror\n"
            "open(%r, 'w').write(result)\n"
            "open(params['out'], 'w').write(json.dumps({'v': 1, 'ok': True, 'text': %r, 'meta': {}, "
            "'charset': 'utf-8', 'guessed': False}))\n" % (str(marker), sup.GOOD_TEXT))
    deps = sup.fake_deps(tmp_path, body=body)
    deps["extract_job_limits"] = jobobject.JobLimits(512 * MiB, N)
    outcome, _ = call(tmp_path, deps=deps)
    assert outcome.status == "succeeded" and marker.read_text() == "blocked:1816"
    marker.unlink()
    deps["extract_job_limits"] = jobobject.JobLimits(512 * MiB, N + 1)
    call(tmp_path / "again", deps=deps)
    assert marker.read_text() == "spawned"


# ----------------------------------------------------------------------------------------------------- the clock
SLEEPER = ("import sys, time\n"
           "open(sys.argv[1] + '.up', 'w').write('up')\n"
           "time.sleep(120)\n")


def test_a_child_that_sleeps_past_the_deadline_is_timeout_within_five_seconds_and_no_process_is_left(tmp_path, pids):
    states = []
    real_kill = jobobject.kill_tree

    def kill(job, proc):
        pids.append(proc.pid)
        clean = real_kill(job, proc)
        states.append((clean, job.active_processes()))
        return clean
    deps = sup.fake_deps(tmp_path, body=SLEEPER)
    deps["kill_tree"] = kill
    started = time.monotonic()
    outcome, _ = call(tmp_path, deps=deps, limits={"extract_timeout": 1})
    elapsed = time.monotonic() - started
    failed(outcome, "TIMEOUT")
    assert outcome.timeout_kind == "extract" and outcome.hint is None
    assert 0.9 <= elapsed <= 1 + 5
    assert states and states[0][0] is True and states[0][1] <= 1  # conhost may count; no engine process is left


def test_a_job_deadline_that_comes_first_is_a_job_timeout(tmp_path, pids):
    deps = sup.fake_deps(tmp_path, body=SLEEPER)
    real_kill = jobobject.kill_tree
    deps["kill_tree"] = lambda job, proc: (pids.append(proc.pid), real_kill(job, proc))[1]
    outcome, _ = call(tmp_path, deps=deps, deadline=time.monotonic() + 1, limits={"extract_timeout": 100})
    failed(outcome, "TIMEOUT")
    assert outcome.timeout_kind == "job"


def test_the_wait_is_a_loop_on_the_injected_clock_and_sleep(tmp_path):
    class Slow(sup.FakeProc):
        def __init__(self):
            super().__init__(None)
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls < 100 else self.returncode

    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += seconds

    def spawn(argv, env, cwd, stdout, stderr, holder=None, limits=None):
        pair = (Slow(), sup.FakeJob())
        holder.append(pair)
        return pair
    outcome, _ = call(tmp_path, deps={"spawn": spawn, "extract_sleep": sleep, "extract_poll": 1},
                      clock=lambda: clock["now"], limits={"extract_timeout": 10}, deadline=1e9)
    failed(outcome, "TIMEOUT")
    assert outcome.timeout_kind == "extract" and clock["now"] >= 10


def test_a_tree_that_cannot_be_emptied_is_tool_failed_with_the_warning(tmp_path):
    spawn = FakeSpawn(good_effect)
    outcome, _ = call(tmp_path, deps={"spawn": spawn, "kill_tree": lambda job, proc: False})
    tool_failed(outcome)
    assert outcome.warnings == ["process_tree_not_empty"]


def test_a_timeout_with_a_tree_that_stays_keeps_the_warning(tmp_path):
    class Slow(sup.FakeProc):
        def poll(self):
            return None

    def spawn(argv, env, cwd, stdout, stderr, holder=None, limits=None):
        pair = (Slow(), sup.FakeJob())
        holder.append(pair)
        return pair
    outcome, _ = call(tmp_path, deps={"spawn": spawn, "kill_tree": lambda job, proc: False, "extract_poll": 0.01},
                      limits={"extract_timeout": 0.05})
    failed(outcome, "TIMEOUT")
    assert outcome.warnings == ["process_tree_not_empty"]


# ------------------------------------------------------------------------------------------------ the folders
def test_a_junction_in_the_place_of_the_output_folder_is_tool_failed_and_the_target_is_not_read(tmp_path, monkeypatch):
    import _winapi
    target = tmp_path / "elsewhere"
    target.mkdir()
    write_result({"out": str(target / "result.json")})  # a good result, waiting behind the link
    reads = []
    real = extract.read_result
    monkeypatch.setattr(extract, "read_result", lambda *args: reads.append(args) or real(*args))

    def effect(argv, params):
        folder = os.path.dirname(params["out"])
        os.remove(argv[-1])
        os.rmdir(folder)
        _winapi.CreateJunction(str(target), folder)  # the folder is now a link to a place with a good result
    outcome, item_dir = call(tmp_path, deps={"spawn": FakeSpawn(effect)})
    tool_failed(outcome)
    assert reads == [] and (target / "result.json").exists()
    assert ytdlp.reparse_dir(os.path.join(item_dir, "x"))


def test_the_check_of_the_caller_sees_the_folders_and_may_veto(tmp_path):
    seen = []

    def check(path):
        seen.append(os.path.basename(path))
        if os.path.basename(path) == "x":
            raise OSError("no")
    outcome, _ = call(tmp_path, deps={"spawn": FakeSpawn(good_effect)}, check=check)
    tool_failed(outcome)
    assert "x" in seen


def test_the_defaults_come_from_the_limits_module():
    assert limits_mod.DEFAULTS["extract_timeout"] == 30 and limits_mod.DEFAULTS["max_extract_chars"] == 200_000
    assert extract.MAX_HTML_BYTES == 5 * MiB


# ------------------------------------------------------------------------------------------------------ Ctrl+C
def test_a_ctrl_c_after_the_spawn_ends_the_tree_and_closes_the_job(tmp_path):
    killed = []

    def spawn(argv, env, cwd, stdout, stderr, holder=None, limits=None):
        pair = (sup.FakeProc(None), sup.FakeJob())
        holder.append(pair)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        call(tmp_path, deps={"spawn": spawn, "kill_tree": lambda job, proc: killed.append(job) or True})
    assert len(killed) == 1 and killed[0].closed is True


def test_a_ctrl_c_with_a_tree_that_stays_says_so(tmp_path):
    def spawn(argv, env, cwd, stdout, stderr, holder=None, limits=None):
        holder.append((sup.FakeProc(None), sup.FakeJob()))
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt) as raised:
        call(tmp_path, deps={"spawn": spawn, "kill_tree": lambda job, proc: False})
    assert raised.value.process_tree_not_empty is True


def test_a_ctrl_c_in_the_wait_of_a_real_child_leaves_no_process(tmp_path, pids):
    state = {"n": 0}

    def sleep(seconds):
        state["n"] += 1
        if state["n"] == 3:
            raise KeyboardInterrupt
        time.sleep(seconds)
    deps = sup.fake_deps(tmp_path, body=SLEEPER)
    real_kill = jobobject.kill_tree
    deps.update(extract_sleep=sleep, kill_tree=lambda job, proc: (pids.append(proc.pid), real_kill(job, proc))[1])
    with pytest.raises(KeyboardInterrupt):
        call(tmp_path, deps=deps)
    assert pids and procs.wait_dead(pids, 5) == []


def test_a_page_that_cannot_be_read_is_not_html(tmp_path):
    assert extract.read_head(str(tmp_path / "missing.html")) == b""
    outcome = extract.run_extract_item(str(tmp_path / "missing.html"), str(tmp_path), None, LIMITS, {}, {},
                                       time.monotonic, 1e9)
    failed(outcome, "CONTENT_MISMATCH", "not_html")


def test_the_page_limits_are_the_lowest_of_three_caps():
    base = limits_mod.resolve({})[0]
    assert extract.page_limits(base)["max_html_bytes"] == 5 * MiB == extract.page_limits(base)["max_bytes_per_file"]
    low = limits_mod.resolve({"max_html_bytes": 1000})[0]
    assert extract.page_limits(low)["max_bytes_per_file"] == 1000 and low["max_bytes_per_file"] > 1000
