"""Default limits and hard caps (SPEC section 9) and their validation."""

import math

from .errors import ContractError

GiB = 2 ** 30
MiB = 2 ** 20

DEFAULTS = {
    "max_items": 100,
    "max_bytes_per_file": 2 * GiB,
    "max_total_bytes": 10 * GiB,
    "max_artifacts_per_item": 10,
    "concurrency": 3,
    "per_host_concurrency": 2,
    "connect_timeout": 15,
    "idle_timeout": 60,
    "item_timeout": 600,
    "media_item_timeout": 2700,  # yt-dlp items: a long video needs longer than a file
    "job_timeout": 4 * 3600,
    "retries": 3,
    "max_redirects": 5,
    "max_html_bytes": 10 * MiB,
}
HARD_CAPS = {
    "max_items": 1000,
    "max_bytes_per_file": 20 * GiB,
    "max_total_bytes": 100 * GiB,
    "max_artifacts_per_item": 50,
    "concurrency": 8,
    "per_host_concurrency": 4,
    "connect_timeout": 60,
    "idle_timeout": 300,
    "item_timeout": 4 * 3600,
    "media_item_timeout": 4 * 3600,
    "job_timeout": 24 * 3600,
    "retries": 5,
    "max_redirects": 10,
    "max_html_bytes": 50 * MiB,
}
TIMEOUT_KEYS = ("connect_timeout", "idle_timeout", "item_timeout", "media_item_timeout", "job_timeout")  # seconds, may be fractional
ZERO_OK = ("retries",)  # `retries: 0` is valid: every item runs once (FR14.1)
FREE_SPACE_FLOOR = 5 * GiB


def _number(value, key):
    path = "$.limits." + key
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("INVALID_REQUEST", path, "wrong_type")
    if key not in TIMEOUT_KEYS and not isinstance(value, int):
        raise ContractError("INVALID_REQUEST", path, "wrong_type")
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError("INVALID_REQUEST", path, "out_of_range")
    if value < 0 or (value == 0 and key not in ZERO_OK) or value > HARD_CAPS[key]:
        raise ContractError("INVALID_REQUEST", path, "out_of_range")
    return value


def resolve(limits_in):
    """Return (effective limits, warnings). `concurrency` is accepted but this release runs one item at a time,
    so its warning is always present; `retries` works (FR14)."""
    if limits_in is None:
        limits_in = {}
    if not isinstance(limits_in, dict):
        raise ContractError("INVALID_REQUEST", "$.limits", "wrong_type")
    for key in limits_in:
        if key not in DEFAULTS:
            raise ContractError("INVALID_REQUEST", "$.limits", "unknown_key")
    effective = dict(DEFAULTS)
    for key, value in limits_in.items():
        effective[key] = _number(value, key)
    return effective, ["concurrency_not_applied"]


def required_free_bytes(effective, item_count):
    """FR3.7: 5 GiB + min(max_total_bytes, max_bytes_per_file x items)."""
    return FREE_SPACE_FLOOR + min(effective["max_total_bytes"], effective["max_bytes_per_file"] * item_count)
