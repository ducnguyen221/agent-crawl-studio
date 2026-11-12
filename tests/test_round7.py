"""Review of the TK13 round-5 fixes and of TK16 / round 6 (RF160-RF189): a refused ninth lookup is refused at once, a
stream that expires is closed, an injected client cannot send its own Cookie or Authorization, Ctrl+C can no longer
leave run() anywhere, a placed file or a temp file never outlives a Ctrl+C, a refused job removes only its own
folders (a junction is removed as a link), a dead log does not touch the job, stdout equals manifest.json."""

import io
import json
import os
import time

import httpcore
import httpx
import pytest

from agent_crawl_studio import cli, outroot, runner
from agent_crawl_studio.urlpolicy import Policy

from test_netfetch import LOOP, Resolver, Sink, lim, nf, policy_for  # noqa: F401
from test_netfetch import server as netfetch_server  # noqa: F401
from test_netfetch_round2 import SlowInner, three_addresses
from test_netfetch_round3 import FakeStream, backend_free
from test_run_batch import files_of, go, job_dir_of, manifest_of, request_of, server, world  # noqa: F401
from test_round6 import interrupt, statuses


# ----------------------------------------------------------------------------- netfetch
def test_a_ninth_lookup_is_refused_at_once_even_with_a_long_timeout(nf):
    import threading
    release = threading.Event()
    resolver = Resolver({"slow.example": [lambda: (release.wait(5), [LOOP])[1]]})
    backend = nf.PinnedBackend(Policy(ports=(80,), ip_ok=lambda a: True, resolver=resolver))
    for _ in range(nf.MAX_RESOLVER_THREADS):
        with pytest.raises(nf.NetError):
            backend.connect_tcp("slow.example", 80, timeout=0.05)
    started = time.monotonic()
    with pytest.raises(nf.NetError) as caught:
        backend.connect_tcp("slow.example", 80, timeout=3)  # it must not wait for a free place
    assert caught.value.code == "TIMEOUT" and time.monotonic() - started < 1.0
    assert len(resolver.calls) == nf.MAX_RESOLVER_THREADS
    release.set()
    for _ in range(100):
        if backend_free(nf):
            break
        time.sleep(0.05)


def test_a_stream_that_expires_before_the_tls_handshake_is_closed_first(nf):
    backend = nf.PinnedBackend(Policy())
    inner = FakeStream()
    stream = nf.DeadlineStream(inner, backend)
    backend.deadline = time.monotonic() - 1
    with pytest.raises(httpcore.ReadTimeout):
        stream.start_tls(None, "h")
    assert inner.closed and ("tls", None) not in inner.calls


def test_the_connect_phase_uses_the_time_left_before_the_deadline(nf):
    resolver = Resolver({"pinned.example": [["203.0.113.1"]]})
    backend = nf.PinnedBackend(Policy(ports=(80,), ip_ok=lambda address: True, resolver=resolver))
    backend._inner = SlowInner()
    backend.deadline = time.monotonic() + 0.3
    with pytest.raises(httpcore.ConnectTimeout):
        backend.connect_tcp("pinned.example", 80, timeout=5)
    assert backend._inner.calls[0][1] <= 0.3
    backend.deadline = time.monotonic() - 1
    with pytest.raises(httpcore.ReadTimeout):
        backend.connect_tcp("pinned.example", 80, timeout=5)
    assert resolver.calls == ["pinned.example"]  # nothing was asked after the deadline


def test_cookie_and_authorization_headers_of_an_injected_client_are_removed(nf, no_socket):
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, headers={"Content-Length": "2"}, stream=httpx.ByteStream(b"ok"))
    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"Cookie": "sid=leak", "Authorization": "Bearer leak"})
    out = nf.fetch_item("https://files.example.com/a", "auto", lim(), Sink(), Policy(resolver=Resolver()), client=client)
    assert out.status == "ok" and len(seen) == 1
    assert "cookie" not in seen[0] and "authorization" not in seen[0] and "leak" not in json.dumps(seen[0])


# ----------------------------------------------------------------------------- RF180: Ctrl+C anywhere in run()
def test_ctrl_c_in_the_part_check_cancels_the_job(world, server):
    def attrs(path):
        if path.endswith(".part"):
            raise KeyboardInterrupt
    result, code = go(world, server, [server.url("/ok")] * 2, deps={"attrs_of": attrs})
    assert code == 130 and result["status"] == "cancelled" and statuses(result) == ["cancelled"] * 2
    assert os.listdir(os.path.join(job_dir_of(world, result), ".tmp")) == [] and manifest_of(world, result) == result


def test_ctrl_c_in_the_url_check_cancels_the_job(world, server, monkeypatch):
    def boom(url, policy=None):
        raise KeyboardInterrupt
    monkeypatch.setattr(runner.urlpolicy, "check_url", boom)
    result, code = go(world, server, [server.url("/ok")] * 2)
    assert code == 130 and statuses(result) == ["cancelled"] * 2 and manifest_of(world, result) == result


def test_ctrl_c_while_the_job_is_being_set_up_removes_the_job_and_says_interrupted(world, server):
    calls = []

    def replace(src, dst):
        calls.append(dst)
        interrupt()
    result, code = go(world, server, [server.url("/ok")], deps={"replace": replace})
    assert code == 130 and result["job_id"] is None and result["error"]["code"] == "INTERRUPTED"
    assert os.listdir(os.path.join(world["work"], "jobs")) == []  # no job folder, no temp manifest left behind


# ----------------------------------------------------------------------------- RF181 / RF182
def test_a_file_placed_by_a_rename_that_is_then_interrupted_is_taken_away_again(world, server):
    def rename_then_interrupt(src, dst):
        os.rename(src, dst)
        interrupt()
    result, code = go(world, server, [server.url("/ok")] * 2, deps={"rename": rename_then_interrupt})
    assert code == 130 and statuses(result) == ["cancelled"] * 2
    assert files_of(world, result) == [] and os.listdir(os.path.join(job_dir_of(world, result), ".tmp")) == []


def test_ctrl_c_inside_the_replace_leaves_no_temp_manifest(world, server):
    calls = []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            interrupt()
        os.replace(src, dst)
    result, code = go(world, server, [server.url("/ok")] * 2, deps={"replace": replace})
    assert code == 130
    assert [name for name in os.listdir(job_dir_of(world, result)) if name.endswith(".tmp")] == []


# ----------------------------------------------------------------------------- RF183: a junction is removed as a link
def test_a_refused_job_never_deletes_what_a_junction_points_to(world, server, tmp_path):
    import _winapi
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "marker.txt").write_text("do not delete", encoding="utf-8")
    real_create = outroot.create_job

    def create_then_plant_a_junction(root, job_id, **kw):
        job = real_create(root, job_id, **kw)
        _winapi.CreateJunction(str(precious), os.path.join(job, ".tmp"))
        return job
    result, code = go(world, server, [server.url("/ok")],
                      deps={"create_job": create_then_plant_a_junction, "attrs_of": outroot.default_attrs_of})
    assert code == 2 and result["error"]["code"] == "OUTPUT_PATH_BLOCKED"
    assert (precious / "marker.txt").read_text(encoding="utf-8") == "do not delete"
    assert os.listdir(os.path.join(world["work"], "jobs")) == []


# ----------------------------------------------------------------------------- RF184 / RF189: the run log
def test_a_log_that_fails_its_check_does_not_touch_the_job(world, server):
    def attrs(path):
        return (0x400, 0) if path.endswith("run.log") else None
    result, code = go(world, server, [server.url("/ok")] * 2, deps={"attrs_of": attrs})
    assert code == 0 and result["status"] == "succeeded"
    assert not os.path.exists(os.path.join(job_dir_of(world, result), "logs", "run.log"))


def test_the_run_log_keeps_its_handle_and_does_not_reopen_by_path(tmp_path):
    (tmp_path / "job").mkdir()
    log = runner.RunLog(str(tmp_path / "job"), lambda path: None)
    log.write("a")
    moved = log.path + ".moved"
    os.rename(log.path, moved)
    open(log.path, "w", encoding="utf-8").close()  # something else now sits at the path
    log.write("b")
    log.close()
    assert open(moved, encoding="utf-8").read() == "a\nb\n" and open(log.path, encoding="utf-8").read() == ""


# ----------------------------------------------------------------------------- RF185 / RF187
def test_a_free_space_probe_that_fails_while_choosing_the_place_refuses_the_job(world, server):
    def free(path):
        raise OSError("probe")
    result, code = go(world, server, [server.url("/ok")], deps={"free_bytes_of": free})
    assert code == 2 and result["job_id"] is None and result["error"]["code"] == "OUTPUT_WRITE_FAILED"


def test_stdout_and_manifest_json_are_the_same_bytes(tmp_path):
    out = io.StringIO()
    path = tmp_path / "request.json"
    path.write_bytes(json.dumps({"schema_version": 1, "urls": ["http://127.0.0.1/a", "ftp://x.example/b"]}).encode("utf-8"))
    env = {"CRAWL_STUDIO_WORK": str(tmp_path / "work"), "OPCOS_ROOT": str(tmp_path / "opcos")}
    rc = cli.main(["run", "--request", str(path)], out=out, env=env)
    job_id = json.loads(out.getvalue())["job_id"]
    on_disk = (tmp_path / "work" / "jobs" / job_id / "manifest.json").read_bytes()
    assert rc == 1 and out.getvalue().encode("ascii") == on_disk + b"\n"
