"""`capabilities`: report what the tool can do on this machine. Never installs anything."""

import os
import re
import shutil
import subprocess
from importlib import metadata

from . import envbuild
from .errors import error_object

SCHEMA_VERSION = 1
ENGINES = ("yt-dlp", "httpx")
_FFMPEG_VERSION = re.compile(r"ffmpeg version (\d+(?:\.\d+)*)")
_NODE_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)")

HINT_ENGINE = "run: opcos-runtime ensure --repo-dir plugins/agent-crawl-studio"
HINT_FFMPEG = "install ffmpeg or set FFMPEG_DIR to its bin folder"
HINT_NODE = "install Node.js and put node on PATH"


TIMEOUT_RC = -9  # system_run result for "the command ran out of time and was killed"
PROBE_TIMEOUT = 10  # seconds; short, for `ffmpeg -version`, `node --version` and `ffprobe`
DEFAULT_TIMEOUT = 30


def system_run(argv, env=None, timeout=DEFAULT_TIMEOUT, cwd=None):
    """Run a command; return (returncode, stdout, stderr). Never raises for a missing binary.

    A missing or unstartable binary gives (127, "", ""); running out of `timeout` seconds gives
    (TIMEOUT_RC, "", "") so a caller can tell the two apart."""
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, env=env, timeout=timeout, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return (TIMEOUT_RC, "", "")
    except (OSError, subprocess.SubprocessError):
        return (127, "", "")
    return (done.returncode, done.stdout or "", done.stderr or "")


def default_version_of(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        raise LookupError(name)


def _probe(path, args, pattern, run, env):
    try:
        rc, out, _ = run([path] + args, env=env, timeout=PROBE_TIMEOUT)
    except OSError:
        return None
    if rc != 0:
        return None
    match = pattern.search(out.strip())
    return match.group(1) if match else None


def _absolute(path):
    """A tool path from `which` is trusted only when absolute (never a cwd-relative hit)."""
    return path if path and os.path.isabs(path) else None


def locate_ffmpeg(env, which):
    """FFMPEG_DIR first, then the explicit PATH of `env`."""
    ffmpeg_dir = env.get("FFMPEG_DIR")
    found = _absolute(which("ffmpeg", path=ffmpeg_dir)) if ffmpeg_dir else None
    return found or _absolute(which("ffmpeg", path=env.get("PATH")))


def locate_ffprobe(ffmpeg_path, which):
    """ffprobe must sit next to the ffmpeg we located; absolute paths only."""
    return _absolute(which("ffprobe", path=os.path.dirname(ffmpeg_path)))


def locate_node(which, env=None):
    return _absolute(which("node", path=(env or {}).get("PATH")))


def _tool(path, version):
    if path and version:
        return {"path": path, "version": version, "status": "ok"}
    return {"path": path, "version": None, "status": "missing"}


class WorkRootError(ValueError):
    """The work root cannot be made into an absolute path from the environment."""


def work_root(env):
    explicit = env.get("CRAWL_STUDIO_WORK")
    if explicit:
        root = explicit
    else:
        base = env.get("OPCOS_ROOT")
        if not base and env.get("USERPROFILE"):
            base = os.path.join(env["USERPROFILE"], "OpcOS")
        if not base:
            raise WorkRootError("no CRAWL_STUDIO_WORK, OPCOS_ROOT or USERPROFILE")
        root = os.path.join(base, "workspaces", "agent-crawl-studio")
    if not os.path.isabs(root):
        raise WorkRootError("work root is not absolute")
    return root


def build_capabilities(env, which, run, version_of=default_version_of):
    engines = {}
    hints = []
    for name in ENGINES:
        try:
            engines[name] = {"version": version_of(name), "status": "ok"}
        except LookupError:
            engines[name] = {"version": None, "status": "missing"}
    if any(e["status"] == "missing" for e in engines.values()):
        hints.append(HINT_ENGINE)

    probe_env = envbuild.build_subprocess_env(env, env.get("TEMP") or env.get("SystemRoot") or r"C:\Windows")
    ffmpeg_path = locate_ffmpeg(env, which)
    ffmpeg = _tool(ffmpeg_path, _probe(ffmpeg_path, ["-version"], _FFMPEG_VERSION, run, probe_env) if ffmpeg_path else None)
    if ffmpeg["status"] == "missing":
        hints.append(HINT_FFMPEG)

    node_path = locate_node(which, env)
    node = _tool(node_path, _probe(node_path, ["--version"], _NODE_VERSION, run, probe_env) if node_path else None)
    if node["status"] == "missing":
        hints.append(HINT_NODE)

    try:
        root = work_root(env)
    except WorkRootError:
        root = None
        if not hints:
            hints = None

    cap = {
        "schema_version": SCHEMA_VERSION,
        "engines": engines,
        "ffmpeg": ffmpeg,
        "node": node,
        "work_root": root,
    }
    if hints:
        cap["error"] = error_object("DEPENDENCY_MISSING", hint="; ".join(hints))
    elif hints is None:
        cap["error"] = error_object("WORK_ROOT_UNKNOWN")
    return cap
