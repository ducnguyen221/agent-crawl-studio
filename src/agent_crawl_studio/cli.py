"""Command line: stdout is always exactly one JSON line; logs and library noise never reach it."""

import contextlib
import json
import logging
import os
import shutil
import sys
import warnings

from . import capabilities as cap
from . import schema
from .errors import EXIT_FOR, ContractError, error_object

# The whole public surface. No option here can weaken a policy (SPEC FR2.8).
COMMANDS = {"capabilities": (), "plan": ("--request",), "run": ("--request",)}
USAGE = "usage: crawl-studio capabilities | plan --request <file> | run --request <file>"


def _emit(out, obj):
    out.write(json.dumps(obj, ensure_ascii=True, separators=(",", ":")) + "\n")


def _usage(out, hint=USAGE):
    _emit(out, {"schema_version": cap.SCHEMA_VERSION, "error": error_object("USAGE_ERROR", hint=hint)})
    return 2


def _read_request(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(schema.MAX_REQUEST_BYTES + 1)
    except OSError:
        raise ContractError("INVALID_REQUEST", "$", "out_of_range") from None


def _plan(out, env, args, plan_fn):
    try:
        request = schema.parse_request(_read_request(args[2]))
    except ContractError as exc:
        _emit(out, {"schema_version": schema.SCHEMA_VERSION, "kind": "plan", "valid": False,
                    "error": error_object("INVALID_REQUEST", hint=exc.hint)})
        return 2
    if plan_fn is None:
        from . import runner  # imported late: the planner needs the policy modules
        plan_fn = runner.plan
    result = plan_fn(request, env)
    _emit(out, result)
    error = result.get("error")
    if not error:
        return 0
    return EXIT_FOR.get(error["code"]) or 2


def _run(out, env, args):
    try:
        schema.parse_request(_read_request(args[2]))
    except ContractError as exc:
        _emit(out, {"schema_version": schema.SCHEMA_VERSION, "kind": "result", "job_id": None, "status": "failed",
                    "error": error_object("INVALID_REQUEST", hint=exc.hint)})
        return 2
    return _usage(out, hint="run is not available yet in this build")


def _valid(argv):
    """argv must be `<command>` followed by exactly the (flag, value) pairs COMMANDS lists for it."""
    spec = COMMANDS.get(argv[0]) if argv else None
    return spec is not None and len(argv) == 1 + 2 * len(spec) and argv[1::2] == list(spec)


@contextlib.contextmanager
def _quiet():
    """Nothing but our own JSON line may reach stdout: library warnings are ignored, log records are
    disabled and anything printed meanwhile goes to stderr."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with warnings.catch_warnings(), contextlib.redirect_stdout(sys.stderr):
            warnings.simplefilter("ignore")
            yield
    finally:
        logging.disable(previous)


def main(argv, out=None, env=None, which=None, run=None, version_of=None, build=None, plan_fn=None):
    out = out if out is not None else sys.stdout
    argv = list(argv)
    if not _valid(argv):
        return _usage(out)
    env = env if env is not None else dict(os.environ)
    with _quiet():
        if argv[0] == "capabilities":
            return _capabilities(out, env, which, run, version_of, build)
        try:
            if argv[0] == "plan":
                return _plan(out, env, argv, plan_fn)
            return _run(out, env, argv)
        except KeyboardInterrupt:
            _emit(out, {"schema_version": schema.SCHEMA_VERSION, "error": error_object("INTERRUPTED")})
            return 130
        except Exception:  # noqa: BLE001 - the text of an unexpected failure is never printed
            _emit(out, {"schema_version": schema.SCHEMA_VERSION, "error": error_object("INTERNAL_ERROR")})
            return 1


def _capabilities(out, env, which, run, version_of, build):
    try:
        result = (build or cap.build_capabilities)(
            env,
            which or shutil.which,
            run or cap.system_run,
            version_of=version_of or cap.default_version_of,
        )
    except KeyboardInterrupt:
        _emit(out, {"schema_version": cap.SCHEMA_VERSION, "error": error_object("INTERRUPTED")})
        return 130
    except Exception:  # noqa: BLE001 - the text of an unexpected failure is never printed
        _emit(out, {"schema_version": cap.SCHEMA_VERSION, "error": error_object("INTERNAL_ERROR")})
        return 1
    _emit(out, result)
    return 0
