"""FR4.3, FR5.1, FR5.2: the yt-dlp flag set is fixed, matches the plan word for word,
and never contains a forbidden flag. The agent cannot pass raw flags."""

import inspect
import os

import pytest

from agent_crawl_studio import flags

FFMPEG_DIR = r"C:\tools\ffmpeg\bin"
ITEM = os.path.join("C:\\", "w", "jobs", "smoke-1", ".tmp", "0001")
DL = os.path.join(ITEM, "dl")
FORMAT = "bv*[height<=1080]+ba/b[height<=1080]"
PRINT = "%(.{title,uploader,duration,extractor_key,filesize,filesize_approx})j"
NODE = r"C:\Program Files\nodejs\node.exe"


def build(**over):
    args = dict(
        ffmpeg_dir=FFMPEG_DIR,
        item_dir=ITEM,
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
        "--playlist-items", "1",
        "--use-extractors", "default,-generic",
        "--proxy", "",
        "--js-runtimes", "node:" + NODE,
        "--ffmpeg-location", FFMPEG_DIR,
        "--paths", "home:" + DL,
        "--paths", "temp:" + DL,
        "--cache-dir", os.path.join(ITEM, "cache"),
        "--max-filesize", "50M",
        "--socket-timeout", "20",
        "-f", FORMAT,
        "--no-mtime",
        "--no-progress",
        "--print", PRINT,
        "--no-simulate",
        "-o", "media.%(ext)s",
    ]
    assert build() == expected


def test_proxy_url_goes_after_dash_dash_proxy():
    url = "http://%s:x@127.0.0.1:5123" % ("ab" * 32)
    got = build(proxy_url=url)
    assert got[got.index("--proxy") + 1] == url
    flags.validate_flags(got)


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
    assert params == {"ffmpeg_dir", "item_dir", "max_filesize", "socket_timeout", "node_path", "proxy_url"}


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
    for field in ("ffmpeg_dir", "item_dir", "node_path"):
        with pytest.raises(ValueError):
            build(**{field: ""})


@pytest.mark.parametrize(
    "bad",
    [
        ["-f", "best"],
        ["-f", "bv*"],
        ["--format", FORMAT],
        ["--print", "%(url)s"],
        ["--print", "%(title)s"],
        ["--proxy=http://evil"],
        ["--proxy", "http://evil"],
        ["--proxy", "http://127.0.0.1:8080"],
        ["--proxy", "http://%s:x@127.0.0.1:99999999" % ("ab" * 32)],
        ["--proxy", "http://%s:x@10.0.0.1:5123" % ("ab" * 32)],
        ["--max-downloads", "1"],
    ],
)
def test_validate_flags_rejects_other_values_of_fixed_flags(bad):
    with pytest.raises(ValueError):
        flags.validate_flags(build() + bad)


def test_validate_flags_rejects_a_value_changed_inside_the_fixed_set():
    got = build()
    got[got.index("-f") + 1] = "best"
    with pytest.raises(ValueError):
        flags.validate_flags(got)
    got = build()
    got[got.index("--print") + 1] = "%(url)s"
    with pytest.raises(ValueError):
        flags.validate_flags(got)
    got = build()
    got[got.index("--proxy") + 1] = "http://evil"
    with pytest.raises(ValueError):
        flags.validate_flags(got)
