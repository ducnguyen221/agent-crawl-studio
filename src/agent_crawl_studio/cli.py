"""Command line: stdout is always exactly one JSON line."""

import json
import os
import shutil
import sys

from . import capabilities as cap
from .errors import error_object


def _emit(out, obj):
    out.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def main(argv, out=None, env=None, which=None, run=None, version_of=None, build=None):
    out = out if out is not None else sys.stdout
    if list(argv) != ["capabilities"]:
        _emit(out, {"schema_version": cap.SCHEMA_VERSION, "error": error_object("USAGE_ERROR", hint="usage: crawl-studio capabilities")})
        return 2
    try:
        result = (build or cap.build_capabilities)(
            env if env is not None else dict(os.environ),
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
