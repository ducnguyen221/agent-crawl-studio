"""FR1.8, RF43: the real command line in a child process. Library warnings, log records and stray prints
must never reach stdout, even with `-W always` and a logging handler that writes to stdout."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

NOISY = r"""
import sys, logging, warnings
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
import agent_crawl_studio.schema as schema
original = schema.parse_request
def noisy(raw):
    warnings.warn("library warning text")
    logging.getLogger("lib").warning("library log text")
    print("stray print text")
    sys.stdout.write("raw write text\n")
    return original(raw)
schema.parse_request = noisy
import agent_crawl_studio.__main__ as entry
sys.argv = ["crawl-studio", "plan", "--request", sys.argv[1]]
entry.run()
"""


def run_child(request_bytes, tmp_path):
    request = tmp_path / "r.json"
    request.write_bytes(request_bytes)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1", CRAWL_STUDIO_WORK=str(tmp_path / "work"))
    return subprocess.run([sys.executable, "-X", "utf8", "-W", "always", "-c", NOISY, str(request)],
                          capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)


def test_noise_from_libraries_never_reaches_stdout(tmp_path):
    done = run_child(b'{"schema_version": 2, "urls": ["https://example.com/"]}', tmp_path)
    assert done.stdout.count("\n") == 1, done.stdout
    assert json.loads(done.stdout)["error"]["code"] == "INVALID_REQUEST" and done.returncode == 2
    for text in ("library warning text", "library log text", "stray print text", "raw write text"):
        assert text not in done.stdout
    assert not (tmp_path / "work").exists()


def test_a_request_that_makes_the_library_raise_still_gives_one_line(tmp_path):
    done = run_child(b"{", tmp_path)
    assert done.stdout.count("\n") == 1 and json.loads(done.stdout)["error"]["code"] == "INVALID_REQUEST"
