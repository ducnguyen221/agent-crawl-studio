"""The planner and the runner. `plan` reports what `run` would do: no socket is opened (names are never resolved),
nothing is created or written, and the output place is decided only by `outroot.resolve_output`. `run` downloads
the batch into a new job folder, one item after another, and ends every path (also Ctrl+C) with a result.

`deps` is a dict that only code (tests) fills; the command line and the launcher pass none: `policy`
(urlpolicy.Policy), the outroot system-call parameters `attrs_of`, `drive_type_of`, `sync_root_of`,
`free_bytes_of`, `realpath`, and for `run` the hooks `fetch`, `clock`, `now`, `token_of`, `sleep`, `rename`,
`replace`, `motw`, `create_job`. No option, environment variable, config key or request field reaches it
(SPEC FR2.8)."""

import contextlib
import os
import shutil
import time
from datetime import datetime, timezone

from . import artifacts, capabilities, manifest, netfetch, router
from . import config as config_mod
from . import limits, outroot, schema, sniff, urlpolicy, ytdlp
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


def _normalised(url, policy):
    """The NormalizedUrl of `url`, or None when the policy refuses it (its redaction then keeps the old way)."""
    try:
        return urlpolicy.check_url(url, policy)
    except urlpolicy.Blocked:
        return None


def _item(index, spec, mode, policy, media_hosts, tools_ok):
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

    items = [_item(n, spec, request.mode, policy, media_hosts, tools_ok) for n, spec in enumerate(request.urls, 1)]
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
MEDIA_DEPS = ("engine_prefix", "spawn", "kill_tree", "scan", "poll", "environ", "proxy_url")


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
            self.close()
            with contextlib.suppress(OSError):
                os.remove(self.path)
        except OSError:
            self._dead = True
            self.close()

    def close(self):
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
        entry["adapter"] = "direct-http"
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
        self.dns_probe = deps.get("dns_probe") or urlpolicy.dns_probe
        self.media_deps = {key: deps[key] for key in MEDIA_DEPS if key in deps}
        self.tools = []  # looked up once, when the first media item needs it
        self.warnings = []  # job-level warnings raised by items (`process_tree_not_empty`)
        self.limits = request.limits_effective
        self.clock = deps.get("clock", time.monotonic)
        self.fetch = deps.get("fetch") or netfetch.fetch_item
        self.free_bytes_of = deps.get("free_bytes_of", outroot.default_free_bytes_of)
        self.deadline = self.clock() + self.limits["job_timeout"]
        self.used = 0
        self.cancelled = self.timed_out = False
        self.stop_code = None  # a code that fails every item that follows

    def entry(self, index, spec, code=None, outcome=None, artifact=None, skipped=False, **media):
        return _entry(self.policy, index, spec, code, outcome, artifact, skipped, **media)

    def next_item(self, index, spec):
        if self.cancelled:
            return self.entry(index, spec, "CANCELLED")
        if self.stop_code:
            return self.entry(index, spec, self.stop_code)
        if self.request.mode in UNSUPPORTED_MODES:
            return self.entry(index, spec, "UNSUPPORTED_SOURCE")
        if self.timed_out or self.clock() >= self.deadline:
            self.timed_out = True
            return self.entry(index, spec, "TIMEOUT")
        try:
            norm = urlpolicy.check_url(spec.url, self.policy)
            adapter = router.route(norm, self.request.mode, self.media_hosts)
        except urlpolicy.Blocked:
            return self.entry(index, spec, "URL_BLOCKED")
        if adapter == router.ADAPTER_MEDIA:
            return self.media(index, spec, norm)
        return self.download(index, spec)

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
        item_root = os.path.join(self.job_dir, ".tmp", item_id)
        try:
            verdict = self.dns_probe(norm.host)
            if verdict == urlpolicy.DNS_BLOCKED:
                return self.entry(index, spec, "URL_BLOCKED")
            if verdict != urlpolicy.DNS_OK:
                return self.entry(index, spec, "NETWORK_ERROR", adapter=router.ADAPTER_MEDIA, reason="dns_failed",
                                  retryable=True)
            dirs = ytdlp.make_item_dirs(self.job_dir, item_id, self.check)
            room = max(self.free_bytes_of(self.job_dir) - FREE_RESERVE, 0)
            limits = dict(self.limits, media_item_timeout=max(min(
                self.limits["media_item_timeout"], self.deadline - self.clock()), 0))
            outcome = ytdlp.run_media_item(norm.url, item_id, dirs, limits, tools,
                                           budget=max(self.limits["max_total_bytes"] - self.used, 0), room=room,
                                           clock=self.clock, deps=self.media_deps)
            self.engine_log(item_id, dirs)
            return self.media_result(index, spec, norm, tools, outcome)
        except PathBlocked:
            self.stop_code = "OUTPUT_PATH_BLOCKED"
            return self.entry(index, spec, "OUTPUT_PATH_BLOCKED")
        except OSError:  # the disk (folders, the free-space probe), not the engine
            return self.entry(index, spec, "OUTPUT_WRITE_FAILED")
        except Exception:  # noqa: BLE001 - one item's surprise fails that item only (a Ctrl+C still passes through)
            return self.entry(index, spec, "TOOL_FAILED", adapter=router.ADAPTER_MEDIA)
        finally:
            ytdlp.clear_item_dir(item_root, self.check)

    def engine_log(self, item_id, dirs):
        """logs/<id>-ytdlp.log: the tail of the engine's stderr, URLs made safe, every line cleaned (FR9.3)."""
        try:
            text = ytdlp.redact_log(ytdlp.read_tail(os.path.join(dirs.io, "stderr.bin")))
            lines = [manifest.untrusted_text(line) for line in text.splitlines()]
            if any(lines):
                logs = os.path.join(self.job_dir, "logs")
                if self.check is not None:
                    self.check(logs)  # a junction in the place of logs/ would send the log out of the job
                with open(os.path.join(logs, "%s-ytdlp.log" % item_id), "x", encoding="utf-8",
                          newline="\n") as out:
                    out.write("\n".join(lines) + "\n")
        except (OSError, PathBlocked):
            pass  # a log that cannot be written never fails the item

    def media_result(self, index, spec, norm, tools, outcome):
        for warning in outcome.warnings:
            if warning not in self.warnings and warning != "metadata_unreadable":
                self.warnings.append(warning)
        adapter = router.ADAPTER_MEDIA
        if outcome.status != "succeeded":
            if outcome.code == "TIMEOUT" and self.clock() >= self.deadline:
                self.timed_out = True
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

    def download(self, index, spec):
        part = None
        try:
            try:
                part = self.writer.open("%04d" % index)
            except artifacts.ArtifactError:
                return self.entry(index, spec, "OUTPUT_WRITE_FAILED")
            except PathBlocked:
                self.stop_code = "OUTPUT_PATH_BLOCKED"
                return self.entry(index, spec, "OUTPUT_PATH_BLOCKED")
            return self.fetch_and_place(index, spec, part)
        except KeyboardInterrupt:  # anywhere from the opening of the part to the end of the item
            if part is not None:
                self.writer.abort(part)
                if part.placed is not None:  # the file was already placed: it must not outlive the cancelled item
                    with contextlib.suppress(OSError):
                        os.remove(part.placed)
            self.writer.discard(["%04d" % index])
            self.cancelled = True
            return self.entry(index, spec, "CANCELLED")

    def fail(self, index, spec, part, code, outcome=None):
        self.writer.abort(part)
        return self.entry(index, spec, code, outcome)

    def fetch_and_place(self, index, spec, part):
        try:
            room = max(self.free_bytes_of(self.job_dir) - FREE_RESERVE, 0)
        except Exception as exc:  # noqa: BLE001 - a probe that fails fails this item, not the job
            return self.fail(index, spec, part, "OUTPUT_WRITE_FAILED" if isinstance(exc, OSError) else "TOOL_FAILED")

        def sink(chunk):
            if part.bytes + len(chunk) > room:
                raise outroot.WriteBlocked(0, "space")
            part.write(chunk)

        try:
            outcome = self.fetch(spec.url, spec.expect, self.limits, sink, self.policy,
                                 total_remaining=max(self.limits["max_total_bytes"] - self.used, 0),
                                 clock=self.clock, job_deadline=self.deadline)
        except (OSError, outroot.WriteBlocked):  # the disk, not the network
            return self.fail(index, spec, part, "OUTPUT_WRITE_FAILED")
        except Exception:  # noqa: BLE001 - an unexpected failure fails this item only; its text is never kept
            return self.fail(index, spec, part, "TOOL_FAILED")
        if outcome.status != "ok":
            if outcome.code == "TIMEOUT" and self.clock() >= self.deadline:
                self.timed_out = True
            return self.fail(index, spec, part, outcome.code, outcome)
        try:
            artifact = self.writer.commit(part, content_type=outcome.headers_clean.get("content-type"),
                                          source_url_redacted=outcome.final_url_redacted, host_url=outcome.final_url_redacted)
        except PathBlocked:
            self.stop_code = "OUTPUT_PATH_BLOCKED"
            return self.fail(index, spec, part, "OUTPUT_PATH_BLOCKED", outcome)
        except Exception as exc:  # noqa: BLE001 - ArtifactError, a file taken away after the rename, anything else
            code = "OUTPUT_WRITE_FAILED" if isinstance(exc, (OSError, artifacts.ArtifactError)) else "TOOL_FAILED"
            return self.fail(index, spec, part, code, outcome)
        self.used += artifact["bytes"]
        return self.entry(index, spec, None, outcome, artifact)


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
        mf.start(job_id, _stamp(started), request.limits_effective, len(request.urls), warnings)
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


def _add_missing(request, mf, writer, policy):
    for index, spec in enumerate(request.urls, 1):
        if index > len(mf.doc["items"]):
            writer.discard(["%04d" % index])  # a file placed for an item that was never recorded
            mf.add_item(_entry(policy, index, spec, "CANCELLED"), write=False)


def _finish_cancelled(mf, moment_of):
    try:  # the file if it can be written, else the memory copy with a warning
        mf.finish("cancelled", _stamp(moment_of()), None, [], write=True)
    except manifest.ManifestError:
        mf.finish("cancelled", _stamp(moment_of()), None, ["manifest_not_updated"], write=False)


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
        _retry_interrupt(lambda: _add_missing(request, mf, writer, policy))
        _retry_interrupt(lambda: writer.reconcile(_listed(mf.doc)))  # a manifest exists, so the writer does too
        _retry_interrupt(lambda: _finish_cancelled(mf, moment_of))
    _retry_interrupt(state["log"].close)  # likewise
    return mf.doc, JOB_EXIT[mf.doc["status"]]


def run(request, env, deps=None):
    """Download the batch described by `request` into a new job folder. Returns (result, exit code); the result
    is the final manifest. Items run one after another, in order; a failure of one never stops the others.
    A Ctrl+C ends as `cancelled` (exit 130) with whatever exists, cleaned up: it leaves `run` only in its first few
    lines (before anything exists) or as a second interrupt inside the safety net's own retries."""
    deps = deps or {}
    policy = deps.get("policy") or urlpolicy.Policy()
    moment_of = deps.get("now") or (lambda: datetime.now(timezone.utc))
    state = {"warnings": list(request.warnings)}
    try:
        return _run(request, env, deps, policy, moment_of, state)
    except KeyboardInterrupt:
        return _salvage(request, state, policy, moment_of)


def _run(request, env, deps, policy, moment_of, state):
    pieces, refusal = _start_job(request, env, deps, policy, state["warnings"], moment_of, state)
    if refusal is not None:
        return refusal
    job_dir, writer, mf, log = pieces
    job = _Job(request, deps, policy, writer, job_dir, _media_hosts(env), env, state.get("check"))
    manifest_failed = False
    for index, spec in enumerate(request.urls, 1):
        if manifest_failed:  # nothing more is run, but the result still lists every item (SPEC FR3.5)
            mf.add_item(job.entry(index, spec, skipped=True), write=False)
            continue
        try:
            entry = job.next_item(index, spec)
        except KeyboardInterrupt as stop:  # anywhere in an item that its own handlers do not cover
            job.cancelled = True
            if getattr(stop, "process_tree_not_empty", False) and "process_tree_not_empty" not in job.warnings:
                job.warnings.append("process_tree_not_empty")
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
    done = False
    for _ in range(3):  # a Ctrl+C during the final write is taken and the write is tried again
        try:
            mf.finish(status, _stamp(moment_of()), error, extra, write=not manifest_failed)
            done = True
            break
        except KeyboardInterrupt:
            job.cancelled, status = True, "cancelled"
        except manifest.ManifestError:
            status, done = "failed", True
            mf.finish(status, _stamp(moment_of()), error_object("OUTPUT_WRITE_FAILED"), ["manifest_not_updated"], write=False)
            break
    if not done:  # interrupted every time: the memory copy is the result, and it says the file is not up to date
        for _ in range(3):
            try:
                mf.finish(status, _stamp(moment_of()), error, extra + ["manifest_not_updated"], write=False)
                break
            except KeyboardInterrupt:
                status = "cancelled"
        if mf.doc["status"] is None:  # even setting it in memory was interrupted every time
            mf.doc["status"], mf.doc["finished_at"] = "cancelled", _stamp(moment_of())
    for _ in range(2):
        try:
            log.close()
            break
        except KeyboardInterrupt:
            pass
    return mf.doc, JOB_EXIT[mf.doc["status"]]
