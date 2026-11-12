"""The fixed yt-dlp flag set. Callers cannot pass raw flags; every value is validated."""

import os
import re

# Documentation of what must never appear; enforcement is the allow-list below (ALLOWED_FLAGS).
FORBIDDEN_FLAGS = (
    "--cookies-from-browser",
    "--downloader",
    "--downloader-args",
    "--external-downloader",
)

_SIZE = re.compile(r"^[1-9][0-9]*[KMGkmg]?$")


def _need(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % name)
    return value


def fixed_ytdlp_flags(ffmpeg_dir, job_dir, max_filesize, socket_timeout, node_path):
    _need(ffmpeg_dir, "ffmpeg_dir")
    _need(job_dir, "job_dir")
    _need(node_path, "node_path")
    if not isinstance(max_filesize, str) or not _SIZE.match(max_filesize):
        raise ValueError("max_filesize must look like 50M")
    if isinstance(socket_timeout, bool) or not isinstance(socket_timeout, int) or socket_timeout <= 0:
        raise ValueError("socket_timeout must be a positive integer")
    job_tmp = os.path.join(job_dir, ".tmp")
    return [
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-remote-components",
        "--no-exec",
        "--no-batch-file",
        "--no-playlist",
        "--use-extractors", "default,-generic",
        "--proxy", "",
        "--js-runtimes", "node:" + node_path,
        "--ffmpeg-location", ffmpeg_dir,
        "--paths", "home:" + os.path.join(job_dir, "files"),
        "--paths", "temp:" + job_tmp,
        "--cache-dir", os.path.join(job_tmp, "cache"),
        "--max-filesize", max_filesize,
        "--socket-timeout", str(socket_timeout),
        "-o", "%(id)s.%(ext)s",
    ]


ALLOWED_FLAGS = (
    "--ignore-config",
    "--no-plugin-dirs",
    "--no-remote-components",
    "--no-exec",
    "--no-batch-file",
    "--no-playlist",
    "--use-extractors",
    "--proxy",
    "--js-runtimes",
    "--ffmpeg-location",
    "--paths",
    "--cache-dir",
    "--max-filesize",
    "--socket-timeout",
    "-o",
)


def validate_flags(argv):
    """Allow-list check: every item that looks like a flag must equal one of ALLOWED_FLAGS exactly.

    Abbreviations, `--flag=value` forms and any flag not in the fixed set are rejected, so a
    value starting with "-" is rejected too (values of the fixed set never start with "-")."""
    for item in argv:
        if item.startswith("-") and item not in ALLOWED_FLAGS:
            raise ValueError("flag not in the fixed allow-list: %s" % item.split("=", 1)[0])
