"""Smoke: download one approved video through the core (`runner.run`, mode `media`) and report one verdict.

Usage: python scripts/smoke_download.py --url <url> --work <dir> [--plan-only]
Run it with the Python of the venv `core` (the launcher's interpreter). stdout is ONE JSON line:
{"status": "ok" | "BLOCKED_ENV" | "FAIL" | "NOT_RUN", ...}; exit 0 only for "ok" (130 when interrupted).

This script is a thin layer (SPEC FR17.1): the URL rules, the host list, the DNS probe, the engine, the folders and
the cleanup are the core's. It only builds a one-URL request, calls `runner.run` (or `runner.plan` with
`--plan-only`) and maps the answer: a succeeded job is "ok"; a missing tool or a name that does not resolve is
"BLOCKED_ENV"; any other end is "FAIL". Nothing of the engine's text is ever printed.
"""

import argparse
import json
import os
import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from agent_crawl_studio import runner, schema  # noqa: E402
from agent_crawl_studio.errors import error_object  # noqa: E402


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _emit(out, obj):
    out.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def _result(out, status, code=1, **extra):
    obj = {"status": status}
    obj.update(extra)
    _emit(out, obj)
    return code


def _verdict(result):
    """(status, exit code, extra fields) of a finished job."""
    item = result["items"][0] if result.get("items") else None
    if result.get("status") == "succeeded" and item and item["artifacts"]:
        artifact = item["artifacts"][0]
        return "ok", 0, {"job_id": result["job_id"], "file": artifact["path"], "bytes": artifact["bytes"],
                         "sha256": artifact["sha256"]}
    error = (item or {}).get("error") or result.get("error") or {}
    extra = {"job_id": result.get("job_id"), "error": {"code": error.get("code")}}
    if error.get("code") == "DEPENDENCY_MISSING" or error.get("reason") == "dns_failed":
        return "BLOCKED_ENV", 1, extra
    return "FAIL", 1, extra


def main(argv, out=None, env=None, deps=None):
    out = out if out is not None else sys.stdout
    try:
        return _main(argv, out, env if env is not None else dict(os.environ), deps)
    except KeyboardInterrupt:
        return _result(out, "FAIL", 130, error=error_object("INTERRUPTED"))


def _main(argv, out, env, deps):
    parser = _Parser(prog="smoke_download.py", add_help=False)
    parser.add_argument("--url", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--plan-only", action="store_true")
    try:
        args = parser.parse_args(argv)
    except ValueError:
        _emit(out, {"status": "FAIL", "error": error_object("USAGE_ERROR")})
        return 2
    doc = {"schema_version": schema.SCHEMA_VERSION, "urls": [args.url], "mode": "media", "output_dir": args.work}
    try:
        request = schema.parse_request(json.dumps(doc).encode("utf-8"))
    except Exception:  # noqa: BLE001 - a request the core refuses; its text is never echoed
        return _result(out, "FAIL", error=error_object("INVALID_REQUEST"))
    if args.plan_only:
        planned = runner.plan(request, env, deps)
        return _result(out, "NOT_RUN", valid=planned["valid"],
                       decision=planned["items"][0]["decision"] if planned["items"] else None)
    result, _code = runner.run(request, env, deps)
    if _code == 130:  # the core ended the job as `cancelled`
        return _result(out, "FAIL", 130, error=error_object("INTERRUPTED"), job_id=result.get("job_id"))
    status, code, extra = _verdict(result)
    return _result(out, status, code, **extra)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
