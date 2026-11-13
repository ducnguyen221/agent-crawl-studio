"""Request v1: parse, validate and normalise with the standard library only (no jsonschema).

Every failure is a ContractError whose `path` names an object of OUR contract and whose `why` is one of
unknown_key / wrong_type / out_of_range: no user key name or value is ever copied into it."""

import json
import secrets
from collections import namedtuple
from dataclasses import dataclass, field

from . import limits as limits_mod
from .errors import JOB_EXIT, ContractError

SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
REQUEST_KEYS = ("schema_version", "urls", "mode", "output_dir", "limits", "naming_policy", "existing_file_policy")
URL_ITEM_KEYS = ("url", "expect")
MODES = ("auto", "direct", "media", "gallery", "crawl")
EXPECTS = ("auto", "binary", "html")
NAMING_POLICIES = ("id_hash",)
EXISTING_FILE_POLICIES = ("rename",)

UrlSpec = namedtuple("UrlSpec", ["url", "expect"])


@dataclass
class Request:
    schema_version: int
    urls: list
    mode: str
    output_dir: object
    naming_policy: str
    existing_file_policy: str
    limits_effective: dict
    warnings: list = field(default_factory=list)


def _bad(path, why):
    return ContractError("INVALID_REQUEST", path, why)


def _reject_constant(_name):
    raise ValueError("non-finite number")


def _pairs(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen[key] = value
    return seen


def _load(raw):
    if not isinstance(raw, (bytes, bytearray)):
        raise _bad("$", "wrong_type")
    if len(raw) > MAX_REQUEST_BYTES:
        raise _bad("$", "out_of_range")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise _bad("$", "wrong_type") from None
    try:
        return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_pairs)
    except ValueError as exc:
        # a duplicate key is a key the contract does not allow twice; everything else is "not JSON"
        raise _bad("$", "unknown_key" if str(exc) == "duplicate key" else "wrong_type") from None
    except RecursionError:
        raise _bad("$", "wrong_type") from None


def _choice(value, allowed, path):
    if not isinstance(value, str):
        raise _bad(path, "wrong_type")
    if value not in allowed:
        raise _bad(path, "out_of_range")
    return value


def _url_item(item, index):
    path = "$.urls[%d]" % index
    if isinstance(item, str):
        return UrlSpec(item, "auto")
    if not isinstance(item, dict):
        raise _bad(path, "wrong_type")
    for key in item:
        if key not in URL_ITEM_KEYS:
            raise _bad(path, "unknown_key")
    if not isinstance(item.get("url"), str):
        raise _bad(path, "wrong_type")
    expect = item.get("expect", "auto")
    if not isinstance(expect, str):
        raise _bad(path, "wrong_type")
    if expect not in EXPECTS:
        raise _bad(path, "out_of_range")
    return UrlSpec(item["url"], expect)


def parse_request(raw):
    """bytes -> Request. Raises ContractError for anything outside the v1 contract."""
    doc = _load(raw)
    if not isinstance(doc, dict):
        raise _bad("$", "wrong_type")
    for key in doc:
        if key not in REQUEST_KEYS:
            raise _bad("$", "unknown_key")
    for key in ("schema_version", "urls"):
        if key not in doc:
            raise _bad("$", "wrong_type")
    version = doc["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise _bad("$.schema_version", "wrong_type")
    if version != SCHEMA_VERSION:
        raise _bad("$.schema_version", "out_of_range")

    if "limits" in doc and doc["limits"] is None:  # absent is fine, an explicit null is not
        raise _bad("$.limits", "wrong_type")
    effective, warnings = limits_mod.resolve(doc.get("limits"))

    urls = doc["urls"]
    if not isinstance(urls, list):
        raise _bad("$.urls", "wrong_type")
    if not 1 <= len(urls) <= effective["max_items"]:
        raise _bad("$.urls", "out_of_range")
    specs = [_url_item(item, index) for index, item in enumerate(urls)]

    mode = _choice(doc.get("mode", "auto"), MODES, "$.mode")
    naming = _choice(doc.get("naming_policy", "id_hash"), NAMING_POLICIES, "$.naming_policy")
    existing = _choice(doc.get("existing_file_policy", "rename"), EXISTING_FILE_POLICIES, "$.existing_file_policy")

    output_dir = doc.get("output_dir")
    if "output_dir" in doc:
        if not isinstance(output_dir, str):
            raise _bad("$.output_dir", "wrong_type")
        if not output_dir:
            raise _bad("$.output_dir", "out_of_range")
    return Request(version, specs, mode, output_dir, naming, existing, effective, warnings)


def job_status(item_statuses, cancelled=False):
    """FR1.7: succeeded / partial / failed / cancelled from the item statuses."""
    if cancelled:
        return "cancelled"
    statuses = list(item_statuses)
    done = [s for s in statuses if s == "succeeded"]
    if statuses and all(s in ("succeeded", "skipped") for s in statuses):
        return "succeeded"
    if done and len(done) < len(statuses):
        return "partial"
    return "failed"


def new_job_id(now, token=None):
    """YYYYMMDD-HHMMSS-<6 lowercase hex>."""
    return now.strftime("%Y%m%d-%H%M%S") + "-" + (token if token is not None else secrets.token_hex(3))


PLAN_KEYS = ("schema_version", "kind", "valid", "network_checked", "output", "limits_effective", "items", "warnings")
PLAN_ITEM_KEYS = ("id", "url_redacted", "url_sha256", "adapter", "decision")


def validate_plan(obj):
    """Problems found in a plan object (an empty list when it has the documented shape)."""
    if not isinstance(obj, dict):
        return ["$"]
    problems = [key for key in PLAN_KEYS if key not in obj]
    if isinstance(obj.get("items"), list):
        for index, item in enumerate(obj["items"]):
            if not isinstance(item, dict):
                problems.append("items[%d]" % index)
                continue
            problems += ["items[%d].%s" % (index, key) for key in PLAN_ITEM_KEYS if key not in item]
    return problems


RESULT_KEYS = ("schema_version", "kind", "job_id", "status", "started_at", "finished_at", "counts", "limits_effective",
               "items", "warnings")
RESULT_ITEM_KEYS = ("id", "url_redacted", "url_sha256", "adapter", "status", "artifacts", "untrusted")


def validate_result(obj):
    """Problems found in a result / manifest object (an empty list when it has the documented shape)."""
    if not isinstance(obj, dict):
        return ["$"]
    problems = [key for key in RESULT_KEYS if key not in obj]
    if obj.get("status") is not None and not (isinstance(obj["status"], str) and obj["status"] in JOB_EXIT):
        problems.append("status")
    if isinstance(obj.get("items"), list):
        for index, item in enumerate(obj["items"]):
            if not isinstance(item, dict):
                problems.append("items[%d]" % index)
                continue
            problems += ["items[%d].%s" % (index, key) for key in RESULT_ITEM_KEYS if key not in item]
        counts = obj.get("counts")
        if (obj.get("status") is not None and obj.get("job_id") is not None and isinstance(counts, dict)
                and "total" in counts and len(obj["items"]) != counts["total"]):
            problems.append("counts.total")  # a finished job lists every item it was given
    return problems
