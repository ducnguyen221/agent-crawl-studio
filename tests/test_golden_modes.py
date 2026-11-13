"""SPR-04 TK8 (SPEC FR21.5, SC29; `tests/fixtures/golden/README.md`): the plan and the result of the `auto` and `direct`
modes equal the samples written before any SPR-04 change, after the normalisation of the README, but for exactly three
differences the SPEC declares in section 7: the two new keys of `limits_effective` and `network_bytes` of the result.

`normalize` is the one function for this comparison (the result part is also used by `test_network_budget.py`); it
builds on `gen_golden.normalise`, which makes the constants of the README. TK9 adds the run part here."""

import copy
import json
import os
import socket

import pytest

from agent_crawl_studio import runner, urlpolicy
from fixtures import httpd
from fixtures.golden import gen_golden

GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures", "golden")
NEW_LIMITS = ("extract_timeout", "max_extract_chars")


def normalize(doc, port, work, job_id=None):
    """`doc` after the constants of the golden README, without the three differences SPEC section 7 allows (the two
    new `limits_effective` keys and `network_bytes`). Anything else that differs stays and fails the comparison."""
    clean = gen_golden.normalise(doc, port, work, job_id)
    for key in NEW_LIMITS:
        clean.get("limits_effective", {}).pop(key, None)
    clean.pop("network_bytes", None)
    return clean


def golden(kind, mode):
    with open(os.path.join(GOLDEN, "%s-%s.json" % (kind, mode)), encoding="utf-8") as handle:
        return json.load(handle)


def policy_for(server):
    return urlpolicy.Policy(
        ports=(server.port,), ip_ok=lambda address: address == "127.0.0.1",
        resolver=lambda host, port, proto=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])


@pytest.mark.parametrize("mode", ["auto", "direct"])
def test_the_plan_of_auto_and_direct_equals_the_golden_sample_but_for_the_two_new_limits(tmp_path, mode):
    work = str(tmp_path / "work")
    env = dict(os.environ, CRAWL_STUDIO_WORK=work)
    with httpd.LocalServer() as server:
        request = gen_golden.build(mode, server)
        planned = runner.plan(request, env, {"policy": policy_for(server)})
        assert all(planned["limits_effective"][key] == value
                   for key, value in (("extract_timeout", 30), ("max_extract_chars", 200_000)))
        assert normalize(planned, server.port, work) == golden("plan", mode)


@pytest.mark.parametrize("mode", ["auto", "direct"])
def test_the_result_of_auto_and_direct_equals_the_golden_sample_but_for_the_declared_differences(tmp_path, mode):
    """SPR-04 TK9 (FR21.5, SC29): the `run` of the two modes, now that `run` knows the extract mode, still gives the
    sample (after `normalize`), and no artifact of these modes carries `role`, `untrusted`, `chars` or `truncated`."""
    work = str(tmp_path / "work")
    env = dict(os.environ, CRAWL_STUDIO_WORK=work)
    with httpd.LocalServer() as server:
        request = gen_golden.build(mode, server)
        result, code = runner.run(request, env, {"policy": policy_for(server)})
        assert code == 0 and isinstance(result["network_bytes"], int) and result["network_bytes"] >= 0
        assert normalize(result, server.port, work, result["job_id"]) == golden("result", mode)
    assert all(item["adapter"] == "direct-http" for item in result["items"])
    for item in result["items"]:
        for artifact in item["artifacts"]:
            assert not {"role", "untrusted", "chars", "truncated"} & set(artifact)


@pytest.mark.parametrize("mode", ["auto", "direct"])
def test_the_golden_plan_items_carry_no_extract_marks(mode):
    sample = golden("plan", mode)
    assert all(item["adapter"] == "direct-http" for item in sample["items"])
    assert not any(key in sample["limits_effective"] for key in NEW_LIMITS)


def test_normalize_drops_only_the_three_declared_differences():
    doc = {"limits_effective": {"max_items": 100, "extract_timeout": 30, "max_extract_chars": 200000},
           "network_bytes": 5, "items": [{"adapter": "direct-http", "role": "extract"}]}
    clean = normalize(copy.deepcopy(doc), 1, "C:/w", None)
    assert clean == {"limits_effective": {"max_items": 100}, "items": [{"adapter": "direct-http", "role": "extract"}]}
    assert normalize({"items": []}, 1, "C:/w") == {"items": []}
