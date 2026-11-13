"""A stand-in for `python -m yt_dlp` used by the media tests (SPR-03). Run as:

    python -P fake_ytdlp.py <scenario> <the fixed flags...> -- <url>

The scenario is the first argument (the tests put it in the injected engine prefix); the real flags follow
and are read only for `--paths home:<dl>` (the item folders are `<item>/dl`, `<item>/io`). No network."""

import json
import os
import subprocess
import sys
import time

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
WEBM = b"\x1a\x45\xdf\xa3\x42\x82\x84webm" + b"\x00" * 64
OK_META = {"title": "Me at the zoo", "uploader": "jawed", "duration": 19, "extractor_key": "Youtube"}


def _dl(argv):
    for i, item in enumerate(argv):
        if item == "--paths" and i + 1 < len(argv) and argv[i + 1].startswith("home:"):
            return argv[i + 1][5:]
    raise SystemExit("no --paths home:")


def _write(dl, name, data):
    with open(os.path.join(dl, name), "wb") as handle:
        handle.write(data)


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _err(text):
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _tree(level, io):
    """tree3: this process, a child and a grandchild all sleep; every PID goes into <io>/pids.txt."""
    with open(os.path.join(io, "pids.txt"), "a", encoding="ascii") as handle:
        handle.write("%d\n" % os.getpid())
    if level < 3:
        subprocess.Popen([sys.executable, "-P", os.path.abspath(__file__), "tree-child", str(level + 1), io])
    time.sleep(120)


def main(argv):
    scenario = argv[1]
    if scenario == "tree-child":
        _tree(int(argv[2]), argv[3])
        return 0
    dl = _dl(argv)
    io = os.path.join(os.path.dirname(dl), "io")
    if scenario == "ok_mp4":
        _write(dl, "media.mp4", MP4)
        _emit(OK_META)
    elif scenario == "ok_two_files":
        _write(dl, "media.mp4", MP4)
        _write(dl, "media.webm", WEBM)
        _emit(OK_META)
    elif scenario == "fail_429":
        _err("ERROR: [youtube] x: HTTP Error 429: Too Many Requests")
        return 1
    elif scenario == "fail_403":
        _err("ERROR: [youtube] x: unable to download video data: HTTP Error 403: Forbidden")
        return 1
    elif scenario == "fail_dns":
        _err("ERROR: [youtube] x: Unable to download webpage: getaddrinfo failed")
        return 1
    elif scenario == "fail_mixed":
        _err("ERROR: [youtube] x: HTTP Error 403: Forbidden")
        _err("ERROR: [youtube] x: HTTP Error 429: Too Many Requests")
        return 1
    elif scenario == "title_marker":
        _err("WARNING: [youtube] sign in to confirm (only a warning)")
        _err("ERROR: [youtube] boom")
        _emit({"title": "HTTP Error 429 sign in to confirm"})
        return 1
    elif scenario == "big_stream":
        deadline = time.monotonic() + 60
        with open(os.path.join(dl, "media.mp4"), "wb") as handle:
            while time.monotonic() < deadline:
                handle.write(b"\x00" * (1 << 20))
                handle.flush()
                time.sleep(0.1)
    elif scenario == "zero_files_big":
        _emit({"title": "big", "filesize_approx": 10 ** 12})
    elif scenario == "zero_files":
        _emit({"title": "nothing"})
    elif scenario == "tree3":
        _tree(1, io)
    elif scenario == "print_env":
        _write(dl, "media.mp4", MP4)
        _emit({"keys": sorted(os.environ), "TEMP": os.environ.get("TEMP")})
    elif scenario == "json_bad":
        _write(dl, "media.mp4", MP4)
        sys.stdout.write("this is not json\n")
    elif scenario == "json_evil":
        _write(dl, "media.mp4", MP4)
        _emit({"title": "a\x1b[31m‮b\n" + "x" * 500, "uploader": "u\r\nv", "duration": 19, "extractor_key": "Youtube"})
    elif scenario == "stderr_secret":
        _err("ERROR: [youtube] GET https://r.example.com/videoplayback/sig/SECRETPATH/x?sig=SECRETVALUE tok123")
        return 1
    else:
        raise SystemExit("unknown scenario")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
