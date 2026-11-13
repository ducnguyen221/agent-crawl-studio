"""FR6.1 / TK16: `scripts/smoke_download.py` uses the core for the IP rule, the DNS probe, the media host list and
the folder confinement; there is no second copy of any of them. RED until the smoke script imports the core."""

import io
import json
import re
from pathlib import Path

import pytest

from agent_crawl_studio import config, outroot, urlpolicy

from test_smoke_args import (ROOT, URL, Recorder, env_for, go, job, smoke, which, work_dir)  # noqa: F401

# the exact pattern of SPEC SC8 / RF6: any of these in scripts/ is a second copy of the policy
OWN_COPY = re.compile(r"def _public\b|def default_dns_probe\b|def _confined_work\b|^ALLOWLIST =", re.M)


def test_no_script_carries_its_own_copy_of_the_policy():
    found = [(path.name, match.group(0)) for path in (ROOT / "scripts").glob("*.py")
             for match in OWN_COPY.finditer(path.read_text(encoding="utf-8"))]
    assert found == []


def test_the_smoke_dns_probe_is_the_core_one_looked_up_at_call_time(tmp_path, monkeypatch):
    seen = []

    def blocked(host, *args, **kwargs):
        seen.append(host)
        return urlpolicy.DNS_BLOCKED
    monkeypatch.setattr(urlpolicy, "dns_probe", blocked)
    out = io.StringIO()
    rec = Recorder()
    rec.work = work_dir(tmp_path)
    rc = smoke.main(["--url", URL, "--work", str(work_dir(tmp_path))], out=out, run=rec, which=which,
                    env=env_for(tmp_path), now=lambda: __import__("datetime").datetime(2026, 11, 13, 1, 2, 3), rand=lambda: "abc123")
    obj = json.loads(out.getvalue())
    assert rc == 1 and obj["status"] == "FAIL" and obj["error"]["code"] == "URL_BLOCKED"
    assert seen == ["www.youtube.com"] and rec.calls == []


def test_the_dns_constants_are_the_core_ones():
    assert (smoke.DNS_OK, smoke.DNS_FAIL, smoke.DNS_BLOCKED) == (urlpolicy.DNS_OK, urlpolicy.DNS_FAIL, urlpolicy.DNS_BLOCKED)


def test_the_media_host_list_is_the_core_default_read_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_MEDIA_HOSTS", ("media.example.org",))
    rec = Recorder()
    rc, obj = go(tmp_path, rec)  # a YouTube URL is not on the (replaced) list any more
    assert rc == 1 and obj["error"]["code"] == "URL_BLOCKED" and rec.calls == []
    rec = Recorder()
    rc, obj = go(tmp_path, rec, argv=["--url", "https://media.example.org/v", "--work", str(work_dir(tmp_path))])
    assert obj["status"] == "ok"


def test_the_work_folder_check_is_the_core_one(tmp_path, monkeypatch):
    seen = []

    def refuse(arg, work_root, **kwargs):
        seen.append((arg, work_root))
        return None
    monkeypatch.setattr(outroot, "confined_work", refuse)
    rec = Recorder()
    rc, obj = go(tmp_path, rec)
    assert rc == 1 and obj["error"]["code"] == "OUTPUT_PATH_BLOCKED" and rec.calls == []
    assert seen == [(str(work_dir(tmp_path)), str(tmp_path / "root"))]
