"""The `retry` command: run again, in a new job, the items of an old job that failed in a way that can pass or were
cancelled (SPEC FR15). The old manifest is a file anybody could have written, so it is read like the request is
(the same refusals for special paths), must sit under an allowed root, and has every field this command uses checked
before one folder is made or one connection is opened. The old job is never changed."""

import json
import os
import re
import stat

from . import cli, manifest, outroot, runner, schema, urlpolicy
from . import config as config_mod
from .errors import JOB_EXIT, ContractError
from .winpath import PathBlocked

MAX_MANIFEST_BYTES = 16 * 2 ** 20
FILE_NAME = "manifest.json"
ITEM_ID = re.compile(r"[0-9]{4}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def _bad(why="wrong_type"):
    return ContractError("INVALID_REQUEST", "$", why)


def _check_place(path, env, deps):
    """The manifest sits in a job folder under an allowed root: not a floor place, not a cloud tree, no junction or
    link from the root down (the folder, then the file itself). Returns the absolute path."""
    if not isinstance(path, str) or cli._special_path(path) or os.path.basename(path).lower() != FILE_NAME:
        raise _bad("out_of_range")
    absolute = os.path.abspath(path)
    params = {key: deps[key] for key in runner.OUTROOT_PARAMS if key in deps}
    cfg = config_mod.load_config(env)  # WorkRootError: the command says WORK_ROOT_UNKNOWN
    try:
        root = outroot.resolve_output(os.path.dirname(absolute), cfg, env, required_free=0, **params)
        outroot.post_create_check(absolute, root, **{key: params[key] for key in ("attrs_of", "realpath") if key in params})
    except (PathBlocked, outroot.WriteBlocked, OSError):
        raise _bad("out_of_range") from None
    return absolute


def _read(absolute):
    try:
        if not stat.S_ISREG(os.lstat(absolute).st_mode):
            raise _bad("out_of_range")
        with open(absolute, "rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
    except OSError:
        raise _bad("out_of_range") from None
    if len(raw) > MAX_MANIFEST_BYTES:
        raise _bad("out_of_range")
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=schema._reject_constant, object_pairs_hook=schema._pairs)
    except (ValueError, RecursionError):
        raise _bad("wrong_type") from None


def _is_text(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _check_shape(doc):
    """FR15.2: a finished result v1 whose ids, statuses, hashes and `retryable` flags can be trusted."""
    if not isinstance(doc, dict) or schema.validate_result(doc):
        raise _bad()
    version = doc["schema_version"]
    if isinstance(version, bool) or version != schema.SCHEMA_VERSION or doc["kind"] != "result":
        raise _bad()
    if not (isinstance(doc["status"], str) and doc["status"] in JOB_EXIT):  # `null`: the job never finished
        raise _bad()
    counts = doc["counts"]
    if not _is_text(doc["job_id"], schema.JOB_ID) or not isinstance(counts, dict) or isinstance(
            counts.get("total"), bool) or not isinstance(counts.get("total"), int):
        raise _bad()
    if not isinstance(doc["items"], list):
        raise _bad()
    seen = set()
    for item in doc["items"]:
        if not isinstance(item, dict) or not _is_text(item.get("id"), ITEM_ID) or item["id"] in seen or int(item["id"]) < 1:
            raise _bad()
        seen.add(item["id"])
        if item.get("status") not in manifest.STATUSES or not _is_text(item.get("url_sha256"), SHA256):
            raise _bad()
        error = item.get("error")
        if "error" in item and not (isinstance(error, dict) and isinstance(error.get("retryable"), bool)):
            raise _bad()
        if item["status"] == "failed" and error is None:
            raise _bad()


def load_manifest(path, env, deps):
    """The old manifest at `path`, checked (FR15.2). Raises ContractError (INVALID_REQUEST) for anything that is not
    a finished result of ours in a place we may read; makes nothing and opens no connection."""
    doc = _read(_check_place(path, env, deps or {}))
    _check_shape(doc)
    return doc


def select_items(old, request, policy):
    """The 1-based ids to run again (FR15.3, FR15.4): the `failed` items with `error.retryable: true` and the
    `cancelled` ones. Every id of the old manifest must be an item of `request` with the same URL (by hash), and an
    original manifest must have been made from a request of as many items as it counts."""
    count = len(request.urls)
    if "retry_of" not in old and old["counts"]["total"] != count:
        raise _bad("out_of_range")
    picked = []
    for item in old["items"]:
        number = int(item["id"])
        if number > count or item["url_sha256"] != urlpolicy.url_sha256(request.urls[number - 1].url, policy):
            raise _bad("out_of_range")
        if item["status"] == "cancelled" or (item["status"] == "failed" and item["error"]["retryable"] is True):
            picked.append(number)
    if not picked:
        raise ContractError("INVALID_REQUEST", "$", "nothing_to_retry")
    return sorted(picked)


def retry(manifest_path, request, env, deps=None):
    """Run the selected items again in a new job (FR15.5, FR15.6). Returns (result, exit code) like `runner.run`."""
    deps = deps or {}
    policy = deps.get("policy") or urlpolicy.Policy()
    old = load_manifest(manifest_path, env, deps)
    only = select_items(old, request, policy)
    return runner.run(request, env, deps, only=only, retry_of=old["job_id"])
