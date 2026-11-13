"""SC7 harness: run a real two-item batch against the loopback fixture server with an injected policy (the only way
loopback is ever allowed: policy is a parameter of the code, never an option), using whatever Python runs this
script, for instance the core venv Python the launcher uses (it has no pytest; this script needs none).

Usage: <python> tests/harness_fixture_run.py --work <dir>      (PYTHONPATH must include the repo's src folder)
stdout is ONE JSON line: the result of the run; the exit code is the one of the job (0 when it succeeded)."""

import argparse
import json
import os
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for path in (str(HERE), str(HERE.parent / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from fixtures.httpd import LocalServer  # noqa: E402
from agent_crawl_studio import runner, schema  # noqa: E402
from agent_crawl_studio.urlpolicy import Policy  # noqa: E402

LOOP = "127.0.0.1"


def main(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--work", required=True)
    parser.add_argument("--mode", default="auto", choices=("auto", "extract"))
    parser.add_argument("--page", default="blog.html")  # the page of tests/fixtures/extract that `--mode extract` reads
    args = parser.parse_args(argv)
    env = dict(os.environ, CRAWL_STUDIO_WORK=args.work)
    with LocalServer() as server:
        policy = Policy(
            ports=(server.port,), ip_ok=lambda address: address == LOOP,
            resolver=lambda host, port, proto=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (LOOP, port))])
        body = {"schema_version": 1, "urls": [server.url("/file?size=2000"), server.url("/report.pdf?size=3000")]}
        if args.mode == "extract":
            body = {"schema_version": 1, "mode": "extract", "urls": [server.url("/page/" + args.page)]}
        request = schema.parse_request(json.dumps(body).encode("utf-8"))
        result, code = runner.run(request, env, {"policy": policy})
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
