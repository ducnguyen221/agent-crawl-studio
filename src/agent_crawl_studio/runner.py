"""Planner (Slice A): `plan` reports what `run` would do. No socket is opened (names are never resolved),
nothing is created or written, and the output place is decided only by `outroot.resolve_output`.

`deps` is a dict that only code (tests) fills: `policy` (urlpolicy.Policy) and the outroot system-call
parameters `attrs_of`, `drive_type_of`, `sync_root_of`, `free_bytes_of`, `realpath`. No option, environment
variable, config key or request field reaches it (SPEC FR2.8)."""

import contextlib
import os
import time
from datetime import datetime, timezone

from . import artifacts, manifest, netfetch
from . import config as config_mod
from . import limits, outroot, schema, urlpolicy
from .capabilities import WorkRootError
from .errors import ITEM_STATUS_FOR, JOB_EXIT, REASONS, error_object, item_error
from .winpath import PathBlocked

ADAPTER_DIRECT = "direct-http"
UNSUPPORTED_MODES = ("media", "gallery", "crawl")
OUTROOT_PARAMS = ("attrs_of", "drive_type_of", "sync_root_of", "free_bytes_of", "realpath")


def _item(index, spec, mode, policy):
    base = {
        "id": "%04d" % index,
        "url_redacted": urlpolicy.redact(spec.url),
        "url_sha256": urlpolicy.url_sha256(spec.url, policy),
        "expect": spec.expect,
    }
    try:
        urlpolicy.check_url(spec.url, policy)
    except urlpolicy.Blocked:
        return dict(base, adapter=None, decision="blocked", error=item_error("URL_BLOCKED"))
    if mode in UNSUPPORTED_MODES:
        return dict(base, adapter=None, decision="blocked", error=item_error("UNSUPPORTED_SOURCE"))
    return dict(base, adapter=ADAPTER_DIRECT, decision="allow")


def _blocked(error):
    return {"decision": "blocked", "root": None, "parent": None, "error": error}


def plan(request, env, deps=None):
    deps = deps or {}
    policy = deps.get("policy") or urlpolicy.Policy()
    items = [_item(n, spec, request.mode, policy) for n, spec in enumerate(request.urls, 1)]
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


def _stamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rejected(request, warnings, error, moment):
    """A job refused before it started: no job id, exit 2 (SPEC FR1.7)."""
    counts = {"total": len(request.urls), "succeeded": 0, "failed": 0, "blocked": 0, "cancelled": 0, "skipped": 0}
    result = {"schema_version": schema.SCHEMA_VERSION, "kind": "result", "job_id": None, "status": "failed",
              "started_at": _stamp(moment), "finished_at": _stamp(moment), "counts": counts,
              "limits_effective": request.limits_effective, "items": [], "warnings": warnings, "error": error}
    return result, 2


def _write_error(exc):
    return error_object("OUTPUT_PATH_BLOCKED", hint=exc.reason) if isinstance(exc, PathBlocked) else error_object(
        "OUTPUT_WRITE_FAILED", hint=getattr(exc, "hint", None))


class RunLog:
    """logs/run.log: one line per item. The folder is made and checked here (a junction in its place refuses the
    job); the file is created exclusively and checked once; after that, any trouble just stops the logging."""

    def __init__(self, job_dir, check):
        self.directory = os.path.join(job_dir, "logs")
        self.path = os.path.join(self.directory, "run.log")
        self._check, self._started, self._dead = check, False, False
        os.mkdir(self.directory)
        check(self.directory)

    def write(self, line):
        if self._dead:
            return
        try:
            with open(self.path, "a" if self._started else "x", encoding="utf-8", newline="\n") as handle:
                handle.write(manifest.untrusted_text(line) + "\n")
            if not self._started:
                self._started = True
                self._check(self.path)
        except (OSError, PathBlocked):
            self._dead = True


class _Job:
    """The state of one run: which item is next, what has been used, and why the rest must stop."""

    def __init__(self, request, deps, policy, writer, job_dir):
        self.request, self.policy, self.writer, self.job_dir = request, policy, writer, job_dir
        self.limits = request.limits_effective
        self.clock = deps.get("clock", time.monotonic)
        self.fetch = deps.get("fetch") or netfetch.fetch_item
        self.free_bytes_of = deps.get("free_bytes_of", outroot.default_free_bytes_of)
        self.deadline = self.clock() + self.limits["job_timeout"]
        self.used = 0
        self.cancelled = self.timed_out = False
        self.stop_code = None  # a code that fails every item that follows

    def entry(self, index, spec, code=None, outcome=None, artifact=None, skipped=False):
        """The manifest item for an end state: a success (artifact), an error `code`, or `skipped`."""
        entry = {"id": "%04d" % index, "url_redacted": urlpolicy.redact(spec.url),
                 "url_sha256": urlpolicy.url_sha256(spec.url, self.policy), "adapter": None,
                 "status": "skipped" if skipped else "succeeded", "artifacts": [], "untrusted": {}}
        if artifact is not None:
            entry["adapter"] = "direct-http"
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
            reason = outcome.reason if outcome is not None and code in REASON_CODES and outcome.reason in REASONS else None
            entry["error"] = item_error(
                code, reason=reason, http_status=outcome.http_status if outcome is not None and code in (
                    "AUTH_REQUIRED", "RATE_LIMITED", "NETWORK_ERROR") else None,
                retryable=outcome.retryable if outcome is not None and code == "NETWORK_ERROR" else None)
        return entry

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
            urlpolicy.check_url(spec.url, self.policy)
        except urlpolicy.Blocked:
            return self.entry(index, spec, "URL_BLOCKED")
        return self.download(index, spec)

    def download(self, index, spec):
        try:
            part = self.writer.open("%04d" % index)
        except artifacts.ArtifactError:
            return self.entry(index, spec, "OUTPUT_WRITE_FAILED")
        except PathBlocked:
            self.stop_code = "OUTPUT_PATH_BLOCKED"
            return self.entry(index, spec, "OUTPUT_PATH_BLOCKED")
        try:
            return self.fetch_and_place(index, spec, part)
        except KeyboardInterrupt:  # anywhere between the open part and the placed file
            self.writer.abort(part)
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
    for name in ("logs", ".tmp", "files", "manifest.json"):
        path = os.path.join(job_dir, name)
        with contextlib.suppress(OSError):
            os.rmdir(path) if os.path.isdir(path) else os.remove(path)
    with contextlib.suppress(OSError):
        os.rmdir(job_dir)


def _start_job(request, env, deps, policy, warnings, moment_of):
    """Config, output place, job folder, writers. Returns (job pieces, None) or (None, (result, 2))."""
    try:
        cfg = config_mod.load_config(env)
        out_params = {key: deps[key] for key in OUTROOT_PARAMS if key in deps}
        root = outroot.resolve_output(
            request.output_dir, cfg, env,
            required_free=limits.required_free_bytes(request.limits_effective, len(request.urls)), **out_params)
    except WorkRootError:
        return None, _rejected(request, warnings, error_object("WORK_ROOT_UNKNOWN"), moment_of())
    except (PathBlocked, outroot.WriteBlocked) as exc:
        error = error_object("OUTPUT_PATH_BLOCKED", hint=exc.reason) if isinstance(exc, PathBlocked) else error_object(
            "OUTPUT_WRITE_FAILED", hint="missing_gib=%d" % exc.missing_gib)
        return None, _rejected(request, warnings, error, moment_of())
    warnings = warnings + [warning for warning in root.warnings if warning not in warnings]
    create = deps.get("create_job", outroot.create_job)
    token_of = deps.get("token_of")
    for _ in range(JOB_ID_TRIES):
        started = moment_of()
        job_id = schema.new_job_id(started, token_of() if token_of else None)
        try:
            job_dir = create(root, job_id, **out_params)
            break
        except outroot.JobExists:
            continue  # never reuse or remove a folder that is already there: draw another id
        except (PathBlocked, outroot.WriteBlocked) as exc:
            return None, _rejected(request, warnings, _write_error(exc), moment_of())
    else:
        return None, _rejected(request, warnings, error_object("OUTPUT_WRITE_FAILED", hint="job_id_taken"), moment_of())
    sleep = deps.get("sleep", time.sleep)
    realpath_args = {key: out_params[key] for key in ("attrs_of", "realpath") if key in out_params}

    def check(path):
        outroot.post_create_check(path, root, **realpath_args)

    try:
        writer = artifacts.ArtifactWriter(
            job_dir, rename=deps.get("rename", os.rename), motw=deps.get("motw", artifacts.set_motw), sleep=sleep,
            check=check)
        log = RunLog(job_dir, check)
        mf = manifest.ManifestWriter(job_dir, replace=deps.get("replace", os.replace), sleep=sleep, check=check)
        mf.start(job_id, _stamp(started), request.limits_effective, len(request.urls), warnings)
    except (PathBlocked, artifacts.ArtifactError, manifest.ManifestError, OSError) as exc:
        _undo_job(job_dir)
        error = _write_error(exc) if isinstance(exc, PathBlocked) else error_object("OUTPUT_WRITE_FAILED")
        return None, _rejected(request, warnings, error, moment_of())
    return (job_dir, writer, mf, log), None


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


def run(request, env, deps=None):
    """Download the batch described by `request` into a new job folder. Returns (result, exit code); the result
    is the final manifest. Items run one after another, in order; a failure of one never stops the others."""
    deps = deps or {}
    policy = deps.get("policy") or urlpolicy.Policy()
    moment_of = deps.get("now") or (lambda: datetime.now(timezone.utc))
    pieces, refusal = _start_job(request, env, deps, policy, list(request.warnings), moment_of)
    if refusal is not None:
        return refusal
    job_dir, writer, mf, log = pieces
    job = _Job(request, deps, policy, writer, job_dir)
    manifest_failed = False
    for index, spec in enumerate(request.urls, 1):
        if manifest_failed:  # nothing more is run, but the result still lists every item (SPEC FR3.5)
            mf.add_item(job.entry(index, spec, skipped=True), write=False)
            continue
        entry = job.next_item(index, spec)
        try:
            _record(mf, log, job, entry)
        except manifest.ManifestError:
            manifest_failed = True
    status = "failed" if (manifest_failed or job.timed_out) else schema.job_status(
        [item["status"] for item in mf.doc["items"]], cancelled=job.cancelled)
    error, extra = None, []
    if manifest_failed:
        error, extra = error_object("OUTPUT_WRITE_FAILED"), ["manifest_not_updated"]
    for _ in range(2):  # a Ctrl+C during the final write is taken once and the write is tried again
        try:
            mf.finish(status, _stamp(moment_of()), error, extra, write=not manifest_failed)
            break
        except KeyboardInterrupt:
            job.cancelled, status = True, "cancelled"
        except manifest.ManifestError:
            status = "failed"
            mf.finish(status, _stamp(moment_of()), error_object("OUTPUT_WRITE_FAILED"), ["manifest_not_updated"], write=False)
            break
    return mf.doc, JOB_EXIT[mf.doc["status"]]
