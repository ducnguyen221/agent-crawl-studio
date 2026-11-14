"""Writes the four golden samples (plan-auto, plan-direct, result-auto, result-direct) for SPR-04 PLAN TK1.

The samples are made by the code as it stands at the SPR-03 end (commit f396845), before any SPR-04 change, with the
same loopback fixture server and injected policy that `tests/harness_fixture_run.py` uses (loopback is only ever
allowed by a policy passed as a parameter of the code). One batch of two URLs per mode: an HTML page and a binary
file. The output is normalised (keys listed in README.md) so two runs on any work folder give identical bytes.

Usage: <python> -X utf8 tests/fixtures/golden/gen_golden.py --work <empty dir> --out <dir>   (PYTHONPATH not needed)"""

import argparse
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (str(ROOT / "tests"), str(ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from fixtures.httpd import LocalServer  # noqa: E402
from agent_crawl_studio import runner, schema  # noqa: E402
from agent_crawl_studio.urlpolicy import Policy  # noqa: E402

LOOP = "127.0.0.1"
TIME_KEYS = ("started_at", "finished_at")
HASH_KEYS = ("url_sha256",)


def build(mode, server):
    return schema.parse_request(json.dumps({"schema_version": 1, "mode": mode, "urls": [
        server.url("/html?size=3000"), server.url("/file?size=2000")]}).encode("utf-8"))


def normalise(doc, port, work, job_id):
    """Replace time fields, the job id, the fixture port, url hashes and absolute work paths by constants."""
    forms = {work, work.replace("\\", "/"), work.replace("/", "\\"), os.path.normpath(work),
             os.path.normpath(work).replace("\\", "/")}
    forms = sorted(forms, key=len, reverse=True)

    def text(value):
        for form in forms:
            value = value.replace(form, "<WORK>")
        if job_id:
            value = value.replace(job_id, "<JOB_ID>")
        return value.replace(":%d" % port, ":<PORT>")

    def walk(node, key=None):
        if isinstance(node, dict):
            return {name: walk(value, name) for name, value in node.items()}
        if isinstance(node, list):
            return [walk(value, key) for value in node]
        if isinstance(node, str):
            if key in TIME_KEYS:
                return "<TIMESTAMP>"
            if key in HASH_KEYS:
                return "<URL_SHA256>"
            return text(node)
        return node

    return walk(doc)


def main(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--work", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, CRAWL_STUDIO_WORK=args.work)
    with LocalServer() as server:
        policy = Policy(
            ports=(server.port,), ip_ok=lambda address: address == LOOP,
            resolver=lambda host, port, proto=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (LOOP, port))])
        for mode in ("auto", "direct"):
            request = build(mode, server)
            planned = runner.plan(request, env, {"policy": policy})
            result, code = runner.run(request, env, {"policy": policy})
            if code != 0:
                sys.stderr.write("run %s exited %d\n" % (mode, code))
                return 1
            for kind, doc in (("plan", planned), ("result", result)):
                clean = normalise(doc, server.port, args.work, result.get("job_id"))
                blob = json.dumps(clean, indent=1, ensure_ascii=True) + "\n"
                if str(server.port) in blob or "Users" in blob or "OpcOS" in blob:
                    sys.stderr.write("unnormalised value left in %s-%s\n" % (kind, mode))
                    return 1
                (out / ("%s-%s.json" % (kind, mode))).write_text(blob, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
