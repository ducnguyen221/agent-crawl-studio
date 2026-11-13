"""SPR-03 TK5 (FR9.1-FR9.7, FR11, SC14, SC20): the yt-dlp adapter's pure parts - item folders, tool lookup,
the command, the closed failure table, untrusted metadata, log redaction. No process of the adapter runs here
(TK7/TK8); the fixture `fake_ytdlp.py` is only exercised directly."""

import json
import os
import subprocess
import sys

import pytest

from agent_crawl_studio import envbuild, flags, limits, ytdlp

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "fixtures", "fake_ytdlp.py")
SRC = os.path.dirname(os.path.dirname(os.path.abspath(ytdlp.__file__)))
FFMPEG_EXE = r"C:\tools\ffmpeg\bin\ffmpeg.exe"
FFMPEG_DIR = r"C:\tools\ffmpeg\bin"
NODE = r"C:\Program Files\nodejs\node.exe"
URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def which_of(found):
    def which(name, path=None):
        return found.get(name)

    return which


ALL = {"ffmpeg": FFMPEG_EXE, "node": NODE}


def version_ok(name):
    assert name == "yt-dlp"
    return "2026.10.26"


def version_missing(name):
    raise LookupError(name)


# ---------------------------------------------------------------------------------------- item folders

def test_item_dirs_are_four_subfolders_of_tmp_item(tmp_path):
    seen = []
    dirs = ytdlp.make_item_dirs(str(tmp_path), "0001", seen.append)
    root = os.path.join(str(tmp_path), ".tmp", "0001")
    assert dirs.root == root
    assert (dirs.dl, dirs.home, dirs.cache, dirs.io) == tuple(os.path.join(root, n) for n in ("dl", "home", "cache", "io"))
    assert all(os.path.isdir(p) for p in (dirs.dl, dirs.home, dirs.cache, dirs.io))
    assert root in seen  # the injected check saw the item folder before anything was written in it


@pytest.mark.parametrize("bad", ["1", "00001", "../x", "abcd", "", "0001\\x"])
def test_item_id_must_be_four_digits(tmp_path, bad):
    with pytest.raises(ValueError):
        ytdlp.make_item_dirs(str(tmp_path), bad, None)
    assert not os.path.exists(os.path.join(str(tmp_path), ".tmp"))


def test_item_dirs_check_may_veto(tmp_path):
    def veto(path):
        raise OSError("junction")

    with pytest.raises(OSError):
        ytdlp.make_item_dirs(str(tmp_path), "0001", veto)


# ---------------------------------------------------------------------------------------- tools, command

def test_find_tools_needs_ffmpeg_node_and_the_package():
    tools = ytdlp.find_tools({"PATH": "x"}, which_of(ALL), version_ok)
    assert tools == ytdlp.Tools(FFMPEG_DIR, NODE, "2026.10.26")
    assert ytdlp.find_tools({"PATH": "x"}, which_of({"node": NODE}), version_ok) is None
    assert ytdlp.find_tools({"PATH": "x"}, which_of({"ffmpeg": FFMPEG_EXE}), version_ok) is None
    assert ytdlp.find_tools({"PATH": "x"}, which_of(ALL), version_missing) is None


def test_engine_prefix_isolates_the_interpreter():
    assert ytdlp.engine_prefix() == [sys.executable, "-P", "-X", "utf8", "-m", "yt_dlp"]


def test_importing_the_adapter_does_not_import_yt_dlp():
    code = "import sys; from agent_crawl_studio import ytdlp; sys.exit('yt_dlp' in sys.modules)"
    done = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": SRC}, timeout=60)
    assert done.returncode == 0


def test_build_command_is_prefix_flags_dashdash_url(tmp_path):
    dirs = ytdlp.make_item_dirs(str(tmp_path), "0007", None)
    tools = ytdlp.Tools(FFMPEG_DIR, NODE, "2026.10.26")
    effective, _ = limits.resolve({})
    prefix = ["py", "-m", "yt_dlp"]
    argv = ytdlp.build_command(prefix, URL, tools, dirs, effective)
    expected = flags.fixed_ytdlp_flags(FFMPEG_DIR, dirs.root, str(effective["max_bytes_per_file"]), 60, NODE)
    assert argv == prefix + expected + ["--", URL]
    assert argv[argv.index("--max-filesize") + 1] == "2147483648"
    assert argv[argv.index("--socket-timeout") + 1] == "60"
    assert argv[argv.index("--paths") + 1] == "home:" + dirs.dl
    assert argv[argv.index("-o") + 1] == "media.%(ext)s"
    assert argv[argv.index("--proxy") + 1] == ""


def test_build_command_rounds_a_fractional_idle_timeout_up(tmp_path):
    dirs = ytdlp.make_item_dirs(str(tmp_path), "0001", None)
    effective, _ = limits.resolve({"idle_timeout": 2.5})
    argv = ytdlp.build_command(["py"], URL, ytdlp.Tools(FFMPEG_DIR, NODE, "1"), dirs, effective)
    assert argv[argv.index("--socket-timeout") + 1] == "3"


def test_build_command_carries_the_proxy_and_validates_it(tmp_path):
    dirs = ytdlp.make_item_dirs(str(tmp_path), "0001", None)
    effective, _ = limits.resolve({})
    tools = ytdlp.Tools(FFMPEG_DIR, NODE, "1")
    proxy = "http://%s:x@127.0.0.1:5123" % ("cd" * 32)
    argv = ytdlp.build_command(["py"], URL, tools, dirs, effective, proxy_url=proxy)
    assert argv[argv.index("--proxy") + 1] == proxy
    with pytest.raises(ValueError):
        ytdlp.build_command(["py"], URL, tools, dirs, effective, proxy_url="http://evil")


# ---------------------------------------------------------------------------------------- failure table

def test_markers_table_is_closed_and_ordered():
    assert isinstance(ytdlp.MARKERS, tuple)
    assert all(isinstance(row, tuple) and len(row) == 4 for row in ytdlp.MARKERS)
    needles = [row[0] for row in ytdlp.MARKERS]
    assert all(n == n.lower() for n in needles)
    # TK1 spike: `--print` makes yt-dlp quiet, so "larger than max-filesize" is never printed; SIZE_LIMIT comes
    # from the core's own watch (FR12.4, FR12.5), not from this table.
    assert not any("max-filesize" in n for n in needles)
    assert "SIZE_LIMIT" not in {row[1] for row in ytdlp.MARKERS}
    order = [row[1] + ":" + str(row[2]) for row in ytdlp.MARKERS]
    assert order.index("RATE_LIMITED:None") < order.index("NETWORK_ERROR:forbidden")


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot", ("AUTH_REQUIRED", None, False)),
        ("ERROR: [youtube] x: Private video. Sign in", ("AUTH_REQUIRED", None, False)),
        ("ERROR: [youtube] x: members-only content", ("AUTH_REQUIRED", None, False)),
        ("ERROR: [youtube] x: HTTP Error 429: Too Many Requests", ("RATE_LIMITED", None, True)),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", ("NETWORK_ERROR", "forbidden", False)),
        ("ERROR: Unable to download webpage: getaddrinfo failed", ("NETWORK_ERROR", "dns_failed", True)),
        ("ERROR: Failed to resolve 'www.youtube.com'", ("NETWORK_ERROR", "dns_failed", True)),
        ("ERROR: Unsupported URL: https://x.example/", ("UNSUPPORTED_SOURCE", None, False)),
        ("ERROR: The read operation timed out", ("TIMEOUT", None, True)),
        ("ERROR: something nobody planned for", ("TOOL_FAILED", None, False)),
        ("ERROR: File is larger than max-filesize (3 bytes > 2 bytes)", ("TOOL_FAILED", None, False)),
        ("", ("TOOL_FAILED", None, False)),
    ],
)
def test_classify_failure_table(stderr, expected):
    assert ytdlp.classify_failure(stderr) == expected


def test_classify_failure_takes_the_first_marker_in_table_order_not_the_first_line():
    mixed = "ERROR: HTTP Error 403: Forbidden\nERROR: HTTP Error 429: Too Many Requests\n"
    assert ytdlp.classify_failure(mixed) == ("RATE_LIMITED", None, True)


def test_classify_failure_reads_only_error_lines():
    text = "WARNING: sign in to confirm\n[info] HTTP Error 429\nERROR: boom\n"
    assert ytdlp.classify_failure(text) == ("TOOL_FAILED", None, False)


# ---------------------------------------------------------------------------------------- metadata

def test_parse_metadata_cleans_and_reads_the_declared_size():
    raw = b'{"title":"a\\u202eb\\n","duration":19,"filesize_approx":5}'
    assert ytdlp.parse_metadata(raw) == ({"title": "ab", "duration": "19"}, 5, [])


def test_parse_metadata_maps_the_four_fields():
    raw = json.dumps({"title": "T", "uploader": "U", "duration": 19.5, "extractor_key": "Youtube", "filesize": 7,
                      "filesize_approx": 9, "url": "https://signed.example/?sig=SECRET"}).encode()
    untrusted, size, warnings = ytdlp.parse_metadata(raw)
    assert untrusted == {"title": "T", "uploader": "U", "duration": "19.5", "extractor": "Youtube"}
    assert size == 9 and warnings == []  # the larger of the two; nothing but the four fields is kept


def test_parse_metadata_title_is_at_most_200_clean_characters():
    raw = json.dumps({"title": "\x1b[31m\u202e" + "x" * 500 + "\n"}).encode()
    untrusted, size, _ = ytdlp.parse_metadata(raw)
    assert untrusted == {"title": "x" * 200} and size is None


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b'{"title":"a"}\n{"title":"b"}\n',
        b"[1, 2]",
        b'"a string"',
        b'{"title": ',
        b"[" * 5000 + b"]" * 5000,
        b"x" * (4 * 2 ** 20 + 1),
        b'{"title":"' + b"x" * (4 * 2 ** 20) + b'"}',
    ],
)
def test_parse_metadata_unreadable(raw):
    assert ytdlp.parse_metadata(raw) == ({}, None, ["metadata_unreadable"])


def test_parse_metadata_ignores_values_of_the_wrong_type():
    raw = b'{"title": 5, "uploader": ["a"], "duration": true, "filesize": true, "filesize_approx": "9", "extractor_key": null}'
    assert ytdlp.parse_metadata(raw) == ({}, None, [])


def test_parse_metadata_accepts_bytes_that_are_not_utf8():
    untrusted, _, warnings = ytdlp.parse_metadata(b'{"title": "caf\xe9"}')
    assert warnings == [] and untrusted["title"].startswith("caf")


# ---------------------------------------------------------------------------------------- log redaction

def test_redact_log_hides_urls_beyond_the_host_and_listed_secrets():
    text = "GET https://r.googlevideo.com/videoplayback/sig/SECRETPATH/x?sig=SECRET tok123"
    assert ytdlp.redact_log(text, secrets=("tok123",)) == "GET https://r.googlevideo.com/REDACTED REDACTED"


def test_redact_log_drops_userinfo_and_handles_the_proxy_url():
    token = "ab" * 32
    text = "proxy http://%s:x@127.0.0.1:5123 and https://u:pw@h.example.com/a b" % token
    got = ytdlp.redact_log(text, secrets=(token,))
    assert token not in got and "pw" not in got and "u:" not in got
    assert got == "proxy http://127.0.0.1:5123/REDACTED and https://h.example.com/REDACTED b"


def test_redact_log_ignores_empty_secrets_and_keeps_plain_text():
    assert ytdlp.redact_log("ERROR: boom\nline two", secrets=("", None)) == "ERROR: boom\nline two"


def test_redact_log_keeps_the_last_256_kib():
    assert ytdlp.MAX_LOG == 256 * 1024
    got = ytdlp.redact_log("A" * (300 * 1024) + "END")
    assert len(got.encode("utf-8")) == ytdlp.MAX_LOG and got.endswith("END")
    assert ytdlp.redact_log("short") == "short"


def test_redact_log_cut_in_the_middle_of_a_character_stays_valid_text():
    got = ytdlp.redact_log("\u00e9" * (200 * 1024))
    assert len(got.encode("utf-8")) <= ytdlp.MAX_LOG and set(got) == {"\u00e9"}


# ---------------------------------------------------------------------------------------- fixture + env

def run_fake(scenario, tmp_path, env=None):
    dl, io = tmp_path / "dl", tmp_path / "io"
    dl.mkdir()
    io.mkdir()
    argv = [sys.executable, "-P", FAKE, scenario, "--paths", "home:" + str(dl), "--", URL]
    done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60, cwd=str(tmp_path))
    return done, sorted(os.listdir(dl))


@pytest.mark.parametrize(
    "scenario,code,files",
    [
        ("ok_mp4", 0, ["media.mp4"]),
        ("ok_two_files", 0, ["media.mp4", "media.webm"]),
        ("fail_429", 1, []),
        ("fail_403", 1, []),
        ("fail_dns", 1, []),
        ("fail_mixed", 1, []),
        ("title_marker", 1, []),
        ("zero_files_big", 0, []),
        ("zero_files", 0, []),
        ("json_bad", 0, ["media.mp4"]),
        ("json_evil", 0, ["media.mp4"]),
        ("stderr_secret", 1, []),
    ],
)
def test_the_fake_engine_scenarios(tmp_path, scenario, code, files):
    done, got = run_fake(scenario, tmp_path)
    assert done.returncode == code and got == files


def test_the_fake_engine_failures_are_classified_as_the_table_says(tmp_path):
    expected = {"fail_429": "RATE_LIMITED", "fail_403": "NETWORK_ERROR", "fail_dns": "NETWORK_ERROR",
                "fail_mixed": "RATE_LIMITED", "title_marker": "TOOL_FAILED", "stderr_secret": "TOOL_FAILED"}
    for scenario, code in expected.items():
        sub = tmp_path / scenario
        sub.mkdir()
        done, _ = run_fake(scenario, sub)
        assert ytdlp.classify_failure(done.stderr)[0] == code, scenario


def test_the_child_environment_holds_only_allowed_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CRAWL_FAKE_API_KEY", "leak")
    home = tmp_path / "home"
    home.mkdir()
    env = envbuild.build_subprocess_env(dict(os.environ), str(home), node_dir=os.path.dirname(sys.executable))
    done, _ = run_fake("print_env", tmp_path, env=env)
    reported = json.loads(done.stdout)
    assert "CRAWL_FAKE_API_KEY" not in reported["keys"]
    assert set(reported["keys"]) <= set(envbuild.ALLOWED_KEYS)
    assert reported["TEMP"] == str(home)
    untrusted, _, warnings = ytdlp.parse_metadata(done.stdout.encode())
    assert warnings == [] and untrusted == {}  # the env report has none of the four fields
