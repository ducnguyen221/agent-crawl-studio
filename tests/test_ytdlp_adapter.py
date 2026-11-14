"""SPR-03 TK5 (FR9.1-FR9.7, FR11, SC14, SC20): the yt-dlp adapter's pure parts - item folders, tool lookup,
the command, the closed failure table, untrusted metadata, log redaction. No process of the adapter runs here
(TK7/TK8); the fixture `fake_ytdlp.py` is only exercised directly."""

import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

from agent_crawl_studio import envbuild, flags, jobobject, limits, ytdlp
from fixtures import procs

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
    assert ytdlp.find_tools({"PATH": "x"}, which_of(ALL), lambda name: "") is None


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
    raw = json.dumps({"title": "\x1b\u202e" + "x" * 500 + "\n"}).encode()
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
    ids=["empty", "not_json", "two_objects", "array", "string", "truncated", "deeply_nested", "over_4mib",
         "object_over_4mib"],  # short ids: a 4 MiB id would overflow PYTEST_CURRENT_TEST
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
    assert {k.upper() for k in reported["keys"]} <= {k.upper() for k in envbuild.ALLOWED_KEYS}  # Windows upper-cases
    assert reported["TEMP"] == str(home)
    untrusted, _, warnings = ytdlp.parse_metadata(done.stdout.encode())
    assert warnings == [] and untrusted == {}  # the env report has none of the four fields


# ------------------------------------------------------------------- SPR-03 TK7: run_media_item (FR9.6, FR10.1, FR12)

MiB = 2 ** 20
GiB = 2 ** 30
TOOLS = ytdlp.Tools(FFMPEG_DIR, NODE, "2026.10.26")
LIMITS = limits.resolve({})[0]


def media(tmp_path, scenario, *, limits_in=None, budget=10 ** 12, room=10 ** 12, clock=time.monotonic, **deps_extra):
    dirs = ytdlp.make_item_dirs(str(tmp_path), "0001", None)
    deps = {"engine_prefix": lambda: [sys.executable, "-P", FAKE, scenario], "poll": 0.05}
    deps.update(deps_extra)
    outcome = ytdlp.run_media_item(URL, "0001", dirs, limits_in or LIMITS, TOOLS, budget=budget, room=room,
                                   clock=clock, deps=deps)
    return outcome, dirs


@pytest.fixture
def spawned():
    """Every (process, job) the code under test started; at teardown the processes are checked ended."""
    seen = []
    yield seen
    stragglers = procs.reap([proc.pid for proc, _ in seen])
    assert not stragglers, "processes left alive: %r" % (stragglers,)


def recording_spawn(spawned):
    def spawn(argv, env, cwd, out, err, holder=None):
        mine = [] if holder is None else holder
        result = jobobject.spawn_in_job(argv, env, cwd, out, err, holder=mine)
        spawned.append(mine[0])
        return result

    return spawn


def test_the_outcome_has_the_fields_of_the_contract():
    assert ytdlp.MediaOutcome._fields == ("status", "code", "reason", "retryable", "file_path", "untrusted",
                                          "declared_size", "warnings")


def test_a_good_run_returns_the_one_file_and_the_clean_metadata(tmp_path):
    outcome, dirs = media(tmp_path, "ok_mp4")
    assert (outcome.status, outcome.code, outcome.retryable) == ("succeeded", None, False)
    assert outcome.file_path == os.path.join(dirs.dl, "media.mp4")
    assert outcome.untrusted == {"title": "Me at the zoo", "uploader": "jawed", "duration": "19", "extractor": "Youtube"}
    assert outcome.warnings == []


def test_unreadable_metadata_is_a_warning_not_a_failure(tmp_path):
    outcome, _ = media(tmp_path, "json_bad")
    assert outcome.status == "succeeded" and outcome.warnings == ["metadata_unreadable"] and outcome.untrusted == {}


@pytest.mark.parametrize("scenario,code,reason,retryable", [
    ("fail_429", "RATE_LIMITED", None, True),
    ("fail_403", "NETWORK_ERROR", "forbidden", False),
    ("fail_dns", "NETWORK_ERROR", "dns_failed", True),
    ("title_marker", "TOOL_FAILED", None, False),
])
def test_a_failed_run_is_classified_from_its_stderr(tmp_path, scenario, code, reason, retryable):
    outcome, dirs = media(tmp_path, scenario)
    assert (outcome.status, outcome.code, outcome.reason, outcome.retryable) == ("failed", code, reason, retryable)
    assert outcome.file_path is None
    assert os.path.getsize(os.path.join(dirs.io, "stderr.bin")) > 0  # the raw log stays for the runner to redact


def test_an_engine_that_cannot_start_is_tool_failed(tmp_path):
    dirs = ytdlp.make_item_dirs(str(tmp_path), "0001", None)
    deps = {"engine_prefix": lambda: [r"C:\no\such\python.exe"], "poll": 0.05}
    outcome = ytdlp.run_media_item(URL, "0001", dirs, LIMITS, TOOLS, budget=10 ** 12, room=10 ** 12,
                                   clock=time.monotonic, deps=deps)
    assert (outcome.status, outcome.code, outcome.reason) == ("failed", "TOOL_FAILED", None)


def test_the_engine_runs_with_the_item_environment_and_folder(tmp_path):
    outcome, dirs = media(tmp_path, "print_env")
    assert outcome.status == "succeeded"
    with open(os.path.join(dirs.io, "stdout.bin"), "rb") as handle:
        reported = json.loads(handle.read())
    assert reported["TEMP"] == dirs.home


# ----------------------------------------------------------------------------- size watch (FR12.4, FR12.5)

def test_size_watch_a_stream_without_declared_length_is_cut_at_the_cap(tmp_path, spawned):
    started = time.monotonic()
    outcome, dirs = media(tmp_path, "big_stream", limits_in=dict(LIMITS, max_bytes_per_file=3 * MiB),
                          spawn=recording_spawn(spawned))
    assert (outcome.status, outcome.code, outcome.retryable) == ("failed", "SIZE_LIMIT", False)
    assert time.monotonic() - started < 20  # cut while writing (the fake writes for 60 s), not after it ended
    assert spawned[0][0].poll() is not None  # the engine was ended, not left writing


def test_size_watch_largest_file_over_the_budget(tmp_path):
    outcome, _ = media(tmp_path, "sleep_then_ok", budget=4, scan=lambda dl: [5])
    assert outcome.code == "SIZE_LIMIT"


def test_size_watch_total_over_twice_the_cap(tmp_path):
    cap = dict(LIMITS, max_bytes_per_file=4)
    outcome, _ = media(tmp_path, "sleep_then_ok", limits_in=cap, scan=lambda dl: [3, 3, 3])  # each <= 4, 9 > 8
    assert outcome.code == "SIZE_LIMIT"


def test_size_watch_a_merge_peak_inside_the_budget_is_allowed(tmp_path):
    peak = int(1.8 * GiB)
    outcome, _ = media(tmp_path, "sleep_then_ok", budget=3 * GiB, scan=lambda dl: [peak, peak])  # 3.6 GiB at peak
    assert outcome.status == "succeeded"


def test_size_watch_a_full_disk_is_an_output_failure(tmp_path):
    outcome, _ = media(tmp_path, "sleep_then_ok", room=5, scan=lambda dl: [10])
    assert (outcome.status, outcome.code, outcome.retryable) == ("failed", "OUTPUT_WRITE_FAILED", False)


def test_size_watch_default_scan_counts_regular_files_only(tmp_path):
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / "a.part").write_bytes(b"x" * 10)
    (dl / "b").write_bytes(b"y" * 5)
    (dl / "sub").mkdir()
    (dl / "sub" / "c").write_bytes(b"z" * 99)
    assert sorted(ytdlp.scan_sizes(str(dl))) == [5, 10]
    assert ytdlp.scan_sizes(str(tmp_path / "gone")) == []


@pytest.mark.parametrize("scenario,code", [
    ("zero_files_big", "SIZE_LIMIT"),  # exit 0, no file, a declared size over the cap
    ("zero_files", "TOOL_FAILED"),     # exit 0, no file, nothing declared
    ("ok_two_files", "TOOL_FAILED"),   # exit 0, two matching files
])
def test_zero_files_rule(tmp_path, scenario, code):
    outcome, _ = media(tmp_path, scenario)
    assert (outcome.status, outcome.code, outcome.file_path) == ("failed", code, None)


def test_media_timeout(tmp_path, fake_clock, spawned):
    def sleep(seconds):
        time.sleep(0.05)
        fake_clock.sleep(1.0)

    outcome, _ = media(tmp_path, "sleep_then_ok", limits_in=dict(LIMITS, media_item_timeout=3), clock=fake_clock,
                       sleep=sleep, spawn=recording_spawn(spawned))
    assert (outcome.status, outcome.code, outcome.retryable) == ("failed", "TIMEOUT", False)
    assert spawned[0][0].poll() is not None


def test_tree_dead_before_pick(tmp_path, monkeypatch):
    order = []
    real_kill = jobobject.kill_tree
    real_pick = ytdlp.pick_output

    def kill(job, proc, wait=5.0):
        order.append("kill")
        return real_kill(job, proc, wait)

    def pick(dl):
        order.append("pick")
        return real_pick(dl)

    monkeypatch.setattr(ytdlp, "pick_output", pick)
    outcome, _ = media(tmp_path, "ok_mp4", kill_tree=kill)
    assert outcome.status == "succeeded" and order == ["kill", "pick"]


# ----------------------------------------------------------------------------------------- pick_output

def test_pick_output_takes_the_one_whitelisted_file(tmp_path):
    (tmp_path / "media.mp4").write_bytes(b"x")
    (tmp_path / "media.f251.webm").write_bytes(b"y")  # an intermediate: two dots, not on the whitelist
    (tmp_path / "other.mp4").write_bytes(b"z")
    assert ytdlp.pick_output(str(tmp_path)) == os.path.join(str(tmp_path), "media.mp4")


@pytest.mark.parametrize("names,reason", [([], "none"), (["media.f1.mp4"], "none"), (["media.mp4", "media.webm"], "many")])
def test_pick_output_refuses_zero_or_many(tmp_path, names, reason):
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    with pytest.raises(ytdlp.OutputError) as caught:
        ytdlp.pick_output(str(tmp_path))
    assert caught.value.reason == reason


def test_pick_output_refuses_a_hard_link_and_a_directory(tmp_path):
    (tmp_path / "src").write_bytes(b"x")
    os.link(str(tmp_path / "src"), str(tmp_path / "media.mp4"))  # st_nlink == 2
    with pytest.raises(ytdlp.OutputError) as caught:
        ytdlp.pick_output(str(tmp_path))
    assert caught.value.reason == "not_plain"
    (tmp_path / "media.mp4").unlink()
    (tmp_path / "media.mp4").mkdir()
    with pytest.raises(ytdlp.OutputError):
        ytdlp.pick_output(str(tmp_path))


# ------------------------------------------------------------------ I/O error branches of the folder helpers (TK8)

def test_a_missing_folder_or_log_reads_as_nothing(tmp_path):
    gone = str(tmp_path / "gone")
    with pytest.raises(ytdlp.OutputError) as caught:
        ytdlp.pick_output(gone)
    assert caught.value.reason == "none"
    assert ytdlp._tail(gone, 10) == b"" and ytdlp._head(gone, 10) == b""
    assert ytdlp._regular(gone) is False


def test_a_file_that_vanishes_during_the_scan_is_skipped(tmp_path, monkeypatch):
    (tmp_path / "a").write_bytes(b"xx")
    (tmp_path / "b").write_bytes(b"yyy")
    real = os.lstat

    def flaky(path, *args, **kwargs):
        if str(path).endswith("a"):
            raise FileNotFoundError(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(ytdlp.os, "lstat", flaky)
    assert ytdlp.scan_sizes(str(tmp_path)) == [3]


def test_the_tail_and_head_of_a_log(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(b"0123456789")
    assert ytdlp._tail(str(path), 4) == b"6789" and ytdlp._head(str(path), 4) == b"0123"


# ------------------------------------------------------------------------- SPR-03 TK9: clear_item_dir (FR10.5)

def test_clear_item_dir_removes_the_whole_tree(tmp_path):
    dirs = ytdlp.make_item_dirs(str(tmp_path), "0001", None)
    (tmp_path / ".tmp" / "0001" / "dl" / "media.mp4").write_bytes(b"x")
    (tmp_path / ".tmp" / "0001" / "home" / "deep").mkdir()
    (tmp_path / ".tmp" / "0001" / "home" / "deep" / "f").write_bytes(b"y")
    ytdlp.clear_item_dir(dirs.root, None)
    assert not os.path.exists(dirs.root) and (tmp_path / ".tmp").is_dir()
    ytdlp.clear_item_dir(dirs.root, None)  # already gone: nothing to do


def test_clear_item_dir_never_follows_junction(tmp_path):
    import _winapi
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "keep.txt").write_text("mine")
    root = tmp_path / ".tmp" / "0001"
    (tmp_path / ".tmp").mkdir()
    _winapi.CreateJunction(str(secret), str(root))
    ytdlp.clear_item_dir(str(root), None)
    assert (secret / "keep.txt").read_text() == "mine"


def test_clear_item_dir_never_follows_junction_inside_the_tree(tmp_path):
    import _winapi
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "keep.txt").write_text("mine")
    dirs = ytdlp.make_item_dirs(str(tmp_path), "0001", None)
    _winapi.CreateJunction(str(secret), str(tmp_path / ".tmp" / "0001" / "dl" / "sub"))
    ytdlp.clear_item_dir(dirs.root, None)
    assert (secret / "keep.txt").read_text() == "mine" and not os.path.exists(dirs.root)


def test_clear_item_dir_obeys_a_veto_from_check(tmp_path):
    from agent_crawl_studio.winpath import PathBlocked
    dirs = ytdlp.make_item_dirs(str(tmp_path), "0001", None)

    def veto(path):
        raise PathBlocked("reparse")
    ytdlp.clear_item_dir(dirs.root, veto)
    assert os.path.isdir(dirs.dl), "a vetoed folder is not touched"


def test_clear_item_dir_survives_a_file_that_cannot_be_removed(tmp_path):
    dirs = ytdlp.make_item_dirs(str(tmp_path), "0001", None)
    handle = open(os.path.join(dirs.dl, "media.mp4"), "wb")
    try:
        ytdlp.clear_item_dir(dirs.root, None)  # a file in use: no exception
    finally:
        handle.close()


def test_the_folder_helpers_ignore_what_is_not_there(tmp_path):
    gone = str(tmp_path / "gone")
    ytdlp._drop_link(gone)
    assert ytdlp._is_reparse(gone) is False
    ytdlp._remove_tree(gone)
    assert not os.path.exists(gone)


# ---------------------------------------------------------------------------- vòng sửa 1 (review lát A)

@pytest.mark.parametrize("text", [
    "HTTPS://r.example.com/v?sig=AAASECRET",
    "GET //r.example.com/v?sig=BBBSECRET now",
    "rtmps://h.example.com/x?token=CCCSECRET",
    "wss://h.example.com/x?token=DDDSECRET",
    "ERROR: https%3A%2F%2Fr.example.com%2Fv%3Fsig%3DEEESECRET end",
    "Https%3a%2f%2fh.example.com%2Fv%3Fsig%3DFFFSECRET",
])
def test_redact_log_hides_every_url_form(text):
    got = ytdlp.redact_log(text)
    assert "SECRET" not in got and "REDACTED" in got


def test_redact_log_keeps_a_double_slash_that_is_not_a_url():
    assert ytdlp.redact_log("a // b and path a//b") == "a // b and path a//b"


def test_read_tail_drops_the_first_line_when_the_file_was_cut(tmp_path):
    path = tmp_path / "stderr.bin"
    url_line = b"ERROR: https://r.example.com/videoplayback?sig=SIGSECRET123&expire=1\n"
    path.write_bytes(url_line + b"x" * (ytdlp.MAX_LOG - 30) + b"\nlast line\n")  # the cut lands inside the URL
    got = ytdlp.redact_log(ytdlp.read_tail(str(path)))
    assert "SIGSECRET123" not in got and got.endswith("last line\n")
    short = tmp_path / "short.bin"
    short.write_bytes(b"one\ntwo\n")
    assert ytdlp.read_tail(str(short)) == "one\ntwo\n"
    one_line = tmp_path / "one.bin"
    one_line.write_bytes(b"y" * (ytdlp.MAX_LOG + 5))
    assert ytdlp.read_tail(str(one_line)) == ""


def test_tree_usage_counts_the_whole_item_folder_and_follows_no_junction(tmp_path):
    dirs = ytdlp.make_item_dirs(str(tmp_path), "0001", None)
    pathlib.Path(dirs.dl, "media.mp4").write_bytes(b"a" * 10)
    os.mkdir(os.path.join(dirs.cache, "sub"))
    pathlib.Path(dirs.cache, "sub", "c").write_bytes(b"b" * 7)
    pathlib.Path(dirs.home, "h").write_bytes(b"c" * 3)
    pathlib.Path(dirs.io, "stderr.bin").write_bytes(b"d" * 5)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"z" * 1000)
    link = os.path.join(dirs.cache, "link")
    done = subprocess.run(["cmd", "/c", "mklink", "/J", link, str(outside)], capture_output=True)
    assert done.returncode == 0
    try:
        assert ytdlp.tree_usage(dirs.root) == (25, 5)
        assert ytdlp.tree_usage(os.path.join(str(tmp_path), "missing")) == (0, 0)
    finally:
        os.rmdir(link)  # a link made with `mklink` is not the fixture's (FR16.5): the link only, never `outside`


def test_size_watch_counts_what_is_written_outside_dl(tmp_path):
    outcome, _ = media(tmp_path, "sleep_then_ok", tree_usage=lambda root: (11, 0), room=10, scan=lambda dl: [])
    assert (outcome.status, outcome.code) == ("failed", "OUTPUT_WRITE_FAILED")


def test_size_watch_caps_the_engine_output_files(tmp_path):
    outcome, _ = media(tmp_path, "sleep_then_ok", tree_usage=lambda root: (0, ytdlp.IO_CAP + 1))
    assert (outcome.status, outcome.code, outcome.retryable) == ("failed", "SIZE_LIMIT", False)
