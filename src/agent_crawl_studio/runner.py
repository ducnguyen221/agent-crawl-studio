"""Planner (Slice A): `plan` reports what `run` would do. No socket is opened (names are never resolved),
nothing is created or written, and the output place is decided only by `outroot.resolve_output`.

`deps` is a dict that only code (tests) fills: `policy` (urlpolicy.Policy) and the outroot system-call
parameters `attrs_of`, `drive_type_of`, `sync_root_of`, `free_bytes_of`, `realpath`. No option, environment
variable, config key or request field reaches it (SPEC FR2.8)."""

import os

from . import config as config_mod
from . import limits, outroot, schema, urlpolicy
from .capabilities import WorkRootError
from .errors import error_object, item_error
from .winpath import PathBlocked

ADAPTER_DIRECT = "direct-http"
UNSUPPORTED_MODES = ("media", "gallery", "crawl")
OUTROOT_PARAMS = ("attrs_of", "drive_type_of", "sync_root_of", "free_bytes_of", "realpath")


def _item(index, spec, mode, policy):
    base = {
        "id": "%04d" % index,
        "url_redacted": urlpolicy.redact(spec.url),
        "url_sha256": urlpolicy.url_sha256(spec.url, policy),
        "expect": spec.expect,
    }
    try:
        urlpolicy.check_url(spec.url, policy)
    except urlpolicy.Blocked:
        return dict(base, adapter=None, decision="blocked", error=item_error("URL_BLOCKED"))
    if mode in UNSUPPORTED_MODES:
        return dict(base, adapter=None, decision="blocked", error=item_error("UNSUPPORTED_SOURCE"))
    return dict(base, adapter=ADAPTER_DIRECT, decision="allow")


def _blocked(error):
    return {"decision": "blocked", "root": None, "parent": None, "error": error}


def plan(request, env, deps=None):
    deps = deps or {}
    policy = deps.get("policy") or urlpolicy.Policy()
    items = [_item(n, spec, request.mode, policy) for n, spec in enumerate(request.urls, 1)]
    warnings = list(request.warnings)
    error = None
    output = None
    try:
        cfg = config_mod.load_config(env)
        root = outroot.resolve_output(
            request.output_dir, cfg, env,
            required_free=limits.required_free_bytes(request.limits_effective, len(items)),
            **{key: deps[key] for key in OUTROOT_PARAMS if key in deps})
        output = {"decision": "allow", "root": root.root, "parent": root.parent,
                  "job_dir_template": os.path.join(root.parent, "<job_id>")}
        extra = root.warnings
    except WorkRootError:
        error = error_object("WORK_ROOT_UNKNOWN")
        extra = []
    except PathBlocked as exc:
        error = error_object("OUTPUT_PATH_BLOCKED", hint=exc.reason)
        extra = []
    except outroot.WriteBlocked as exc:
        error = error_object("OUTPUT_WRITE_FAILED", hint="missing_gib=%d" % exc.missing_gib)
        extra = []
    warnings += [warning for warning in extra if warning not in warnings]
    result = {
        "schema_version": schema.SCHEMA_VERSION, "kind": "plan", "valid": error is None, "network_checked": False,
        "output": output if error is None else _blocked(error),
        "limits_effective": request.limits_effective, "items": items, "warnings": warnings,
    }
    if error is not None:
        result["error"] = error
    return result
