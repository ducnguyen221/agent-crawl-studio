"""Subprocess environment rebuilt from an allow-list (never inherited)."""

import os

ALLOWED_KEYS = ("PATH", "SystemRoot", "windir", "PATHEXT", "USERPROFILE", "TEMP", "TMP", "PYTHONUTF8")
_DEFAULT_ROOT = r"C:\Windows"


def process_env():
    """A copy of this process's environment, the `base` of `build_subprocess_env` when none is injected."""
    return dict(os.environ)


def build_subprocess_env(base, job_tmp, node_dir=None):
    root = base.get("SystemRoot") or _DEFAULT_ROOT
    path = [os.path.join(root, "System32"), root]
    if node_dir:
        path.insert(0, node_dir)
    env = {
        "PATH": os.pathsep.join(path),
        "SystemRoot": root,
        "windir": root,
        "USERPROFILE": job_tmp,
        "TEMP": job_tmp,
        "TMP": job_tmp,
        "PYTHONUTF8": "1",
    }
    if base.get("PATHEXT"):
        env["PATHEXT"] = base["PATHEXT"]
    return {key: env[key] for key in ALLOWED_KEYS if key in env}
