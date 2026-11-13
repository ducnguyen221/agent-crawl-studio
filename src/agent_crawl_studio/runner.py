"""The planner and the runner. `plan` reports what `run` would do: no socket is opened (names are never resolved),
nothing is created or written, and the output place is decided only by `outroot.resolve_output`. `run` downloads
the batch into a new job folder, one item after another, and ends every path (also Ctrl+C) with a result.

`deps` is a dict that only code (tests) fills; the command line and the launcher pass none: `policy`
(urlpolicy.Policy), the outroot system-call parameters `attrs_of`, `drive_type_of`, `sync_root_of`,
`free_bytes_of`, `realpath`, and for `run` the hooks `fetch`, `clock`, `now`, `token_of`, `sleep`, `rename`,
`replace`, `motw`, `create_job`. No option, environment variable, config key or request field reaches it
(SPEC FR2.8)."""

import contextlib
import dataclasses
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

from . import artifacts, capabilities, egress, extract, manifest, netfetch, router
from . import config as config_mod
from . import envbuild, jobobject, limits, outroot, retrying, schema, sniff, urlpolicy, ytdlp
from .capabilities import WorkRootError
from .errors import HINTS, ITEM_STATUS_FOR, JOB_EXIT, REASONS, error_object, item_error
from .winpath import PathBlocked

ADAPTER_DIRECT = router.ADAPTER_DIRECT
UNSUPPORTED_MODES = ("gallery", "crawl")
OUTROOT_PARAMS = ("attrs_of", "drive_type_of", "sync_root_of", "free_bytes_of", "realpath")


def _media_hosts(env):
    try:
        return config_mod.load_config(env).media_host_allowlist
    except WorkRootError:
        return config_mod.DEFAULT_MEDIA_HOSTS


def _tools_present(env, deps):
    """True when ffmpeg, node and the `yt-dlp` package are all there (FR8.5): the first two by their paths, the
    package by its metadata, never by importing it."""
    which = deps.get("which") or shutil.which
    version_of = deps.get("version_of") or capabilities.default_version_of
    return ytdlp.find_tools(env, which, version_of) is not None


def _engine_present(deps):
    """True when the extract engine's package is installed (FR18.5): its metadata is read, the package never imported."""
    return extract.find_engine(deps.get("version_of") or capabilities.default_version_of) is not None


def _normalised(url, policy):
    """The NormalizedUrl of `url`, or None when the policy refuses it (its redaction then keeps the old way)."""
    try:
        return urlpolicy.check_url(url, policy)
    except urlpolicy.Blocked:
        return None


def _item(index, spec, mode, policy, media_hosts, tools_ok, engine_ok):
    norm = _normalised(spec.url, policy)
    base = {
        "id": "%04d" % index,
        "url_redacted": urlpolicy.redact(spec.url, norm),
        "url_sha256": urlpolicy.url_sha256(spec.url, policy),
        "expect": spec.expect,
    }
    try:
        if norm is None:
            raise urlpolicy.Blocked("url")
        adapter = router.route(norm, mode, media_hosts)
    except urlpolicy.Blocked:
        return dict(base, adapter=None, decision="blocked", error=item_error("URL_BLOCKED"))
    if adapter is None:
        return dict(base, adapter=None, decision="blocked", error=item_error("UNSUPPORTED_SOURCE"))
    if adapter == router.ADAPTER_MEDIA and not tools_ok():
        return dict(base, adapter=adapter, decision="blocked",
                    error=item_error("DEPENDENCY_MISSING", hint=HINTS["tools_missing"]))
    if adapter == router.ADAPTER_EXTRACT and not engine_ok():
        return dict(base, adapter=adapter, decision="blocked",
                    error=item_error("DEPENDENCY_MISSING", hint=HINTS["extract_engine_missing"]))
    return dict(base, adapter=adapter, decision="allow")


def _blocked(error):
    return {"decision": "blocked", "root": None, "parent": None, "error": error}


def plan(request, env, deps=None):
    deps = deps or {}
    policy = deps.get("policy") or urlpolicy.Policy()
    media_hosts = _media_hosts(env)
    present = []  # the tools are looked for once, and only when an item needs them

    def tools_ok():
        if not present:
            present.append(_tools_present(env, deps))
        return present[0]

    engine = []  # the extract engine is looked for once, and only in the `extract` mode

    def engine_ok():
        if not engine:
            engine.append(_engine_present(deps))
        return engine[0]

    items = [_item(n, spec, request.mode, policy, media_hosts, tools_ok, engine_ok)
             for n, spec in enumerate(request.urls, 1)]
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
    except OSError:  # a probe of the place failed (free space, a folder)
        error = error_object("OUTPUT_WRITE_FAILED")
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


# --- run (Slice B) -------------------------------------------------------------------------------------------------
JOB_ID_TRIES = 5
FREE_RESERVE = 2 ** 30  # an item that would leave less than 1 GiB free is not written
UNTRUSTED_HEADERS = (("content-type", "content_type"), ("content-disposition", "content_disposition"),
                     ("last-modified", "last_modified"), ("retry-after", "retry_after"))
REASON_CODES = ("NETWORK_ERROR", "CONTENT_MISMATCH")
EGRESS_LOG_LINES = 10000
NETWORK_FACTOR = 2  # the network budget of an item / a job is this many times its size limit (FR23, DN19)
EGRESS_LINE_MAX = 200
HEX_TOKEN = re.compile(r"[0-9a-f]{64}(?=:x@)")  # the shape of a proxy token in front of `:x@` (FR24.1)
MEDIA_DEPS = ("engine_prefix", "spawn", "kill_tree", "scan", "poll", "environ", "proxy_url")
EXTRACT_DEPS = ("extract_python", "extract_worker", "extract_job_limits", "extract_poll", "extract_sleep", "spawn",
                "kill_tree")
MARKDOWN_MIMES = frozenset({"text/markdown"})  # the one type the text artifact may have (FR21.2)
CLEAR_TRIES = 10  # an item folder of an extraction child that will not go is tried again, 50 ms apart
CLEAR_PAUSE = 0.05


def _stamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rejected(request, warnings, error, moment, code=2, status="failed"):
    """A job refused before it started: no job id, exit 2 (130 with status `cancelled` when it was interrupted)."""
    counts = {"total": len(request.urls), "succeeded": 0, "failed": 0, "blocked": 0, "cancelled": 0, "skipped": 0}
    result = {"schema_version": schema.SCHEMA_VERSION, "kind": "result", "job_id": None, "status": status,
              "started_at": _stamp(moment), "finished_at": _stamp(moment), "counts": counts,
              "limits_effective": request.limits_effective, "items": [], "warnings": warnings, "error": error}
    return result, code


def _write_error(exc):
    return error_object("OUTPUT_PATH_BLOCKED", hint=exc.reason) if isinstance(exc, PathBlocked) else error_object(
        "OUTPUT_WRITE_FAILED", hint=getattr(exc, "hint", None))


class RunLog:
    """logs/run.log: one line per item. The folder is made and checked here (a junction in its place refuses the
    job); the file is created exclusively and checked once; after that, any trouble just stops the logging."""

    def __init__(self, job_dir, check):
        self.directory = os.path.join(job_dir, "logs")
        self.path = os.path.join(self.directory, "run.log")
        self._check, self._handle, self._dead = check, None, False
        self.closers = []  # other files that end with the run (logs/egress.log)
        os.mkdir(self.directory)
        check(self.directory)

    def write(self, line):
        if self._dead:
            return
        try:
            if self._handle is None:  # created exclusively, checked once, then kept open: never reopened by path
                self._handle = open(self.path, "x", encoding="utf-8", newline="\n")
                self._check(self.path)
            self._handle.write(manifest.untrusted_text(line) + "\n")
            self._handle.flush()
        except PathBlocked:  # the file we just made sits somewhere it must not: take it away, stop logging
            self._dead = True
            self._close_file()
            with contextlib.suppress(OSError):
                os.remove(self.path)
        except OSError:
            self._dead = True
            self._close_file()

    def close(self):
        for closer in self.closers:
            closer()
        self._close_file()

    def _close_file(self):
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()


def _entry(policy, index, spec, code=None, outcome=None, artifact=None, skipped=False, adapter=None, untrusted=None,
           reason=None, retryable=None, hint=None):
    """The manifest item for an end state: a success (artifact), an error `code`, or `skipped`. `adapter`,
    `untrusted`, `reason`, `retryable` and `hint` describe a media item (a direct item has `outcome` instead)."""
    entry = {"id": "%04d" % index, "url_redacted": urlpolicy.redact(spec.url, _normalised(spec.url, policy)),
             "url_sha256": urlpolicy.url_sha256(spec.url, policy), "adapter": adapter,
             "status": "skipped" if skipped else "succeeded", "artifacts": [], "untrusted": dict(untrusted or {})}
    if artifact is not None:
        entry["adapter"] = adapter or "direct-http"
        entry["artifacts"] = [artifact]
    if outcome is not None:
        entry["adapter"] = adapter or "direct-http"  # `extract` downloads its page like the direct adapter
        raw = {name: outcome.headers_clean.get(header) for header, name in UNTRUSTED_HEADERS}
        raw["final_url_redacted"] = outcome.final_url_redacted
        entry["untrusted"] = manifest.clean_untrusted(raw)
        if outcome.http_status is not None:
            entry["http_status"] = outcome.http_status
        if outcome.redirects:
            entry["redirects"] = outcome.redirects
    if code is not None:
        entry["status"] = ITEM_STATUS_FOR[code]
        if outcome is not None:
            reason = outcome.reason if code in REASON_CODES and outcome.reason in REASONS else None
            retryable = outcome.retryable if code == "NETWORK_ERROR" else None
        elif code not in REASON_CODES:
            reason = None
        entry["error"] = item_error(
            code, reason=reason, http_status=outcome.http_status if outcome is not None and code in (
                "AUTH_REQUIRED", "RATE_LIMITED", "NETWORK_ERROR") else None, retryable=retryable, hint=hint)
    return entry


class _Job:
    """The state of one run: which item is next, what has been used, and why the rest must stop."""

    def __init__(self, request, deps, policy, writer, job_dir, media_hosts=(), env=None, check=None):
        self.request, self.policy, self.writer, self.job_dir = request, policy, writer, job_dir
        self.media_hosts, self.env, self.check = media_hosts, env or {}, check
        self.which = deps.get("which") or shutil.which
        self.version_of = deps.get("version_of") or capabilities.default_version_of
        self.egress_of = deps.get("egress") or egress.EgressProxy  # a new proxy for every media item (FR13.1)
        self.egress_lines = 0
        self.egress_lock = threading.Lock()
        self.egress_out = None  # logs/egress.log: created exclusively once, kept open, closed when the run ends
        self.egress_dead = False
        self.media_deps = {key: deps[key] for key in MEDIA_DEPS if key in deps}
        self.extract_deps = {key: deps[key] for key in EXTRACT_DEPS if key in deps}
        self.environ = deps["environ"] if "environ" in deps else envbuild.process_env()  # the child's is built from it
        self.engine = []  # the version of the extract engine, looked up once, when the first extract item needs it
        self.tools = []  # looked up once, when the first media item needs it
        self.warnings = []  # job-level warnings raised by items (`process_tree_not_empty`)
        self.limits = request.limits_effective
        self.clock = deps.get("clock", time.monotonic)
        self.sleep = deps.get("sleep", time.sleep)  # the wait between two attempts of an item (FR14.2)
        self.now = deps.get("now") or (lambda: datetime.now(timezone.utc))
        self.attempt = 1  # the number of the attempt in hand, and which wait ran out if it was a TIMEOUT
        self.timeout_kind = None
        self.fetch = deps.get("fetch") or netfetch.fetch_item
        self.free_bytes_of = deps.get("free_bytes_of", outroot.default_free_bytes_of)
        self.deadline = self.clock() + self.limits["job_timeout"]
        self.used = 0
        self.net_item = 0  # bytes received from the network by the item in hand, over all its attempts (FR23.1)
        self.net_job = 0  # ... by the whole job (FR23.2)
        self.tokens = []  # the token of every proxy this job started (FR24.1)
        self.cancelled = self.timed_out = False
        self.stop_code = None  # a code that fails every item that follows

    def entry(self, index, spec, code=None, outcome=None, artifact=None, skipped=False, **media):
        return _entry(self.policy, index, spec, code, outcome, artifact, skipped, **media)

    def next_item(self, index, spec):
        return self.run_with_retries(index, spec)

    def net_room(self):
        """What the item in hand may still receive from the network: the rest of its budget (2 x `max_bytes_per_file`
        minus what its attempts took) or of the job's (2 x `max_total_bytes`), whichever is less; never below 0."""
        return max(min(NETWORK_FACTOR * self.limits["max_bytes_per_file"] - self.net_item,
                       NETWORK_FACTOR * self.limits["max_total_bytes"] - self.net_job), 0)

    def count_net(self, count):
        """Bytes that came in from the network, for the item and for the job."""
        self.net_item += count
        self.net_job += count

    def run_with_retries(self, index, spec):
        """One item, tried again while its trouble can pass by itself (FR14, `retrying.should_retry`): at most
        `retries` more times, after the wait of `retrying.wait_seconds`, never past the job's deadline. Every attempt
        is a whole item of its own (its own part file, folder, proxy). A Ctrl+C in the wait leaves like any other."""
        attempt = 0
        self.net_item = 0
        while True:
            attempt += 1
            self.attempt, self.timeout_kind = attempt, None
            entry = self.attempt_item(index, spec)
            if attempt > 1:
                entry["attempts"] = attempt
            if attempt > self.limits["retries"] or self.cancelled or self.stop_code or self.timed_out:
                return entry
            if not retrying.should_retry(entry, entry.get("adapter"), self.timeout_kind):
                return entry
            if self.net_room() == 0:  # FR23.1: no network budget left: no further attempt, the last error stays
                return entry
            retry_after = entry.get("untrusted", {}).get("retry_after") if entry.get("http_status") in (
                retrying.RETRY_AFTER_STATUSES) else None
            wait = retrying.wait_seconds(attempt, retry_after, self.now())
            if wait is None or self.clock() + wait >= self.deadline:
                return entry
            self.sleep(wait)

    def attempt_item(self, index, spec):
        if self.cancelled:
            return self.entry(index, spec, "CANCELLED")
        if self.stop_code:
            return self.entry(index, spec, self.stop_code)
        if self.request.mode in UNSUPPORTED_MODES:
            return self.entry(index, spec, "UNSUPPORTED_SOURCE")
        if self.timed_out or self.clock() >= self.deadline:
            self.timed_out = True
            self.timeout_kind = "job"
            return self.entry(index, spec, "TIMEOUT")
        try:
            norm = urlpolicy.check_url(spec.url, self.policy)
            adapter = router.route(norm, self.request.mode, self.media_hosts)
        except urlpolicy.Blocked:
            return self.entry(index, spec, "URL_BLOCKED")
        if self.net_room() == 0:  # FR23.2: the job's network budget is spent: this item opens no connection
            return self.entry(index, spec, "SIZE_LIMIT", adapter=None if adapter == router.ADAPTER_DIRECT else adapter)
        if adapter == router.ADAPTER_MEDIA:
            return self.media(index, spec, norm)
        if adapter == router.ADAPTER_EXTRACT:
            return self.extract(index, spec)
        return self.download(index, spec)

    def extract(self, index, spec):
        """One page of the `extract` mode (FR18.1): the engine must be there, then the page is downloaded the direct way
        and read as text; the two artifacts count for `max_artifacts_per_item` (FR21.4)."""
        adapter = router.ADAPTER_EXTRACT
        if not self.engine:
            self.engine.append(extract.find_engine(self.version_of))
        if self.engine[0] is None:
            return self.entry(index, spec, "DEPENDENCY_MISSING", adapter=adapter, hint=HINTS["extract_engine_missing"])
        if self.limits["max_artifacts_per_item"] < extract.ARTIFACTS_PER_ITEM:
            return self.entry(index, spec, "SIZE_LIMIT", adapter=adapter)
        return self.download(index, spec, adapter)

    def media(self, index, spec, norm):
        if not self.tools:
            self.tools.append(ytdlp.find_tools(self.env, self.which, self.version_of))
        if self.tools[0] is None:
            return self.entry(index, spec, "DEPENDENCY_MISSING", adapter=router.ADAPTER_MEDIA,
                              hint=HINTS["tools_missing"])
        return self.media_and_place(index, spec, norm, self.tools[0])

    def media_and_place(self, index, spec, norm, tools):
        """One media item: probe the name, run the engine in its own folder, take the one file in, and take the
        folder away on every way out (a Ctrl+C passes through after the cleanup)."""
        item_id = "%04d" % index
        item_root = os.path.join(self.job_dir, ".tmp", item_id if self.attempt == 1 else "%s-%d" % (item_id, self.attempt))
        proxy = proxy_url = None
        try:
            dirs = ytdlp.make_item_dirs(self.job_dir, item_id, self.check, self.attempt)
            room = max(self.free_bytes_of(self.job_dir) - FREE_RESERVE, 0)
            limits = dict(self.limits, media_item_timeout=max(min(
                self.limits["media_item_timeout"], self.deadline - self.clock()), 0))
            proxy = self.egress_of(self.policy, byte_cap=self.net_room(),
                                   connect_timeout=self.limits["connect_timeout"], log=self.egress_line)
            try:
                proxy_url = proxy.start()
            except OSError:  # the port (a firewall, no free port), not the disk
                return self.entry(index, spec, "TOOL_FAILED", adapter=router.ADAPTER_MEDIA,
                                  hint=HINTS["proxy_unavailable"])
            deps = dict(self.media_deps, proxy_url=proxy_url)
            token = proxy_url.split("//", 1)[-1].split(":", 1)[0]
            self.tokens.append(token)
            outcome = ytdlp.run_media_item(norm.url, item_id, dirs, limits, tools,
                                           budget=max(self.limits["max_total_bytes"] - self.used, 0), room=room,
                                           clock=self.clock, deps=deps, proxy=proxy)
            self.note_core_job()
            self.engine_log(item_id, dirs, token)
            return self.media_result(index, spec, norm, tools, outcome)
        except PathBlocked:
            self.stop_code = "OUTPUT_PATH_BLOCKED"
            return self.entry(index, spec, "OUTPUT_PATH_BLOCKED")
        except OSError:  # the disk (folders, the free-space probe), not the engine
            return self.entry(index, spec, "OUTPUT_WRITE_FAILED")
        except Exception:  # noqa: BLE001 - one item's surprise fails that item only (a Ctrl+C still passes through)
            return self.entry(index, spec, "TOOL_FAILED", adapter=router.ADAPTER_MEDIA)
        finally:
            try:
                if proxy is not None:
                    proxy.stop()  # after `run_media_item` ended the tree; the port and tunnels close <= 2 s (FR13.8)
                    if proxy_url is not None:
                        self.count_net(proxy.bytes_in)  # what came back through it, whatever way the item ended
            finally:
                ytdlp.clear_item_dir(item_root, self.check)

    def egress_line(self, line):
        """logs/egress.log: one line per CONNECT the proxies saw, at most 10 000 lines a job, written under a lock;
        the file is created exclusively and checked once, then kept open (never reopened by path, like run.log);
        a log that cannot be written never fails an item (FR13.8). Every line is made safe in a fixed order (FR24.1):
        filter the characters, then hide the tokens, then cut the length - a hidden character between the letters of
        a token only joins them after the filter, so the hiding must come after it."""
        line = manifest.strip_unwanted(line)
        line = ytdlp.redact_log(line, secrets=tuple(self.tokens))
        line = HEX_TOKEN.sub("REDACTED", line)[:EGRESS_LINE_MAX]
        with self.egress_lock:
            if self.egress_dead or self.egress_lines >= EGRESS_LOG_LINES:
                return
            try:
                if self.egress_out is None:
                    logs = os.path.join(self.job_dir, "logs")
                    path = os.path.join(logs, "egress.log")
                    if self.check is not None:
                        self.check(logs)
                    self.egress_out = open(path, "x", encoding="utf-8", newline="\n")
                    if self.check is not None:
                        self.check(path)
                self.egress_out.write(line + "\n")
                self.egress_out.flush()
                self.egress_lines += 1
            except PathBlocked:  # the file we just made sits somewhere it must not: take it away, stop logging
                self.egress_dead = True
                made = self.egress_out is not None
                self.close_egress_log()
                if made:
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(self.job_dir, "logs", "egress.log"))
            except OSError:
                self.egress_dead = True
                self.close_egress_log()

    def close_egress_log(self):
        out, self.egress_out = self.egress_out, None
        if out is not None:
            with contextlib.suppress(OSError):
                out.close()

    def note_core_job(self):
        """The net behind the per-item job is missing: a job warning, once (FR22.2)."""
        if not jobobject.core_job_state() and "core_job_unavailable" not in self.warnings:
            self.warnings.append("core_job_unavailable")

    def note_tree(self, stop):
        """A Ctrl+C that says the tree of a child could not be emptied: a job warning, once (FR12.2)."""
        if getattr(stop, "process_tree_not_empty", False) and "process_tree_not_empty" not in self.warnings:
            self.warnings.append("process_tree_not_empty")

    def engine_log(self, item_id, dirs, token=""):
        """logs/<id>-ytdlp.log: the tail of the engine's stderr, URLs made safe, every line cleaned (FR9.3)."""
        try:
            text = ytdlp.read_tail(os.path.join(dirs.io, "stderr.bin"))
        except OSError:
            return  # a log that cannot be read never fails the item
        self.item_log(item_id, "ytdlp", text, (token,))

    def item_log(self, item_id, tag, text, secrets=()):
        """logs/<id>-<tag>.log (`<id>-<tag>-<n>.log` for the n-th attempt): `text` with every URL made safe and every
        line cleaned; nothing is written for an empty text, and a log that cannot be written never fails the item."""
        try:
            lines = [manifest.untrusted_text(line) for line in ytdlp.redact_log(text, secrets=secrets).splitlines()]
            if any(lines):
                logs = os.path.join(self.job_dir, "logs")
                if self.check is not None:
                    self.check(logs)  # a junction in the place of logs/ would send the log out of the job
                name = "%s-%s.log" % (item_id, tag) if self.attempt == 1 else "%s-%s-%d.log" % (item_id, tag, self.attempt)
                with open(os.path.join(logs, name), "x", encoding="utf-8", newline="\n") as out:
                    out.write("\n".join(lines) + "\n")
        except (OSError, PathBlocked):
            pass

    def media_result(self, index, spec, norm, tools, outcome):
        for warning in outcome.warnings:
            if warning not in self.warnings and warning != "metadata_unreadable":
                self.warnings.append(warning)
        adapter = router.ADAPTER_MEDIA
        if outcome.status != "succeeded":
            if outcome.code == "TIMEOUT":  # the proxy's connect timeout is `retryable`; the engine's own deadline is not
                if self.clock() >= self.deadline:
                    self.timed_out = True
                self.timeout_kind = "job" if self.timed_out else ("connect" if outcome.retryable else "item")
            hint = HINTS["job_object_unavailable"] if outcome.reason == "job_object_unavailable" else None
            return self.entry(index, spec, outcome.code, adapter=adapter, reason=outcome.reason, hint=hint,
                              retryable=outcome.retryable)
        redacted = urlpolicy.redact(spec.url, norm)
        try:
            artifact = self.writer.adopt(
                "%04d" % index, outcome.file_path, source_url_redacted=redacted, host_url=redacted,
                engine="yt-dlp", engine_version=tools.version,
                max_bytes=min(self.limits["max_bytes_per_file"], max(self.limits["max_total_bytes"] - self.used, 0)),
                allowed_mimes=sniff.MEDIA_MIMES)
        except PathBlocked:
            self.stop_code = "OUTPUT_PATH_BLOCKED"
            return self.entry(index, spec, "OUTPUT_PATH_BLOCKED", adapter=adapter)
        except artifacts.ArtifactError as exc:
            code = {"mime": "CONTENT_MISMATCH", "size": "SIZE_LIMIT"}.get(exc.reason, "OUTPUT_WRITE_FAILED")
            return self.entry(index, spec, code, adapter=adapter)
        if "metadata_unreadable" in outcome.warnings:
            artifact["warnings"] = list(artifact.get("warnings", [])) + ["metadata_unreadable"]
        self.used += artifact["bytes"]
        return self.entry(index, spec, None, artifact=artifact, adapter=adapter, untrusted=outcome.untrusted)

    def download(self, index, spec, adapter=None):
        """One page or file the direct way; `adapter` is `extract` when the page is to be read as text afterwards."""
        part = None
        try:
            try:
                part = self.writer.open("%04d" % index)
            except artifacts.ArtifactError:
                return self.entry(index, spec, "OUTPUT_WRITE_FAILED", adapter=adapter)
            except PathBlocked:
                self.stop_code = "OUTPUT_PATH_BLOCKED"
                return self.entry(index, spec, "OUTPUT_PATH_BLOCKED", adapter=adapter)
            return self.fetch_and_place(index, spec, part, adapter)
        except KeyboardInterrupt as stop:  # anywhere from the opening of the part to the end of the item
            self.note_tree(stop)
            if part is not None:
                self.writer.abort(part)
                if part.placed is not None:  # the file was already placed: it must not outlive the cancelled item
                    with contextlib.suppress(OSError):
                        os.remove(part.placed)
            self.writer.discard(["%04d" % index])
            self.cancelled = True
            return self.entry(index, spec, "CANCELLED")

    def fail(self, index, spec, part, code, outcome=None, adapter=None):
        self.writer.abort(part)
        return self.entry(index, spec, code, outcome, adapter=adapter)

    def fetch_and_place(self, index, spec, part, adapter=None):
        try:
            room = max(self.free_bytes_of(self.job_dir) - FREE_RESERVE, 0)
        except Exception as exc:  # noqa: BLE001 - a probe that fails fails this item, not the job
            return self.fail(index, spec, part, "OUTPUT_WRITE_FAILED" if isinstance(exc, OSError) else "TOOL_FAILED",
                             adapter=adapter)

        counted = [0]

        def sink(chunk):
            if part.bytes + len(chunk) > room:
                raise outroot.WriteBlocked(0, "space")
            part.write(chunk)
            self.count_net(len(chunk))  # counted as it comes, so that a Ctrl+C keeps what was received (FR23.3)
            counted[0] += len(chunk)

        limits = extract.page_limits(self.limits) if adapter == router.ADAPTER_EXTRACT else self.limits
        try:
            outcome = self.fetch(spec.url, spec.expect, limits, sink, self.policy,
                                 total_remaining=max(self.limits["max_total_bytes"] - self.used, 0),
                                 clock=self.clock, job_deadline=self.deadline, network_room=self.net_room())
        except (OSError, outroot.WriteBlocked):  # the disk, not the network
            return self.fail(index, spec, part, "OUTPUT_WRITE_FAILED", adapter=adapter)
        except Exception:  # noqa: BLE001 - an unexpected failure fails this item only; its text is never kept
            return self.fail(index, spec, part, "TOOL_FAILED", adapter=adapter)
        self.count_net(max(getattr(outcome, "bytes_received", 0) - counted[0], 0))  # held back, or past the sink
        if outcome.status != "ok":
            if outcome.code == "TIMEOUT" and self.clock() >= self.deadline:
                self.timed_out = True
            self.timeout_kind = getattr(outcome, "timeout_kind", None)
            return self.fail(index, spec, part, outcome.code, outcome, adapter)
        if adapter == router.ADAPTER_EXTRACT:
            return self.extract_page(index, spec, part, outcome)
        artifact, failure = self.place(index, spec, part, outcome, adapter)
        if failure is not None:
            return failure
        self.used += artifact["bytes"]
        return self.entry(index, spec, None, outcome, artifact)

    def place(self, index, spec, part, outcome, adapter, **marks):
        """The downloaded part goes into files/ (`marks`: `role`, `mime`, `extra` of `ArtifactWriter.commit`). Returns
        (the artifact, None) or (None, the failed item entry)."""
        try:
            return self.writer.commit(part, content_type=outcome.headers_clean.get("content-type"),
                                      source_url_redacted=outcome.final_url_redacted,
                                      host_url=outcome.final_url_redacted, **marks), None
        except PathBlocked:
            self.stop_code = "OUTPUT_PATH_BLOCKED"
            return None, self.fail(index, spec, part, "OUTPUT_PATH_BLOCKED", outcome, adapter)
        except Exception as exc:  # noqa: BLE001 - ArtifactError, a file taken away after the rename, anything else
            code = "OUTPUT_WRITE_FAILED" if isinstance(exc, (OSError, artifacts.ArtifactError)) else "TOOL_FAILED"
            return None, self.fail(index, spec, part, code, outcome, adapter)

    def extract_page(self, index, spec, part, outcome):
        """The page is in the part: when it is HTML (FR18.3) it is placed as the source artifact and read as text."""
        adapter = router.ADAPTER_EXTRACT
        if not extract.is_html(extract.read_head(part.path)):  # nothing is placed for a page that is not one
            return self.fail(index, spec, part, "CONTENT_MISMATCH", dataclasses.replace(outcome, reason="not_html"), adapter)
        html, failure = self.place(index, spec, part, outcome, adapter, role="source", mime="text/html",
                                   extra={"untrusted": True})
        if failure is not None:
            return failure
        return self.extract_placed(index, spec, outcome, html)

    def extract_placed(self, index, spec, outcome, html):
        """The page is placed (`html`): the child reads it in a folder of its own, the text is adopted as the first
        artifact. Every way out takes the folder away and, unless the item succeeded, the page too (FR19.6)."""
        item_id = "%04d" % index
        item_root = os.path.join(self.job_dir, ".tmp", item_id if self.attempt == 1 else "%s-%d" % (item_id, self.attempt))
        adapter, placed, entry = router.ADAPTER_EXTRACT, os.path.join(self.job_dir, html["path"]), None
        try:
            dirs = ytdlp.make_item_dirs(self.job_dir, item_id, self.check, self.attempt)
            result = extract.run_extract_item(placed, dirs.root, outcome.headers_clean.get("content-type"), self.limits,
                                              self.environ, self.extract_deps, self.clock, self.deadline, self.check)
            self.note_core_job()
            self.item_log(item_id, "extract", extract.stderr_tail(os.path.join(dirs.io, "stderr.bin")))
            entry = self.extract_result(index, spec, outcome, html, result, dirs)
        except PathBlocked:
            self.stop_code = "OUTPUT_PATH_BLOCKED"
            entry = self.entry(index, spec, "OUTPUT_PATH_BLOCKED", adapter=adapter)
        except OSError:  # the disk (the folders, the child's files), not the child
            entry = self.entry(index, spec, "OUTPUT_WRITE_FAILED", adapter=adapter)
        except Exception:  # noqa: BLE001 - one item's surprise fails that item only (a Ctrl+C still passes through)
            entry = self.entry(index, spec, "TOOL_FAILED", adapter=adapter)
        finally:
            self.clear_item(item_root)
            if entry is None or entry["status"] != "succeeded":  # a failure, or a Ctrl+C on its way out
                with contextlib.suppress(OSError):
                    os.remove(placed)
        return entry

    def clear_item(self, item_root):
        """Take the item folder away. The files of a child that was just ended can stay locked for a few milliseconds
        (Windows lets go of the handles of a dead process a moment after it is gone), so a folder that is still there is
        tried again, at most `CLEAR_TRIES` times; whatever cannot go is left, as `clear_item_dir` always does."""
        for _ in range(CLEAR_TRIES):
            ytdlp.clear_item_dir(item_root, self.check)
            if not os.path.lexists(item_root):
                return
            time.sleep(CLEAR_PAUSE)

    def extract_result(self, index, spec, outcome, html, result, dirs):
        """The item entry for what the child gave: an error code, or the two artifacts (the text first)."""
        adapter = router.ADAPTER_EXTRACT
        if "process_tree_not_empty" in result.warnings and "process_tree_not_empty" not in self.warnings:
            self.warnings.append("process_tree_not_empty")
        if result.status != "succeeded":
            if result.code == "TIMEOUT":
                self.timed_out = self.timed_out or result.timeout_kind == "job"
                self.timeout_kind = result.timeout_kind
            return self.entry(index, spec, result.code, adapter=adapter, reason=result.reason, hint=result.hint,
                              retryable=False)  # what went wrong in the extraction is final (FR19.3)
        item_id, redacted = "%04d" % index, outcome.final_url_redacted
        source = os.path.join(dirs.dl, "extract.md")  # in a folder only the core knows: the child cannot touch it
        with open(source, "x", encoding="utf-8", newline="\n") as out:
            out.write(extract.render_markdown(urlpolicy.redact(spec.url, _normalised(spec.url, self.policy)),
                                              html["sha256"], result.text))
        room = max(self.limits["max_total_bytes"] - self.used - html["bytes"], 0)  # both artifacts count (FR21.4)
        try:
            text = self.writer.adopt(
                item_id, source, source_url_redacted=redacted, host_url=redacted, engine=extract.ENGINE,
                engine_version=self.engine[0], max_bytes=min(self.limits["max_bytes_per_file"], room),
                allowed_mimes=MARKDOWN_MIMES, role="extract", mime="text/markdown",
                extra={"untrusted": True, "chars": len(result.text), "truncated": result.truncated})
        except PathBlocked:
            self.stop_code = "OUTPUT_PATH_BLOCKED"
            return self.entry(index, spec, "OUTPUT_PATH_BLOCKED", adapter=adapter)
        except artifacts.ArtifactError as exc:
            code = {"mime": "CONTENT_MISMATCH", "size": "SIZE_LIMIT"}.get(exc.reason, "OUTPUT_WRITE_FAILED")
            return self.entry(index, spec, code, adapter=adapter)
        text["warnings"] = list(text["warnings"]) + result.warnings
        self.used += html["bytes"] + text["bytes"]
        entry = self.entry(index, spec, None, outcome, text, adapter=adapter)
        entry["artifacts"] = [text, html]
        entry["untrusted"].update(manifest.clean_untrusted(result.meta))
        return entry


def _undo_job(job_dir):
    """A job refused after its folder was made: take away what this call made (os.rmdir/os.remove only: a
    junction is removed as a link, never followed, and a folder that is not empty stays)."""
    interrupted = False  # a Ctrl+C in here does not stop the cleanup; the caller is told
    for name in ("logs", ".tmp", "files", "manifest.json", None):
        path = job_dir if name is None else os.path.join(job_dir, name)
        for _ in range(2):  # a step that was interrupted is tried once more
            try:
                os.rmdir(path) if os.path.isdir(path) else os.remove(path)
                break
            except OSError:
                break
            except KeyboardInterrupt:
                interrupted = True
    return interrupted


def _refuse(request, warnings, job_dir, error, moment_of):
    """Take the new job folder away and answer: a refusal (`error`), or `cancelled`/130 when `error` is None or a
    Ctrl+C came during the cleanup."""
    if _undo_job(job_dir) or error is None:
        return _rejected(request, warnings, error_object("CANCELLED"), moment_of(), 130, "cancelled")
    return _rejected(request, warnings, error, moment_of())


def _place_error(exc):
    """The job-level error for a failure while the output place was decided or probed."""
    if isinstance(exc, PathBlocked):
        return error_object("OUTPUT_PATH_BLOCKED", hint=exc.reason)
    if isinstance(exc, outroot.WriteBlocked):
        return error_object("OUTPUT_WRITE_FAILED", hint="missing_gib=%d" % exc.missing_gib)
    return error_object("OUTPUT_WRITE_FAILED")  # a probe of the place failed (OSError)


def _resolve_place(request, env, deps, warnings, moment_of):
    """Config and output place. Returns ((root, out_params, warnings), None) or (None, (result, exit code))."""
    try:
        cfg = config_mod.load_config(env)
        out_params = {key: deps[key] for key in OUTROOT_PARAMS if key in deps}
        root = outroot.resolve_output(
            request.output_dir, cfg, env,
            required_free=limits.required_free_bytes(request.limits_effective, len(request.urls)), **out_params)
    except WorkRootError:
        return None, _rejected(request, warnings, error_object("WORK_ROOT_UNKNOWN"), moment_of())
    except KeyboardInterrupt:  # nothing was made yet
        return None, _rejected(request, warnings, error_object("CANCELLED"), moment_of(), 130, "cancelled")
    except (PathBlocked, outroot.WriteBlocked, OSError) as exc:  # OSError: a probe of the place failed
        return None, _rejected(request, warnings, _place_error(exc), moment_of())
    warnings = warnings + [warning for warning in root.warnings if warning not in warnings]
    return (root, out_params, warnings), None


def _create_job_dir(request, root, out_params, deps, warnings, moment_of, state):
    """The job folder under a fresh id. Returns ((job_dir, job_id, started), None) or (None, (result, exit code))."""
    create = deps.get("create_job", outroot.create_job)
    token_of = deps.get("token_of")
    for _ in range(JOB_ID_TRIES):
        started = moment_of()
        job_id = schema.new_job_id(started, token_of() if token_of else None)
        candidate = os.path.join(root.parent, job_id)
        existed = os.path.lexists(candidate)
        try:
            job_dir = create(root, job_id, **out_params)
            state["job_dir"] = job_dir
            return (job_dir, job_id, started), None
        except outroot.JobExists:
            continue  # never reuse or remove a folder that is already there: draw another id
        except KeyboardInterrupt:  # create_job takes away what it made; if it had just finished, we do
            if not existed:
                _undo_job(candidate)
            return None, _rejected(request, warnings, error_object("CANCELLED"), moment_of(), 130, "cancelled")
        except (PathBlocked, outroot.WriteBlocked) as exc:
            return None, _rejected(request, warnings, _write_error(exc), moment_of())
    return None, _rejected(request, warnings, error_object("OUTPUT_WRITE_FAILED", hint="job_id_taken"), moment_of())


def _open_writers(job_dir, root, out_params, deps, request, warnings, started, job_id, state):
    """The artifact writer, run log and manifest of a new job. Returns ((job_dir, writer, mf, log), None) or
    (None, (result, exit code)) after taking the folder away again."""
    moment_of = deps.get("now") or (lambda: datetime.now(timezone.utc))
    sleep = deps.get("sleep", time.sleep)
    realpath_args = {key: out_params[key] for key in ("attrs_of", "realpath") if key in out_params}

    def check(path):
        outroot.post_create_check(path, root, **realpath_args)

    try:
        writer = artifacts.ArtifactWriter(
            job_dir, rename=deps.get("rename", os.rename), motw=deps.get("motw", artifacts.set_motw), sleep=sleep,
            check=check)
        state["writer"] = writer
        log = RunLog(job_dir, check)
        mf = manifest.ManifestWriter(job_dir, replace=deps.get("replace", os.replace), sleep=sleep, check=check)
        state["log"], state["mf"], state["check"] = log, mf, check
        mf.start(job_id, _stamp(started), request.limits_effective, len(request.urls), warnings, state.get("retry_of"))
    except KeyboardInterrupt:  # Ctrl+C while the job is being set up: take it away again, say `cancelled`
        return None, _refuse(request, warnings, job_dir, None, moment_of)
    except (PathBlocked, artifacts.ArtifactError, manifest.ManifestError, OSError) as exc:
        error = _write_error(exc) if isinstance(exc, PathBlocked) else error_object("OUTPUT_WRITE_FAILED")
        return None, _refuse(request, warnings, job_dir, error, moment_of)
    return (job_dir, writer, mf, log), None


def _start_job(request, env, deps, policy, warnings, moment_of, state):
    """Config, output place, job folder, writers. Returns (job pieces, None) or (None, (result, exit code)): exit
    2 for a refusal, 130 (status `cancelled`) for a Ctrl+C while the job was being set up."""
    place, refusal = _resolve_place(request, env, deps, warnings, moment_of)
    if refusal is not None:
        return None, refusal
    root, out_params, warnings = place
    made, refusal = _create_job_dir(request, root, out_params, deps, warnings, moment_of, state)
    if refusal is not None:
        return None, refusal
    job_dir, job_id, started = made
    return _open_writers(job_dir, root, out_params, deps, request, warnings, started, job_id, state)


def _record(mf, log, job, entry):
    """Log and add one item to the manifest. A Ctrl+C here cancels the job (it is taken once per step and the
    step is repeated); the item is never lost: `add_item` puts it in memory before it writes."""
    line = "%s %s %s %s" % (entry["id"], entry["status"], entry.get("error", {}).get("code", "-"), entry["url_redacted"])
    steps = (lambda: log.write(line), lambda: None if any(item is entry for item in mf.doc["items"]) else mf.add_item(entry))
    for step in steps:
        for _ in range(2):
            try:
                step()
                break
            except KeyboardInterrupt:
                job.cancelled = True
    if not any(item is entry for item in mf.doc["items"]):  # two interrupts in a row before the append: keep it
        mf.add_item(entry, write=False)


def _listed(doc):
    """The file names a manifest lists."""
    return {os.path.basename(artifact["path"]) for item in doc["items"] for artifact in item["artifacts"]}


def _retry_interrupt(step, tries=2):
    """Run `step`; a Ctrl+C inside the safety net is taken and the step is repeated (`tries` times at most, then
    once more unguarded): every step is idempotent."""
    for _ in range(tries):
        try:
            return step()
        except KeyboardInterrupt:
            pass
    return step()


def _add_missing(request, mf, writer, policy, indices):
    """Every item this job was to run that the manifest does not list yet, as cancelled (by id, not by place:
    a `retry` job runs ids that are not 1, 2, 3...)."""
    listed = {item["id"] for item in mf.doc["items"]}
    for index, spec in zip(indices, request.urls):
        if "%04d" % index not in listed:
            writer.discard(["%04d" % index])  # a file placed for an item that was never recorded
            mf.add_item(_entry(policy, index, spec, "CANCELLED"), write=False)


def _finish_cancelled(mf, moment_of, net=None):
    try:  # the file if it can be written, else the memory copy with a warning
        mf.finish("cancelled", _stamp(moment_of()), None, [], write=True, network_bytes=net)
    except manifest.ManifestError:
        mf.finish("cancelled", _stamp(moment_of()), None, ["manifest_not_updated"], write=False, network_bytes=net)


def _salvage(request, state, policy, moment_of):
    """A Ctrl+C that no inner handler took, wherever it came. Before a manifest exists the job never started: take
    away what was made and say `cancelled` (no job id). With a manifest: list the items not recorded yet as
    cancelled and end the job as `cancelled`, unless it had already ended (then its own answer stands)."""
    writer, mf, job_dir = state.get("writer"), state.get("mf"), state.get("job_dir")
    if writer is not None:
        _retry_interrupt(writer.sweep)
    if mf is None or mf.doc is None:
        if job_dir is not None:
            _undo_job(job_dir)
        return _rejected(request, state["warnings"], error_object("CANCELLED"), moment_of(), 130, "cancelled")
    if mf.doc["status"] is None:
        _retry_interrupt(lambda: _add_missing(request, mf, writer, policy, state["indices"]))
        _retry_interrupt(lambda: writer.reconcile(_listed(mf.doc)))  # a manifest exists, so the writer does too
        job = state.get("job")
        _retry_interrupt(lambda: _finish_cancelled(mf, moment_of, None if job is None else job.net_job))
    _retry_interrupt(state["log"].close)  # likewise
    return mf.doc, JOB_EXIT[mf.doc["status"]]


def _scoped(request, only):
    """(the request of the items to run, their 1-based ids). `only` (a `retry` job) picks items of the old request;
    each keeps its own id, so the new job names them as the old one did."""
    if only is None:
        return request, list(range(1, len(request.urls) + 1))
    indices = sorted(set(only))
    return dataclasses.replace(request, urls=[request.urls[index - 1] for index in indices]), indices


def run(request, env, deps=None, *, only=None, retry_of=None):
    """Download the batch described by `request` into a new job folder. Returns (result, exit code); the result
    is the final manifest. Items run one after another, in order; a failure of one never stops the others.
    A Ctrl+C ends as `cancelled` (exit 130) with whatever exists, cleaned up: it leaves `run` only in its first few
    lines (before anything exists) or as a second interrupt inside the safety net's own retries."""
    deps = deps or {}
    policy = deps.get("policy") or urlpolicy.Policy()
    moment_of = deps.get("now") or (lambda: datetime.now(timezone.utc))
    state = {"warnings": list(request.warnings), "indices": None, "retry_of": retry_of}
    try:
        request, state["indices"] = _scoped(request, only)
        return _run(request, env, deps, policy, moment_of, state)
    except KeyboardInterrupt:
        return _salvage(request, state, policy, moment_of)


def _write_final(mf, job, status, error, extra, manifest_failed, moment_of):
    """The last write of the manifest: a Ctrl+C in it is taken and the write is tried again (three times, then the
    memory copy is the result and says the file is not up to date)."""
    done = False
    for _ in range(3):
        try:
            mf.finish(status, _stamp(moment_of()), error, extra, write=not manifest_failed, network_bytes=job.net_job)
            done = True
            break
        except KeyboardInterrupt:
            job.cancelled, status = True, "cancelled"
        except manifest.ManifestError:
            status, done = "failed", True
            mf.finish(status, _stamp(moment_of()), error_object("OUTPUT_WRITE_FAILED"), ["manifest_not_updated"], write=False,
                      network_bytes=job.net_job)
            break
    if not done:  # interrupted every time: the memory copy is the result, and it says the file is not up to date
        for _ in range(3):
            try:
                mf.finish(status, _stamp(moment_of()), error, extra + ["manifest_not_updated"], write=False,
                          network_bytes=job.net_job)
                break
            except KeyboardInterrupt:
                status = "cancelled"
        if mf.doc["status"] is None:  # even setting it in memory was interrupted every time
            mf.doc["status"], mf.doc["finished_at"] = "cancelled", _stamp(moment_of())


def _run(request, env, deps, policy, moment_of, state):
    pieces, refusal = _start_job(request, env, deps, policy, state["warnings"], moment_of, state)
    if refusal is not None:
        return refusal
    job_dir, writer, mf, log = pieces
    job = state["job"] = _Job(request, deps, policy, writer, job_dir, _media_hosts(env), env, state.get("check"))
    log.closers.append(job.close_egress_log)  # closed wherever the run log is: both ends have their Ctrl+C retries
    manifest_failed = False
    for index, spec in zip(state["indices"], request.urls):
        if manifest_failed:  # nothing more is run, but the result still lists every item (SPEC FR3.5)
            mf.add_item(job.entry(index, spec, skipped=True), write=False)
            continue
        try:
            entry = job.next_item(index, spec)
        except KeyboardInterrupt as stop:  # anywhere in an item that its own handlers do not cover
            job.cancelled = True
            job.note_tree(stop)
            writer.sweep()  # a .part opened just before the interrupt may not have reached its owner
            writer.discard(["%04d" % index])
            entry = job.entry(index, spec, "CANCELLED")
        try:
            _record(mf, log, job, entry)
        except manifest.ManifestError:
            manifest_failed = True
    if job.cancelled:  # an interrupted item may have left a finished file the manifest does not list
        writer.sweep()  # again: the handles of the interrupted frames are closed now, so a folder they held can go
        writer.reconcile(_listed(mf.doc))
    status = "failed" if (manifest_failed or job.timed_out) else schema.job_status(
        [item["status"] for item in mf.doc["items"]], cancelled=job.cancelled)
    error, extra = None, list(job.warnings)
    if manifest_failed:
        error, extra = error_object("OUTPUT_WRITE_FAILED"), ["manifest_not_updated"] + job.warnings
    _write_final(mf, job, status, error, extra, manifest_failed, moment_of)
    for _ in range(2):
        try:
            log.close()
            break
        except KeyboardInterrupt:
            pass
    return mf.doc, JOB_EXIT[mf.doc["status"]]
