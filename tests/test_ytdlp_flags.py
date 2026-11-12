"""FR4.3, FR5.1, FR5.2: the yt-dlp flag set is fixed, matches the plan word for word,
and never contains a forbidden flag. The agent cannot pass raw flags."""

import inspect
import os

import pytest

from agent_crawl_studio import flags

FFMPEG_DIR = r"C:\tools\ffmpeg\bin"
JOB = os.path.join("C:\\", "w", "jobs", "smoke-1")
NODE = r"C:\Program Files\nodejs\node.exe"


def build(**over):
    args = dict(
        ffmpeg_dir=FFMPEG_DIR,
        job_dir=JOB,
        max_filesize="50M",
        socket_timeout=20,
        node_path=NODE,
    )
    args.update(over)
    return flags.fixed_ytdlp_flags(**args)


def test_exact_fixed_set_in_order():
    expected = [
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-remote-components",
        "--no-exec",
        "--no-batch-file",
        "--no-playlist",
        "--use-extractors", "default,-generic",
        "--proxy", "",
        "--js-runtimes", "node:" + NODE,
        "--ffmpeg-location", FFMPEG_DIR,
        "--paths", "home:" + os.path.join(JOB, "files"),
        "--paths", "temp:" + os.path.join(JOB, ".tmp"),
        "--cache-dir", os.path.join(JOB, ".tmp", "cache"),
        "--max-filesize", "50M",
        "--socket-timeout", "20",
        "-o", "%(id)s.%(ext)s",
    ]
    assert build() == expected


@pytest.mark.parametrize(
    "forbidden", ["--cookies-from-browser", "--downloader", "--downloader-args", "--external-downloader"]
)
def test_forbidden_flags_absent(forbidden):
    assert forbidden not in build()
    assert forbidden in flags.FORBIDDEN_FLAGS


def test_required_safety_flags_present():
    got = build()
    for flag in ("--ignore-config", "--no-plugin-dirs", "--no-remote-components", "--no-exec", "--no-batch-file", "--no-playlist"):
        assert flag in got
    assert got[got.index("--proxy") + 1] == ""
    assert got[got.index("--use-extractors") + 1] == "default,-generic"
    assert got[got.index("--js-runtimes") + 1].startswith("node:")


def test_paths_use_home_and_temp_types():
    got = build()
    values = [got[i + 1] for i, v in enumerate(got) if v == "--paths"]
    assert [v.split(":", 1)[0] for v in values] == ["home", "temp"]


def test_no_raw_flag_parameter():
    params = set(inspect.signature(flags.fixed_ytdlp_flags).parameters)
    assert params == {"ffmpeg_dir", "job_dir", "max_filesize", "socket_timeout", "node_path"}


def test_validate_flags_accepts_fixed_set():
    flags.validate_flags(build())


@pytest.mark.parametrize(
    "bad",
    [
        ["--cookies-from-browser", "chrome"],
        ["--downloader", "ffmpeg"],
        ["--downloader-args", "ffmpeg:-i x"],
        ["--external-downloader", "aria2c"],
        ["--cookies-from-browser=chrome"],
        ["--external-downloader=aria2c"],
    ],
)
def test_validate_flags_rejects_forbidden(bad):
    with pytest.raises(ValueError):
        flags.validate_flags(build() + bad)


@pytest.mark.parametrize("field,value", [("max_filesize", "50M; calc"), ("max_filesize", ""), ("socket_timeout", "x"), ("socket_timeout", 0)])
def test_rejects_bad_numbers(field, value):
    with pytest.raises(ValueError):
        build(**{field: value})


def test_rejects_missing_paths():
    for field in ("ffmpeg_dir", "job_dir", "node_path"):
        with pytest.raises(ValueError):
            build(**{field: ""})
