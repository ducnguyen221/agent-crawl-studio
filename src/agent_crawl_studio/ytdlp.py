"""The yt-dlp adapter (SPEC FR8.5, FR9.1-FR9.6, FR10.1, FR11, FR12): the per-item folders, tool lookup, the command,
the closed failure table, untrusted metadata, log redaction, and `run_media_item`, which runs one engine child in
a Job Object, watches its folder and ends the whole tree on every way out. Nothing here opens a socket or imports
`yt_dlp` (the package is only ever run as `python -m yt_dlp` in a child)."""

import contextlib
import json
import math
import os
import re
import stat
import sys
import time
from collections import namedtuple
from urllib.parse import urlsplit

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
# The engine's job (SPEC FR22.3): at most 16 processes at once (the engine, node, ffmpeg and what they start: 4 seen
# at the most, PLAN TK1 section 5; the floor of 16 decides), and no memory limit.
MEDIA_JOB_LIMITS = jobobject.JobLimits(memory_bytes=None, active_processes=16)

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

_URL = re.compile(r"(?<![a-z0-9+.\-])([a-z][a-z0-9+.\-]*)://(?:[^/\s?#@]*@)?([^/\s?#]*)[^\s]*", re.IGNORECASE)  # any scheme, any case
_BARE_URL = re.compile(r"(?<![:/\w])//(?:[^/\s?#@]*@)?([^/\s?#]+)[^\s]*")  # `//host/...`, no scheme
_ENCODED_URL = re.compile(r"(?<![a-z0-9+.\-])(?:[a-z][a-z0-9+.\-]*)?%3A%2F%2F\S*", re.IGNORECASE)  # `https%3A%2F%2F...`
IO_CAP = 2 * MAX_METADATA  # stdout.bin / stderr.bin are not meant to outgrow this


def make_item_dirs(job_dir, item_id, check, attempt=1):
    """Create `<job>/.tmp/<id>/{dl,home,cache,io}` (`<id>-<n>` for the n-th attempt, n >= 2: a process of an earlier
    attempt that got away cannot write into the folder of a later one); `check(path)` (raises when a path is a
    reparse point or otherwise not acceptable) sees every folder right after it is made."""
    if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id):
        raise ValueError("item id must be four digits")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be 1 or more")
    tmp = os.path.join(job_dir, ".tmp")
    root = os.path.join(tmp, item_id if attempt == 1 else "%s-%d" % (item_id, attempt))
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
            text = re.sub(re.escape(secret), "REDACTED", text, flags=re.IGNORECASE)
    text = _ENCODED_URL.sub("REDACTED", text)
    text = _URL.sub(lambda found: "%s://%s/REDACTED" % (found.group(1), found.group(2)), text)
    text = _BARE_URL.sub(lambda found: "//%s/REDACTED" % found.group(1), text)
    encoded = text.encode("utf-8", "replace")
    if len(encoded) > MAX_LOG:
        text = encoded[-MAX_LOG:].decode("utf-8", "ignore")
    return text


def _drop_link(path):
    """Take away a link or junction itself (never what it points to)."""
    for remove in (os.rmdir, os.remove):
        try:
            remove(path)
            return
        except OSError:
            continue


def _is_reparse(path):
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & REPARSE)
    except OSError:
        return False


def _remove_tree(path):
    try:
        with os.scandir(path) as entries:
            found = [(entry.path, entry.is_dir(follow_symlinks=False)) for entry in entries]
    except OSError:
        return
    for child, is_dir in found:
        if _is_reparse(child):
            _drop_link(child)
        elif is_dir:
            _remove_tree(child)
            with contextlib.suppress(OSError):
                os.rmdir(child)
        else:
            with contextlib.suppress(OSError):
                os.remove(child)


def clear_item_dir(root, check):
    """Remove `<job>/.tmp/<id>/` and all of it (FR10.5) without ever following a reparse point: a link in the
    place of the folder, or inside it, is removed as a link and what it points to is left alone. `check(path)`
    may veto (PathBlocked or any error: the folder is not touched). Whatever cannot be removed (a file still
    open) is left; this never raises for that."""
    try:
        os.lstat(root)
    except OSError:
        return
    if _is_reparse(root):
        _drop_link(root)
        return
    if check is not None:
        try:
            check(root)
        except Exception:  # noqa: BLE001 - PathBlocked or OSError: do not touch it
            return
    _remove_tree(root)
    with contextlib.suppress(OSError):
        os.rmdir(root)


class OutputError(Exception):
    """`dl/` does not hold exactly one acceptable media file; `reason` is `none`, `many` or `not_plain`."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def reparse_dir(path):
    """True when `path` is a reparse point (a junction or a symbolic link): the core never reads through one
    (SPEC FR22.4); shared by `dl/` and the output folder of the extraction child. False when it is not there."""
    return _is_reparse(path)


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
    if reparse_dir(dl):  # `dl/` was swapped for a link while the engine ran: what it points to is not read
        raise OutputError("not_plain")
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
    if reparse_dir(dl):
        return []
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


def tree_usage(root):
    """(total bytes of every plain file under `<job>/.tmp/<id>/`, the largest file in `io/`); no reparse point is
    followed or counted (FR12.4: the watch sees what the engine, node and ffmpeg write, not only `dl/`)."""
    total, io_largest = 0, 0
    io_dir = os.path.join(root, "io")
    pending = [root]
    while pending:
        here = pending.pop()
        try:
            with os.scandir(here) as entries:
                found = [entry.path for entry in entries]
        except OSError:
            continue
        for path in found:
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if getattr(info, "st_file_attributes", 0) & REPARSE:
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if here == io_dir:
                    io_largest = max(io_largest, info.st_size)
    return total, io_largest


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


def read_tail(path):
    """The last 256 KiB of a file as text (nothing when it cannot be read). When the file was longer, the first
    line is dropped: it may start in the middle of a URL, which redaction could no longer recognise."""
    data = _tail(path, MAX_LOG)
    if len(data) >= MAX_LOG:
        head, newline, rest = data.partition(b"\n")
        data = rest if newline else b""
    return data.decode("utf-8", "replace")


def _head(path, size):
    try:
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def _watch(proc, dirs, limits, budget, room, clock, deps, proxy=None):
    """The 0.5 s loop (FR12.4): returns the code that ended the run, or None when the engine exited by itself."""
    scan = deps.get("scan", scan_sizes)
    sleep = deps.get("sleep", time.sleep)
    poll = deps.get("poll", POLL)
    deadline = clock() + limits["media_item_timeout"]
    while True:
        exited = proc.poll() is not None
        verdict = "SIZE_LIMIT" if proxy is not None and proxy.over_cap.is_set() else None  # FR13.5
        verdict = verdict or _size_verdict(scan(dirs.dl), limits["max_bytes_per_file"], budget, room)
        if not verdict:
            total, io_largest = deps.get("tree_usage", tree_usage)(dirs.root)
            verdict = "SIZE_LIMIT" if io_largest > IO_CAP else ("OUTPUT_WRITE_FAILED" if total > room else None)
        if verdict or exited:
            return verdict
        if clock() >= deadline:
            return "TIMEOUT"
        sleep(poll)


def _denied_code(proxy, url):
    """FR13.10: the code of a run the proxy refused a destination of: the input's own host -> URL_BLOCKED, any
    other (a redirect, a CDN) -> REDIRECT_BLOCKED; None when the proxy refused nothing."""
    denied = list(proxy.denied) if proxy is not None else []
    if not denied:
        return None
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return "URL_BLOCKED" if any(name == host for name, _ in denied) else "REDIRECT_BLOCKED"


def _own_host_failed(proxy, url):
    """True when the proxy could not reach the very host of the input URL (not a CDN or a page it named)."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(str(name).lower().rstrip(".") == host for name, _ in list(getattr(proxy, "failures", None) or []))


def _failed_code(proxy):
    """(code, reason, retryable) of a run the proxy let through but could not connect for, else None. Through a
    proxy the engine never sees a lookup or a connect failure, only "502 Bad Gateway" (SPR-03 review RF1), so the
    proxy's own record is the evidence: a name that did not resolve, then a deadline, then a failed connect."""
    failures = list(getattr(proxy, "failures", None) or [])
    reasons = [reason for _, reason in failures]
    if "dns_failed" in reasons:
        return "NETWORK_ERROR", "dns_failed", True
    if "timeout" in reasons:
        return "TIMEOUT", None, True
    if failures:
        return "NETWORK_ERROR", "other", True
    return None


def _finish(proc, dirs, limits, warnings, proxy=None, url=""):
    """After the tree is dead: the outcome of an engine that ended by itself."""
    if proc.returncode != 0:
        refused = _denied_code(proxy, url)
        if refused:  # the proxy's word comes before the table (FR13.10)
            return _failed(refused, warnings=warnings)
        unreachable = _failed_code(proxy)
        if unreachable and _own_host_failed(proxy, url):  # the input's own host did not answer: that is the cause
            return _failed(unreachable[0], unreachable[1], unreachable[2], warnings)
        text = _tail(os.path.join(dirs.io, "stderr.bin"), MAX_LOG).decode("utf-8", "replace")
        verdict = classify_failure(text)
        if unreachable and verdict == FALLBACK:  # the engine's own words say only "502": the record is all there is
            return _failed(unreachable[0], unreachable[1], unreachable[2], warnings)
        return _failed(verdict[0], verdict[1], verdict[2], warnings)  # a firm word (403, 429, size...) beats a side host
    untrusted, declared, meta_warnings = parse_metadata(_head(os.path.join(dirs.io, "stdout.bin"), MAX_METADATA + 1))
    try:
        path = pick_output(dirs.dl)
    except OutputError as exc:
        if exc.reason == "none" and declared is not None and declared > limits["max_bytes_per_file"]:
            return _failed("SIZE_LIMIT", warnings=warnings + meta_warnings)  # exit 0, no file, declared too large
        return _failed("TOOL_FAILED", warnings=warnings + meta_warnings)
    return MediaOutcome("succeeded", None, None, False, path, untrusted, declared, warnings + meta_warnings)


def _run_engine(argv, env, dirs, limits, budget, room, clock, deps, streams, holder, proxy=None, url=""):
    kill = deps.get("kill_tree", jobobject.kill_tree)
    spawn = deps.get("spawn", jobobject.spawn_in_job)
    try:
        proc, job = spawn(argv, env, dirs.home, streams[0], streams[1], holder=holder, limits=MEDIA_JOB_LIMITS)
    except jobobject.JobObjectError:
        return _failed("TOOL_FAILED", "job_object_unavailable")  # the process (if any) was ended by the spawner
    except OSError:
        return _failed("TOOL_FAILED")
    verdict = _watch(proc, dirs, limits, budget, room, clock, deps, proxy)
    clean = kill(job, proc)  # before anything is read or picked: the tree is dead first (FR10.1)
    warnings = [] if clean else ["process_tree_not_empty"]
    if verdict:
        return _failed(verdict, warnings=warnings)  # a deadline or a size stop is never worth a retry
    if not clean:  # FR10.1: nothing is picked or taken while a process of the tree may still write
        return _failed("TOOL_FAILED", warnings=warnings)
    return _finish(proc, dirs, limits, warnings, proxy, url)


def run_media_item(url, item_id, dirs, limits, tools, *, budget, room, clock, deps, proxy=None):
    """Run the engine for one item and return a MediaOutcome (FR9.6, FR10.1, FR12). `budget` is what is left of
    `max_total_bytes`, `room` what the disk lets the folder use; `deps` injects `engine_prefix`, `spawn`,
    `kill_tree`, `scan`, `sleep`, `poll`, `environ`, `proxy_url`; `proxy` is the item's egress proxy (stops the run
    when its byte cap is crossed, names a refused destination, FR13.5, FR13.10). The engine's stdout and stderr go to files in
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
        return _run_engine(argv, env, dirs, limits, budget, room, clock, deps, streams, holder, proxy, url)
    except BaseException as exc:
        for proc, job in holder:  # an interrupt or a bug: the tree must not outlive this call
            if not kill(job, proc):
                exc.process_tree_not_empty = True  # the runner turns this into a job warning (FR12.2)
        raise
    finally:
        for proc, job in holder:
            job.close()
        for handle in streams:
            handle.close()
