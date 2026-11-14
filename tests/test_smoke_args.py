"""SPR-03 TK13 (FR17.1): `scripts/smoke_download.py` is a thin layer over `runner.run` / `runner.plan`. It builds a
one-URL request (mode `media`), calls the core, and maps the answer: succeeded -> ok, a missing tool or a name that
does not resolve -> BLOCKED_ENV, anything else -> FAIL, `--plan-only` -> NOT_RUN. The core is replaced through
`deps` only (a fake engine process, a fake DNS probe); no network."""

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest

from agent_crawl_studio import config, urlpolicy

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("smoke_download", ROOT / "scripts" / "smoke_download.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
FAKE = str(ROOT / "tests" / "fixtures" / "fake_ytdlp.py")
FFMPEG = r"C:\tools\ffmpeg\bin\ffmpeg.exe"
NODE = r"C:\Program Files\nodejs\node.exe"
FAKES = dict(attrs_of=lambda path: None, drive_type_of=lambda path: 3, sync_root_of=lambda path: False,
             free_bytes_of=lambda path: 10 ** 15)


def which(cmd, path=None):
    return {"ffmpeg": FFMPEG, "node": NODE}.get(cmd)


def env_for(tmp_path):
    return {"CRAWL_STUDIO_WORK": str(tmp_path / "root"), "OPCOS_ROOT": str(tmp_path / "o"),
            "USERPROFILE": str(tmp_path / "h"), "PATH": "x"}


def deps_for(scenario="ok_mp4", dns=urlpolicy.DNS_OK, **extra):
    deps = dict(FAKES, which=which, version_of=lambda name: "2026.10.26", dns_probe=lambda host: dns,
                engine_prefix=lambda: [sys.executable, "-P", FAKE, scenario], poll=0.05, sleep=lambda s: None)
    deps.update(extra)
    return deps


def go(tmp_path, argv=None, deps=None):
    out = io.StringIO()
    rc = smoke.main(argv or ["--url", URL, "--work", "smoke"], out=out, env=env_for(tmp_path),
                    deps=deps if deps is not None else deps_for())
    text = out.getvalue()
    assert text.count("\n") == 1
    return rc, json.loads(text)


def test_a_succeeded_job_is_ok_and_names_the_file(tmp_path):
    rc, obj = go(tmp_path)
    assert rc == 0 and obj["status"] == "ok" and obj["bytes"] > 0 and obj["file"].startswith("files/0001-")


def test_a_missing_tool_is_blocked_env(tmp_path):
    rc, obj = go(tmp_path, deps=deps_for(which=lambda cmd, path=None: None))
    assert rc == 1 and obj["status"] == "BLOCKED_ENV" and obj["error"]["code"] == "DEPENDENCY_MISSING"


class Unresolved:
    """A stand-in egress proxy that let the CONNECT through but could not resolve the name (RF1): the engine only
    saw `502 Bad Gateway`, the proxy's own record says why."""
    over_cap = type("E", (), {"is_set": staticmethod(lambda: False)})()
    bytes_in = 0
    denied = []
    failures = [("www.youtube.com", "dns_failed")]
    url = "http://%s:x@127.0.0.1:5" % ("ab" * 32)

    def start(self):
        return self.url

    def stop(self, timeout=2.0):
        pass


def test_a_name_that_does_not_resolve_is_blocked_env(tmp_path):
    rc, obj = go(tmp_path, deps=deps_for("fail_tunnel_502", egress=lambda *a, **k: Unresolved()))
    assert rc == 1 and obj["status"] == "BLOCKED_ENV" and obj["error"]["code"] == "NETWORK_ERROR"


class Refusing:
    """A stand-in egress proxy that refused the input host (FR13.10)."""
    over_cap = type("E", (), {"is_set": staticmethod(lambda: False)})()
    bytes_in = 0
    denied = [("www.youtube.com", "ip_not_public")]
    url = "http://%s:x@127.0.0.1:5" % ("ab" * 32)

    def start(self):
        return self.url

    def stop(self, timeout=2.0):
        pass


def test_a_non_public_address_is_a_fail_url_blocked(tmp_path):
    rc, obj = go(tmp_path, deps=deps_for("fail_403", egress=lambda *a, **k: Refusing()))
    assert rc == 1 and obj["status"] == "FAIL" and obj["error"]["code"] == "URL_BLOCKED"


def test_an_engine_failure_is_a_fail_and_prints_nothing_of_its_text(tmp_path):
    rc, obj = go(tmp_path, deps=deps_for("stderr_secret"))
    text = json.dumps(obj)
    assert rc == 1 and obj["status"] == "FAIL" and "SECRET" not in text and "tok123" not in text


def test_a_url_outside_the_media_hosts_is_a_fail_url_blocked(tmp_path):
    rc, obj = go(tmp_path, argv=["--url", "https://evil.example.com/v.mp4", "--work", "smoke"])
    assert rc == 1 and obj["status"] == "FAIL" and obj["error"]["code"] == "URL_BLOCKED"


def test_plan_only_is_not_run_and_runs_nothing(tmp_path):
    calls = []
    deps = deps_for(spawn=lambda *a, **k: calls.append(a))
    rc, obj = go(tmp_path, argv=["--url", URL, "--work", "smoke", "--plan-only"], deps=deps)
    assert rc == 1 and obj["status"] == "NOT_RUN" and obj["decision"] == "allow" and calls == []
    assert not (tmp_path / "root" / "jobs").exists()


def test_a_ctrl_c_exits_130_with_a_fixed_error(tmp_path):
    def stop(*args, **kwargs):
        raise KeyboardInterrupt

    rc, obj = go(tmp_path, deps=deps_for(egress=stop))
    assert rc == 130 and obj["error"]["code"] == "INTERRUPTED"


def test_bad_arguments_exit_2_one_json_line():
    out = io.StringIO()
    rc = smoke.main(["--nope"], out=out)
    assert rc == 2 and json.loads(out.getvalue())["error"]["code"] == "USAGE_ERROR"


def test_a_bad_work_folder_is_refused_by_the_core_and_creates_nothing(tmp_path):
    rc, obj = go(tmp_path, argv=["--url", URL, "--work", "..\\..\\escape"])
    assert rc == 1 and obj["status"] == "FAIL" and not (tmp_path / "escape").exists()


@pytest.mark.parametrize("addrs,expected", [
    (["142.250.1.1"], urlpolicy.DNS_OK), (["10.0.0.5"], urlpolicy.DNS_BLOCKED),
    (["::ffff:127.0.0.1"], urlpolicy.DNS_BLOCKED), (["142.250.1.1", "10.0.0.5"], urlpolicy.DNS_BLOCKED)])
def test_dns_probe_classifies_addresses(addrs, expected):
    resolver = lambda host, port, **kw: [(2, 1, 6, "", (a, port)) for a in addrs]  # noqa: E731
    assert urlpolicy.dns_probe("www.youtube.com", resolver=resolver) == expected


def test_config_example_allowlist_matches_the_constant():
    cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert tuple(cfg["media_host_allowlist"]) == config.DEFAULT_MEDIA_HOSTS
