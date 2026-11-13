"""The yt-dlp adapter, pure parts (SPEC FR8.5, FR9.1-FR9.4, FR11): the per-item folders, tool lookup, the command,
the closed failure table, untrusted metadata and log redaction. Nothing here starts a process, opens a socket or
imports `yt_dlp` (the package is only ever run as `python -m yt_dlp` in a child)."""

import json
import math
import os
import re
import sys
from collections import namedtuple

from . import capabilities, flags

MAX_METADATA = 4 * 2 ** 20
MAX_LOG = 256 * 2 ** 10
ITEM_ID = re.compile(r"[0-9]{4}")
ITEM_SUBDIRS = ("dl", "home", "cache", "io")

ItemDirs = namedtuple("ItemDirs", "root dl home cache io")
Tools = namedtuple("Tools", "ffmpeg_dir node version")

# (marker, code, reason, retryable): the order is the priority; a marker is looked for only in the stderr lines
# that start with `ERROR:` (lower-cased), so a title that contains one cannot decide the code. There is no
# "larger than max-filesize" row on purpose: `--print` makes yt-dlp quiet and the line never appears (TK1 spike),
# so SIZE_LIMIT comes from the core's own size watch (FR12.4, FR12.5), not from the engine's words.
MARKERS = (
    ("sign in to confirm", "AUTH_REQUIRED", None, False),
    ("private video", "AUTH_REQUIRED", None, False),
    ("members-only", "AUTH_REQUIRED", None, False),
    ("http error 429", "RATE_LIMITED", None, True),
    ("http error 403", "NETWORK_ERROR", "forbidden", False),
    ("getaddrinfo failed", "NETWORK_ERROR", "dns_failed", True),
    ("failed to resolve", "NETWORK_ERROR", "dns_failed", True),
    ("unsupported url", "UNSUPPORTED_SOURCE", None, False),
    ("timed out", "TIMEOUT", None, True),
)
FALLBACK = ("TOOL_FAILED", None, False)

_URL = re.compile(r"(https?)://(?:[^/\s?#@]*@)?([^/\s?#]*)[^\s]*")


def make_item_dirs(job_dir, item_id, check):
    """Create `<job>/.tmp/<id>/{dl,home,cache,io}`; `check(path)` (raises when a path is a reparse point or
    otherwise not acceptable) sees every folder right after it is made."""
    if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id):
        raise ValueError("item id must be four digits")
    tmp = os.path.join(job_dir, ".tmp")
    root = os.path.join(tmp, item_id)
    os.makedirs(tmp, exist_ok=True)
    os.mkdir(root)
    if check is not None:
        check(root)
    made = []
    for name in ITEM_SUBDIRS:
        path = os.path.join(root, name)
        os.mkdir(path)
        if check is not None:
            check(path)
        made.append(path)
    return ItemDirs(root, *made)


def find_tools(env, which, version_of):
    """Tools(ffmpeg_dir, node, yt-dlp version) or None when any of the three is missing (FR8.5). The package is
    found by its metadata, never imported."""
    ffmpeg = capabilities.locate_ffmpeg(env, which)
    node = capabilities.locate_node(which, env)
    if not ffmpeg or not node:
        return None
    try:
        version = version_of("yt-dlp")
    except LookupError:
        return None
    if not version:
        return None
    return Tools(os.path.dirname(ffmpeg), node, version)


def engine_prefix():
    """`-P`: no cwd or PYTHONPATH entry on sys.path; `-X utf8`: engine text is UTF-8 whatever the console is."""
    return [sys.executable, "-P", "-X", "utf8", "-m", "yt_dlp"]


def build_command(prefix, url, tools, dirs, limits, proxy_url=""):
    """argv = prefix + the fixed flags (validated here, before any process) + `--` + the normalised URL, which
    can therefore never be read as a flag."""
    timeout = max(1, math.ceil(limits["idle_timeout"]))
    fixed = flags.fixed_ytdlp_flags(tools.ffmpeg_dir, dirs.root, str(limits["max_bytes_per_file"]), timeout,
                                    tools.node, proxy_url)
    flags.validate_flags(fixed)
    return list(prefix) + fixed + ["--", url]


def classify_failure(stderr_text):
    """(code, reason, retryable) of a failed run, from the closed table; never a piece of the engine's text."""
    lines = [line.lower() for line in (stderr_text or "").splitlines() if line.startswith("ERROR:")]
    for marker, code, reason, retryable in MARKERS:
        if any(marker in line for line in lines):
            return (code, reason, retryable)
    return FALLBACK


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


def parse_metadata(stdout):
    """(untrusted dict, declared size or None, warnings) from the engine's stdout, which must be exactly one JSON
    object of at most 4 MiB. Only title, uploader, duration and extractor are kept, cleaned like any source text;
    the declared size (the larger of filesize and filesize_approx) is for the core's size rule and is not stored."""
    unreadable = ({}, None, ["metadata_unreadable"])
    if not isinstance(stdout, (bytes, bytearray)) or len(stdout) > MAX_METADATA:
        return unreadable
    try:
        doc = json.loads(bytes(stdout).decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return unreadable
    if not isinstance(doc, dict):
        return unreadable
    raw = {}
    for name, key in (("title", "title"), ("uploader", "uploader"), ("extractor", "extractor_key")):
        if isinstance(doc.get(key), str):
            raw[name] = doc[key]
    duration = _number(doc.get("duration"))
    if duration is not None:
        raw["duration"] = str(duration)
    from . import manifest  # late: `artifacts` will import this module and `manifest` imports `artifacts`
    sizes = [int(v) for v in (_number(doc.get("filesize")), _number(doc.get("filesize_approx"))) if v is not None]
    return manifest.clean_untrusted(raw), (max(sizes) if sizes else None), []


def redact_log(text, secrets=()):
    """Engine stderr made safe to keep: listed secrets (the proxy token) become REDACTED, every URL becomes
    `scheme://host/REDACTED` (no userinfo, path or query: signed links live there), and only the last 256 KiB stay."""
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "REDACTED")
    text = _URL.sub(lambda found: "%s://%s/REDACTED" % (found.group(1), found.group(2)), text)
    encoded = text.encode("utf-8", "replace")
    if len(encoded) > MAX_LOG:
        text = encoded[-MAX_LOG:].decode("utf-8", "ignore")
    return text
