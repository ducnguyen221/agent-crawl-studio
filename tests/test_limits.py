"""SPEC section 9 / FR1.3: default limits, hard caps, validation, honest warnings."""

import pytest

from agent_crawl_studio import limits
from agent_crawl_studio.errors import ContractError

GiB = 2 ** 30
MiB = 2 ** 20

DEFAULTS = {
    "max_items": 100, "max_bytes_per_file": 2 * GiB, "max_total_bytes": 10 * GiB, "max_artifacts_per_item": 10,
    "concurrency": 3, "per_host_concurrency": 2, "connect_timeout": 15, "idle_timeout": 60,
    "item_timeout": 600, "media_item_timeout": 2700, "job_timeout": 4 * 3600, "retries": 3, "max_redirects": 5,
    "max_html_bytes": 10 * MiB,
}
CAPS = {
    "max_items": 1000, "max_bytes_per_file": 20 * GiB, "max_total_bytes": 100 * GiB, "max_artifacts_per_item": 50,
    "concurrency": 8, "per_host_concurrency": 4, "connect_timeout": 60, "idle_timeout": 300,
    "item_timeout": 4 * 3600, "media_item_timeout": 14400, "job_timeout": 24 * 3600, "retries": 5,
    "max_redirects": 10, "max_html_bytes": 50 * MiB,
}


def test_defaults_match_the_spec_table():
    assert limits.DEFAULTS == DEFAULTS
    assert limits.HARD_CAPS == CAPS


def test_resolve_none_and_empty_give_defaults():
    for given in (None, {}):
        effective, warnings = limits.resolve(given)
        assert effective == DEFAULTS
        assert sorted(warnings) == ["concurrency_not_applied", "retries_not_applied"]


def test_resolve_overrides_one_key_and_keeps_others():
    effective, _ = limits.resolve({"max_items": 7, "connect_timeout": 2.5})
    assert effective["max_items"] == 7 and effective["connect_timeout"] == 2.5
    assert effective["idle_timeout"] == DEFAULTS["idle_timeout"]


@pytest.mark.parametrize("key", sorted(CAPS))
def test_value_at_the_cap_is_accepted_and_above_is_rejected(key):
    effective, _ = limits.resolve({key: CAPS[key]})
    assert effective[key] == CAPS[key]
    with pytest.raises(ContractError) as info:
        limits.resolve({key: CAPS[key] + 1})
    assert (info.value.code, info.value.path, info.value.why) == ("INVALID_REQUEST", "$.limits." + key, "out_of_range")


@pytest.mark.parametrize("key", sorted(CAPS))
@pytest.mark.parametrize("bad", [0, -1])
def test_zero_and_negative_are_rejected(key, bad):
    with pytest.raises(ContractError) as info:
        limits.resolve({key: bad})
    assert info.value.why == "out_of_range"


@pytest.mark.parametrize("bad", [True, False, "5", None, [1], {"a": 1}])
def test_wrong_types_are_rejected_for_every_key(bad):
    for key in CAPS:
        with pytest.raises(ContractError) as info:
            limits.resolve({key: bad})
        assert info.value.why == "wrong_type" and info.value.path == "$.limits." + key


@pytest.mark.parametrize("key", ["max_items", "max_bytes_per_file", "concurrency", "max_redirects"])
def test_count_and_byte_limits_must_be_whole_numbers(key):
    with pytest.raises(ContractError) as info:
        limits.resolve({key: 1.5})
    assert info.value.why == "wrong_type"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_timeouts_are_rejected(bad):
    with pytest.raises(ContractError):
        limits.resolve({"connect_timeout": bad})


def test_unknown_key_is_rejected_and_the_key_name_is_not_in_the_path():
    with pytest.raises(ContractError) as info:
        limits.resolve({"SECRETKEY": 1})
    assert (info.value.path, info.value.why) == ("$.limits", "unknown_key") and "SECRETKEY" not in info.value.hint


def test_limits_must_be_an_object():
    for bad in ([], "x", 5, True):
        with pytest.raises(ContractError) as info:
            limits.resolve(bad)
        assert (info.value.path, info.value.why) == ("$.limits", "wrong_type")


def test_concurrency_and_retries_warn_even_when_explicit():
    _, warnings = limits.resolve({"concurrency": 1, "retries": 1})
    assert sorted(warnings) == ["concurrency_not_applied", "retries_not_applied"]


def test_required_free_bytes_formula_of_fr3_7():
    effective, _ = limits.resolve({"max_total_bytes": 10 * GiB, "max_bytes_per_file": 2 * GiB})
    assert limits.required_free_bytes(effective, 1) == 5 * GiB + 2 * GiB
    assert limits.required_free_bytes(effective, 3) == 5 * GiB + 6 * GiB
    assert limits.required_free_bytes(effective, 100) == 5 * GiB + 10 * GiB


def test_media_item_timeout_is_its_own_key_with_a_longer_default():
    effective, _ = limits.resolve({})
    assert effective["media_item_timeout"] == 2700 and effective["item_timeout"] == 600
    assert limits.resolve({"media_item_timeout": 14400})[0]["media_item_timeout"] == 14400
    assert limits.resolve({"media_item_timeout": 0.5})[0]["media_item_timeout"] == 0.5  # a timeout may be fractional
    with pytest.raises(ContractError) as info:
        limits.resolve({"media_item_timeout": 14401})
    assert (info.value.path, info.value.why) == ("$.limits.media_item_timeout", "out_of_range")
    assert "media_item_timeout" in limits.TIMEOUT_KEYS
