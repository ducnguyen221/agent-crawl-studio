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

# One file, at most 1080p, chosen by the tool and never by the agent (FR9.1); the output name is fixed.
# Only https formats: ffmpeg then merges local files and never opens a network URL (FR13.7, TK17 spike).
FORMAT = "bv*[height<=1080][protocol=https]+ba[protocol=https]/b[height<=1080][protocol=https]"
# One JSON line on stdout: the four untrusted fields and, for the core's own size rule, the declared size.
PRINT = "%(.{title,uploader,duration,extractor_key,filesize,filesize_approx})j"
OUTPUT_TEMPLATE = "media.%(ext)s"
# The only proxy a caller may pass: none, or the per-item token URL of the egress proxy (FR13.6).
_PROXY = re.compile(r"^http://[0-9a-f]{64}:x@127\.0\.0\.1:[0-9]{1,5}$")


def _need(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % name)
    return value


def fixed_ytdlp_flags(ffmpeg_dir, item_dir, max_filesize, socket_timeout, node_path, proxy_url=""):
    _need(ffmpeg_dir, "ffmpeg_dir")
    _need(item_dir, "item_dir")
    _need(node_path, "node_path")
    if not isinstance(max_filesize, str) or not _SIZE.match(max_filesize):
        raise ValueError("max_filesize must look like 50M")
    if isinstance(socket_timeout, bool) or not isinstance(socket_timeout, int) or socket_timeout <= 0:
        raise ValueError("socket_timeout must be a positive integer")
    if not isinstance(proxy_url, str) or (proxy_url and not _PROXY.match(proxy_url)):
        raise ValueError("proxy_url must be empty or the local per-item proxy URL")
    download = os.path.join(item_dir, "dl")
    return [
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-remote-components",
        "--no-exec",
        "--no-batch-file",
        "--no-playlist",
        "--playlist-items", "1",
        "--use-extractors", "default,-generic",
        "--proxy", proxy_url,
        "--js-runtimes", "node:" + node_path,
        "--ffmpeg-location", ffmpeg_dir,
        "--paths", "home:" + download,
        "--paths", "temp:" + download,
        "--cache-dir", os.path.join(item_dir, "cache"),
        "--max-filesize", max_filesize,
        "--socket-timeout", str(socket_timeout),
        "-f", FORMAT,
        "--no-mtime",
        "--no-progress",
        "--print", PRINT,
        "--no-simulate",
        "-o", OUTPUT_TEMPLATE,
    ]


ALLOWED_FLAGS = (
    "--ignore-config",
    "--no-plugin-dirs",
    "--no-remote-components",
    "--no-exec",
    "--no-batch-file",
    "--no-playlist",
    "--playlist-items",
    "--use-extractors",
    "--proxy",
    "--js-runtimes",
    "--ffmpeg-location",
    "--paths",
    "--cache-dir",
    "--max-filesize",
    "--socket-timeout",
    "-f",
    "--no-mtime",
    "--no-progress",
    "--print",
    "--no-simulate",
    "-o",
)

# Flags whose value is fixed: the next item of argv must be exactly this (callable: a check of the value).
_FIXED_VALUES = {
    "-f": lambda value: value == FORMAT,
    "--print": lambda value: value == PRINT,
    "--proxy": lambda value: value == "" or bool(_PROXY.match(value)),
    "-o": lambda value: value == OUTPUT_TEMPLATE,
}


def validate_flags(argv):
    """Allow-list check: every item that looks like a flag must equal one of ALLOWED_FLAGS exactly, and the
    value of `-f`, `--print`, `--proxy` and `-o` must be the fixed one.

    Abbreviations, `--flag=value` forms and any flag not in the fixed set are rejected, so a
    value starting with "-" is rejected too (values of the fixed set never start with "-")."""
    for at, item in enumerate(argv):
        if item.startswith("-") and item not in ALLOWED_FLAGS:
            raise ValueError("flag not in the fixed allow-list: %s" % item.split("=", 1)[0])
        accepts = _FIXED_VALUES.get(item)
        if accepts is not None and (at + 1 >= len(argv) or not accepts(argv[at + 1])):
            raise ValueError("value of %s is not the fixed one" % item)
