"""The yt-dlp adapter (SPEC FR8.5, FR9.1-FR9.6, FR10.1, FR11, FR12): the per-item folders, tool lookup, the command,
the closed failure table, untrusted metadata, log redaction, and `run_media_item`, which runs one engine child in
a Job Object, watches its folder and ends the whole tree on every way out. Nothing here opens a socket or imports
`yt_dlp` (the package is only ever run as `python -m yt_dlp` in a child)."""

import json
import math
import os
import re
import stat
import sys
import time
from collections import namedtuple

from . import capabilities, envbuild, errors, flags, jobobject

MAX_METADATA = 4 * 2 ** 20
MAX_LOG = 256 * 2 ** 10
ITEM_ID = re.compile(r"[0-9]{4}")
ITEM_SUBDIRS = ("dl", "home", "cache", "io")

ItemDirs = namedtuple("ItemDirs", "root dl home cache io")
Tools = namedtuple("Tools", "ffmpeg_dir node version")
MediaOutcome = namedtuple("MediaOutcome", "status code reason retryable file_path untrusted declared_size warnings")

MEDIA_NAME = re.compile(r"media\.[a-z0-9]{1,5}")  # the whitelist of FR10.1; intermediates have two dots
REPARSE = 0x400
POLL = 0.5

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


class OutputError(Exception):
    """`dl/` does not hold exactly one acceptable media file; `reason` is `none`, `many` or `not_plain`."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _regular(path):
    """True for a plain file with one link and no reparse point (os.lstat: DirEntry.stat leaves st_nlink at 0)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and not getattr(info, "st_file_attributes", 0) & REPARSE)


def pick_output(dl):
    """The path of the one whitelisted file in `dl/`, or OutputError. Never follows a reparse point; a matching
    name that is a directory, a link or a hard link is `not_plain`, never a file to take."""
    good, bad = [], 0
    try:
        with os.scandir(dl) as entries:
            for entry in entries:
                if not MEDIA_NAME.fullmatch(entry.name):
                    continue
                if _regular(entry.path):
                    good.append(entry.path)
                else:
                    bad += 1
    except OSError:
        raise OutputError("none") from None
    if bad:
        raise OutputError("not_plain")
    if not good:
        raise OutputError("none")
    if len(good) > 1:
        raise OutputError("many")
    return good[0]


def scan_sizes(dl):
    """Sizes of the plain files directly in `dl/` (no reparse point followed; a file that vanishes is skipped).
    `os.lstat`, not `DirEntry.stat`: the size in a directory listing is stale while the engine still holds the
    file open for writing, which is exactly when the watch must see it grow."""
    sizes = []
    try:
        with os.scandir(dl) as entries:
            for entry in entries:
                try:
                    info = os.lstat(entry.path)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & REPARSE:
                    sizes.append(info.st_size)
    except OSError:
        return []
    return sizes


def _size_verdict(sizes, cap, budget, room):
    """FR12.4: the error code that ends the run, or None."""
    if not sizes:
        return None
    largest, total = max(sizes), sum(sizes)
    if largest > cap or total > 2 * cap or largest > budget:
        return "SIZE_LIMIT"
    if total > room:
        return "OUTPUT_WRITE_FAILED"
    return None


def _failed(code, reason=None, retryable=False, warnings=()):
    return MediaOutcome(errors.ITEM_STATUS_FOR.get(code, "failed"), code, reason, retryable, None, {}, None,
                        list(warnings))


def _tail(path, size):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - size))
            return handle.read(size)
    except OSError:
        return b""


def _head(path, size):
    try:
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def _watch(proc, dirs, limits, budget, room, clock, deps):
    """The 0.5 s loop (FR12.4): returns the code that ended the run, or None when the engine exited by itself."""
    scan = deps.get("scan", scan_sizes)
    sleep = deps.get("sleep", time.sleep)
    poll = deps.get("poll", POLL)
    deadline = clock() + limits["media_item_timeout"]
    while True:
        exited = proc.poll() is not None
        verdict = _size_verdict(scan(dirs.dl), limits["max_bytes_per_file"], budget, room)
        if verdict or exited:
            return verdict
        if clock() >= deadline:
            return "TIMEOUT"
        sleep(poll)


def _finish(proc, dirs, limits, warnings):
    """After the tree is dead: the outcome of an engine that ended by itself."""
    if proc.returncode != 0:
        text = _tail(os.path.join(dirs.io, "stderr.bin"), MAX_LOG).decode("utf-8", "replace")
        code, reason, retryable = classify_failure(text)
        return _failed(code, reason, retryable, warnings)
    untrusted, declared, meta_warnings = parse_metadata(_head(os.path.join(dirs.io, "stdout.bin"), MAX_METADATA + 1))
    try:
        path = pick_output(dirs.dl)
    except OutputError as exc:
        if exc.reason == "none" and declared is not None and declared > limits["max_bytes_per_file"]:
            return _failed("SIZE_LIMIT", warnings=warnings + meta_warnings)  # exit 0, no file, declared too large
        return _failed("TOOL_FAILED", warnings=warnings + meta_warnings)
    return MediaOutcome("succeeded", None, None, False, path, untrusted, declared, warnings + meta_warnings)


def _run_engine(argv, env, dirs, limits, budget, room, clock, deps, streams, holder):
    kill = deps.get("kill_tree", jobobject.kill_tree)
    spawn = deps.get("spawn", jobobject.spawn_in_job)
    try:
        proc, job = spawn(argv, env, dirs.home, streams[0], streams[1], holder=holder)
    except jobobject.JobObjectError:
        return _failed("TOOL_FAILED", "job_object_unavailable")  # the process (if any) was ended by the spawner
    except OSError:
        return _failed("TOOL_FAILED")
    verdict = _watch(proc, dirs, limits, budget, room, clock, deps)
    clean = kill(job, proc)  # before anything is read or picked: the tree is dead first (FR10.1)
    warnings = [] if clean else ["process_tree_not_empty"]
    if verdict:
        return _failed(verdict, warnings=warnings)  # a deadline or a size stop is never worth a retry
    return _finish(proc, dirs, limits, warnings)


def run_media_item(url, item_id, dirs, limits, tools, *, budget, room, clock, deps):
    """Run the engine for one item and return a MediaOutcome (FR9.6, FR10.1, FR12). `budget` is what is left of
    `max_total_bytes`, `room` what the disk lets the folder use; `deps` injects `engine_prefix`, `spawn`,
    `kill_tree`, `scan`, `sleep`, `poll`, `environ`, `proxy_url`. The engine's stdout and stderr go to files in
    `dirs.io` (the runner redacts them). On any way out - including Ctrl+C - the tree is ended and empty before
    this returns or re-raises."""
    argv = build_command(deps.get("engine_prefix", engine_prefix)(), url, tools, dirs, limits,
                         deps.get("proxy_url", ""))
    base = deps["environ"] if "environ" in deps else envbuild.process_env()
    env = envbuild.build_subprocess_env(base, dirs.home, node_dir=os.path.dirname(tools.node))
    kill = deps.get("kill_tree", jobobject.kill_tree)
    holder, streams = [], []
    try:
        for name in ("stdout.bin", "stderr.bin"):
            streams.append(open(os.path.join(dirs.io, name), "wb"))
        return _run_engine(argv, env, dirs, limits, budget, room, clock, deps, streams, holder)
    except BaseException:
        for proc, job in holder:  # an interrupt or a bug: the tree must not outlive this call
            kill(job, proc)
        raise
    finally:
        for proc, job in holder:
            job.close()
        for handle in streams:
            handle.close()
