"""Smoke: download one approved video through the pinned engine and prove it is readable.

Usage: python scripts/smoke_download.py --url <url> --work <dir> [--plan-only]
Run it with the Python of the venv `core` (the launcher's interpreter). stdout is ONE JSON line:
{"status": "ok" | "BLOCKED_ENV" | "FAIL" | "NOT_RUN", ...}; exit 0 only for "ok" (130 when interrupted).
Raw engine stderr goes to <job>/logs/stderr.txt, never to stdout. Code imports nothing of OpcOS.

`--work` is the PARENT folder: it must resolve (symlinks and junctions followed) to a place under
<work_root>/jobs, where work_root is CRAWL_STUDIO_WORK or <OpcOS>/workspaces/agent-crawl-studio.
Anything else is FAIL OUTPUT_PATH_BLOCKED and nothing is created. The script itself creates the
job folder `smoke-<yyyymmdd-hhmmss>-<6 random chars>` inside it (never reused); a `--work` that
already has a component starting with "smoke-" is refused (no nesting).

Network profile (SPR-01, "strict"): https only, port 443 only, no userinfo, host in
`config.DEFAULT_MEDIA_HOSTS` (the one list of allowed media hosts; config.example.json mirrors it and a test
keeps them equal), and every address the host resolves to must be public (`urlpolicy.dns_probe`). The folder
check is `outroot.confined_work`. This script carries no copy of any of these rules (SPEC FR6.1).
RESIDUAL RISK (RK, to be closed in a later sprint by an egress proxy): the check covers the URL we
pass and one DNS lookup. yt-dlp then follows HTTP redirects and makes its own lookups; a redirect
to another host, or a DNS answer that changes after our probe, is NOT blocked here.
"""

import argparse
import json
import os
import secrets
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from agent_crawl_studio import capabilities as cap  # noqa: E402
from agent_crawl_studio import config, envbuild, flags, outroot, urlpolicy  # noqa: E402
from agent_crawl_studio.errors import error_object  # noqa: E402
from agent_crawl_studio.urlpolicy import DNS_BLOCKED, DNS_FAIL, DNS_OK  # noqa: E402,F401

NETWORK_MARKERS = (
    "getaddrinfo failed",
    "name or service not known",
    "temporary failure in name resolution",
    "failed to resolve",
    "nodename nor servname",
)
MAX_FILESIZE = "50M"
SOCKET_TIMEOUT = 20
ITEM_TIMEOUT = 45 * 60  # SPEC section 9: one media item may take up to 45 minutes
PROBE_TIMEOUT = cap.PROBE_TIMEOUT


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def default_rand():
    return secrets.token_hex(3)


def _emit(out, obj):
    out.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def _host_allowed(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is not None and port != 443:
        return None
    for domain in config.DEFAULT_MEDIA_HOSTS:  # the core's list, read at call time
        if host == domain or host.endswith("." + domain):
            return host
    return None


def _result(out, status, code=1, **extra):
    obj = {"status": status}
    obj.update(extra)
    _emit(out, obj)
    return code


def main(argv, out=None, run=None, dns_probe=None, which=None, env=None, now=None, rand=None):
    out = out if out is not None else sys.stdout
    try:
        return _main(argv, out, run, dns_probe, which, env, now, rand)
    except KeyboardInterrupt:
        return _result(out, "FAIL", 130, error=error_object("INTERRUPTED"))


def _main(argv, out, run, dns_probe, which, env, now, rand):
    run = run or cap.system_run
    dns_probe = dns_probe or urlpolicy.dns_probe  # the core's probe, looked up at call time
    which = which or shutil.which
    base_env = env if env is not None else dict(os.environ)
    now = now or datetime.now
    rand = rand or default_rand

    parser = _Parser(prog="smoke_download.py", add_help=False)
    parser.add_argument("--url", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--plan-only", action="store_true")
    try:
        args = parser.parse_args(argv)
    except ValueError:
        _emit(out, {"status": "FAIL", "error": error_object("USAGE_ERROR")})
        return 2

    host = _host_allowed(args.url)
    if host is None:
        return _result(out, "FAIL", error=error_object("URL_BLOCKED"))

    try:
        work = outroot.confined_work(args.work, cap.work_root(base_env))
    except cap.WorkRootError:
        return _result(out, "FAIL", error=error_object("WORK_ROOT_UNKNOWN"))
    if work is None:
        return _result(out, "FAIL", error=error_object("OUTPUT_PATH_BLOCKED"))

    ffmpeg = cap.locate_ffmpeg(base_env, which)
    node = cap.locate_node(which, base_env)
    ffprobe = cap.locate_ffprobe(ffmpeg, which) if ffmpeg else None
    if not ffmpeg or not node or not ffprobe:
        return _result(out, "BLOCKED_ENV", error=error_object("DEPENDENCY_MISSING"))

    job = work / ("smoke-" + now().strftime("%Y%m%d-%H%M%S") + "-" + rand())
    work.mkdir(parents=True, exist_ok=True)
    try:
        job.mkdir(exist_ok=False)
    except FileExistsError:
        return _result(out, "FAIL", reason="job_exists")
    for sub in ("files", ".tmp", "logs"):
        (job / sub).mkdir()
    child_env = envbuild.build_subprocess_env(base_env, str(job / ".tmp"), node_dir=os.path.dirname(node))
    flag_list = flags.fixed_ytdlp_flags(os.path.dirname(ffmpeg), str(job), MAX_FILESIZE, SOCKET_TIMEOUT, node)
    flags.validate_flags(flag_list)
    # -P: no cwd / PYTHONPATH entry on sys.path; "--": the URL can never be read as a flag.
    command = [sys.executable, "-P", "-X", "utf8", "-m", "yt_dlp"] + flag_list + ["--", args.url]
    (job / "logs" / "command.txt").write_text("\n".join(command) + "\n", encoding="utf-8")
    info = {"job_dir": str(job), "host": host}

    if args.plan_only:
        return _result(out, "NOT_RUN", **info)
    verdict = dns_probe(host)
    if verdict == DNS_BLOCKED:
        return _result(out, "FAIL", error=error_object("URL_BLOCKED"), **info)
    if verdict != DNS_OK:
        return _result(out, "BLOCKED_ENV", reason="dns", **info)

    try:
        rc, stdout_text, stderr_text = run(command, env=child_env, timeout=ITEM_TIMEOUT, cwd=str(job))
    except Exception:  # noqa: BLE001 - never echo the exception text
        return _result(out, "FAIL", reason="runner_error", **info)
    (job / "logs" / "stdout.txt").write_text(stdout_text or "", encoding="utf-8")
    (job / "logs" / "stderr.txt").write_text(stderr_text or "", encoding="utf-8")
    if rc == cap.TIMEOUT_RC:
        return _result(out, "FAIL", reason="timeout", **info)
    if rc != 0:
        low = (stderr_text or "").lower()
        if any(marker in low for marker in NETWORK_MARKERS):
            return _result(out, "BLOCKED_ENV", reason="name_resolution", **info)
        return _result(out, "FAIL", reason="engine_exit_nonzero", **info)

    produced = [p for p in (job / "files").iterdir() if p.is_file() and not p.name.endswith(".part")]
    if not produced:
        return _result(out, "FAIL", reason="no_output_file", **info)
    best = max(produced, key=lambda p: p.stat().st_size)
    size = best.stat().st_size
    if size <= 0:
        return _result(out, "FAIL", reason="zero_bytes", **info)
    probe_cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(best)]
    try:
        prc, pout, _ = run(probe_cmd, env=child_env, timeout=PROBE_TIMEOUT, cwd=str(job))
    except Exception:  # noqa: BLE001 - never echo the exception text
        return _result(out, "FAIL", reason="runner_error", **info)
    if prc == cap.TIMEOUT_RC:
        return _result(out, "FAIL", reason="timeout", **info)
    if prc != 0:
        return _result(out, "FAIL", reason="ffprobe_failed", **info)
    return _result(out, "ok", 0, file=best.relative_to(job).as_posix(), bytes=size, duration=(pout or "").strip(), **info)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
