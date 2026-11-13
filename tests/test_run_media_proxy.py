"""SPR-03 TK20/TK21 (FR13.6-FR13.8, FR13.10, SC14, SC17): `runner.run` gives every media item its own egress proxy.
The engine is the fixture `fake_ytdlp.py` (`via_proxy` and friends CONNECT through the `--proxy` of their argv); the
"Internet" is the local origin of `test_egress`. No real network."""

import os
import re
import socket

import pytest

from agent_crawl_studio import artifacts, egress
from agent_crawl_studio.urlpolicy import Policy

from test_egress import PUBLIC, _Origin, _Rig
from test_run_batch import go, job_dir_of  # noqa: F401
from test_netfetch import policy_for, server  # noqa: F401
from test_run_batch import world  # noqa: F401
from test_run_media import YT, media_deps, tmp_dirs

PROXY_URL = re.compile(r"^http://[0-9a-f]{64}:x@127\.0\.0\.1:[0-9]{1,5}$")
NAMES = {"www.youtube.com": PUBLIC, "cdn.example.com": PUBLIC, "internal.example.com": "10.0.0.1"}


class Rig(_Rig):
    names = NAMES

    def resolver(self, host, port, proto=0):
        self.lookups.append(host)
        if host not in self.names:
            raise socket.gaierror(11001, "no such name")
        ip = self.names[host]
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    def factory(self):
        """The `egress` dep: a real EgressProxy whose resolver and "Internet" are the rig's."""
        def make(policy, *, byte_cap, connect_timeout, log):
            proxy = egress.EgressProxy(Policy(ports=(443,), resolver=self.resolver), byte_cap=byte_cap,
                                       connect_timeout=connect_timeout, log=log, backend_of=self.backend_of)
            self.proxies.append(proxy)
            return proxy
        return make


@pytest.fixture
def rig():
    origin = _Origin(push=b"x" * 150000)
    r = Rig(origin)
    yield r
    for proxy in r.proxies:
        proxy.stop()
    origin.stop()


def run(world, server, rig, urls, scenario, spawned=None, **extra):
    deps = media_deps(scenario, spawned=spawned, egress=rig.factory(), **extra)
    return go(world, server, urls, deps=deps, mode="media", limits={"max_bytes_per_file": 100000})


def test_the_engine_gets_the_per_item_proxy_and_no_dns_probe_runs(world, server, rig):
    spawned, probed = [], []
    result, code = run(world, server, rig, [YT], "via_proxy", spawned=spawned,
                       dns_probe=lambda host: probed.append(host))
    argv = spawned[0]
    assert code == 0 and result["items"][0]["status"] == "succeeded" and probed == []
    assert PROXY_URL.match(argv[argv.index("--proxy") + 1]) and argv[argv.index("--proxy") + 1] == rig.proxies[0].url
    assert "[protocol=https]" in argv[argv.index("-f") + 1]
    assert tmp_dirs(world, result) == []


def test_the_log_has_one_line_per_connect_and_never_the_token(world, server, rig):
    result, code = run(world, server, rig, [YT], "via_proxy")
    job = job_dir_of(world, result)
    with open(os.path.join(job, "logs", "egress.log"), encoding="utf-8") as handle:
        assert handle.read().splitlines() == ["www.youtube.com 443 allow 150000"]
    token = rig.proxies[0].url.split("//")[1].split(":")[0]
    for folder, _, names in os.walk(job):
        for name in names:
            with open(os.path.join(folder, name), "rb") as handle:
                assert token.encode() not in handle.read(), name


def test_a_redirect_to_an_internal_host_is_blocked_and_nothing_is_reached(world, server, rig):
    result, code = run(world, server, rig, [YT], "redirect_internal")
    item = result["items"][0]
    assert item["status"] == "blocked" and item["error"]["code"] == "REDIRECT_BLOCKED" and rig.pinned == []
    token = rig.proxies[0].url.split("//")[1].split(":")[0]
    job = job_dir_of(world, result)
    for name in os.listdir(os.path.join(job, "logs")):  # the engine printed its proxy URL: the log hides the token
        with open(os.path.join(job, "logs", name), encoding="utf-8") as handle:
            assert token not in handle.read()


def test_a_refused_input_host_is_url_blocked(world, server, rig):
    rig.names = dict(NAMES, **{"www.youtube.com": "10.0.0.1"})
    result, code = run(world, server, rig, [YT], "via_proxy")
    assert result["items"][0]["error"]["code"] == "URL_BLOCKED" and result["items"][0]["status"] == "blocked"
    assert rig.pinned == []


def test_bytes_past_twice_the_per_file_cap_are_a_size_limit(world, server, rig):
    rig.origin.push = b"x" * 400000
    result, code = run(world, server, rig, [YT], "via_proxy_then_hang")
    assert result["items"][0]["error"]["code"] == "SIZE_LIMIT" and rig.proxies[0].over_cap.is_set()
    assert tmp_dirs(world, result) == []


def test_each_item_counts_its_own_bytes(world, server, rig):
    result, code = run(world, server, rig, [YT, YT], "via_proxy")  # 1.5 x the per-file cap each, 3 x together
    assert code == 0 and [i["status"] for i in result["items"]] == ["succeeded", "succeeded"]
    assert len(rig.proxies) == 2 and all(p.bytes_in == 150000 for p in rig.proxies)


def test_ctrl_c_closes_the_proxy(world, server, rig, monkeypatch):
    def boom(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(artifacts.ArtifactWriter, "adopt", boom)
    result, code = run(world, server, rig, [YT], "via_proxy")
    port = int(rig.proxies[0].url.rsplit(":", 1)[1])
    assert code == 130
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


def test_a_job_without_a_media_item_opens_no_port(world, server, rig):
    made = []
    result, code = go(world, server, [server.url("/ok")], deps=media_deps(egress=lambda *a, **k: made.append(1), policy=policy_for(server)),
                      mode="auto")
    assert code == 0 and made == []
