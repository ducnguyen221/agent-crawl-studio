#!/usr/bin/env python3
"""Benchmark of the page-text extraction engines (SPR-04 TK3, SPEC FR27).

Three engines, one process each, no network:
  trafilatura  Python library, run with the Python that has it installed (a throw-away venv under <OpcOS>\\.tmp\\)
  defuddle     Node library, run with `node --require <no-network preload>` on a tiny .mjs that reads the HTML from disk
  baseline     the floor: every text node outside <script>/<style>, written with the standard-library html.parser

Usage (one engine per call; the report mode merges the three result files):
  python scripts/bench_extract.py --fixtures tests/fixtures/extract --engine trafilatura --out out/t.json
  python scripts/bench_extract.py --fixtures tests/fixtures/extract --engine defuddle --node-dir <...>/node_modules --out out/d.json
  python scripts/bench_extract.py --fixtures tests/fixtures/extract --engine baseline --out out/b.json
  python scripts/bench_extract.py --report out/t.json out/d.json out/b.json

Every page is given to an engine twice: the original HTML ("raw") and the HTML after the hidden-part removal of
SPEC FR20.2 ("stripped", written by this script; the real removal is the extract worker's job). Every engine's output goes
through the SAME normalize() before words are split, so a Markdown link or a list marker costs nothing.
Score = harmonic mean of word precision and recall, counted as multisets. The fixtures are decoded with the charset in the
manifest (this plays the role of SPEC FR20.1) and handed to the engines as text.
`--make-large PATH` writes the big page (about 4 MiB, deterministic) that the manifest entry with "generate": "large" asks for.
"""
import argparse
import hashlib
import html as htmllib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from html.parser import HTMLParser

MiB = 1024 * 1024
EXTRACT_URL = "https://example.test/"
# --------------------------------------------------------------------------------------------- normalisation, scoring

_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_LISTMARK = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+", re.M)
_DROPPED = re.compile(r"[*_`~\\]")
_SPACED = re.compile(r"[|>#]")
_RULE = re.compile(r"^[-=:+]+$")


def normalize(text):
    """Markdown/text -> list of comparable words. Same function for gold and for every engine."""
    text = unicodedata.normalize("NFC", text or "").lower()
    text = _IMG.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _LISTMARK.sub("", text)
    text = _DROPPED.sub("", text)
    text = _SPACED.sub(" ", text)
    return [w for w in text.split() if not _RULE.match(w)]


def phrase(text):
    return " ".join(normalize(text))


def f1(out_words, gold_words):
    if not gold_words:
        return None, None, None
    common = sum((Counter(out_words) & Counter(gold_words)).values())
    if not out_words or not common:
        return 0.0, 0.0, 0.0
    p, r = common / len(out_words), common / len(gold_words)
    return 2 * p * r / (p + r), p, r


# ------------------------------------------------------------------------------- FR20.2 hidden-part removal (bench copy)

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_DROP_TAGS = {"script", "style", "noscript", "template", "iframe", "object", "embed", "form", "svg"}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![\d.])|opacity\s*:\s*0(?![\d.])", re.I)


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out, self.stack, self.skip = [], [], 0   # stack of (tag, was_skipped_root)

    @staticmethod
    def _hidden(tag, attrs):
        if tag in _DROP_TAGS:
            return True
        for k, v in attrs:
            if k == "hidden" or (k == "aria-hidden" and (v or "").strip().lower() == "true"):
                return True
            if k == "style" and v and _HIDDEN_STYLE.search(v):
                return True
        return False

    def handle_starttag(self, tag, attrs):
        void = tag in _VOID
        if self.skip:
            if not void:
                self.stack.append((tag, False))
            return
        if self._hidden(tag, attrs):
            if not void:
                self.stack.append((tag, True))
                self.skip += 1
            return
        self.out.append(self.get_starttag_text())
        if not void:
            self.stack.append((tag, False))

    def handle_startendtag(self, tag, attrs):
        if not self.skip and not self._hidden(tag, attrs):
            self.out.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        names = [t for t, _ in self.stack]
        if tag not in names:
            return
        was_skipping = self.skip > 0
        while self.stack:
            top, root = self.stack.pop()
            if root:
                self.skip -= 1
            if top == tag:
                break
        if not was_skipping:
            self.out.append("</%s>" % tag)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)

    def handle_entityref(self, name):
        if not self.skip:
            self.out.append("&%s;" % name)

    def handle_charref(self, name):
        if not self.skip:
            self.out.append("&#%s;" % name)

    def handle_decl(self, decl):
        if not self.skip:
            self.out.append("<!%s>" % decl)

    # comments and processing instructions are dropped (handle_comment / handle_pi not overridden to emit)


def strip_hidden(text):
    p = _Stripper()
    p.feed(text)
    p.close()
    return "".join(p.out)


# ------------------------------------------------------------------------------------------------ the floor (baseline)

_BLOCK = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "section", "article",
          "header", "footer", "nav", "aside", "pre", "blockquote", "title", "body", "html", "head", "hr", "main", "figure", "figcaption"}


class _Floor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def floor_extract(text):
    p = _Floor()
    p.feed(text)
    p.close()
    return "".join(p.parts)


# ----------------------------------------------------------------------------------------- deterministic article builder

_VOCAB = ("river stone garden window paper table morning bridge market lantern copper ladder harbor meadow thread cabinet "
          "orchard compass blanket kettle ribbon basket pebble canvas anchor feather mirror quilt candle timber valley "
          "puzzle engine pillow shelf whistle button carpet drawer fabric hammer jacket marble needle pencil ribbon saddle "
          "ticket violin wallet barrel bottle cradle dinner estate fabric galley harvest island journal kitchen lesson "
          "machine napkin office parcel quarry record season tunnel umbrella village weather yellow zipper arrange borrow "
          "carry deliver explain follow gather handle insert listen measure notice observe prepare quietly repair settle "
          "travel upgrade visit wander gentle simple careful steady narrow broad ancient modern plain bright hollow "
          "tidy rough smooth gentle distant nearby early late often rarely always never during after before between "
          "across behind beside beyond toward without within around against along throughout").split()


def _sentence(rng, lo=7, hi=17):
    n = rng.randint(lo, hi)
    words = [rng.choice(_VOCAB) for _ in range(n)]
    words[0] = words[0].capitalize()
    return " ".join(words) + "."


def _esc(s):
    return htmllib.escape(s, quote=False)


def make_article(seed, target_bytes, rich):
    """Return (html, gold_text). rich=True adds lists, tables, code, quotes (the big page); False is prose with h2 and lists."""
    rng = random.Random(seed)
    body, gold, size, sec = [], [], 0, 0
    while size < target_bytes:
        sec += 1
        title = " ".join(rng.choice(_VOCAB) for _ in range(rng.randint(2, 5))).capitalize() + " %d" % sec
        chunk = ["<h2 id=\"s%d\">%s</h2>\n" % (sec, _esc(title))]
        gold.append(title)
        for _ in range(rng.randint(3, 6)):
            sents = [_sentence(rng) for _ in range(rng.randint(3, 6))]
            gold.append(" ".join(sents))
            marked = list(sents)
            ws = marked[0].split(" ")
            if len(ws) > 4:
                ws[2] = '<a href="https://example.test/ref/%d">%s</a>' % (rng.randint(1, 99999), ws[2])
                ws[3] = "<em>%s</em>" % ws[3]
                marked[0] = " ".join(ws)
            if len(marked) > 1:
                ws = marked[1].split(" ")
                ws[1] = "<strong>%s</strong>" % ws[1]
                marked[1] = " ".join(ws)
            chunk.append("<p>%s</p>\n" % " ".join(marked))
            if rng.random() < 0.3:
                items = [_sentence(rng, 3, 9) for _ in range(rng.randint(3, 7))]
                gold.extend(items)
                chunk.append("<ul>\n%s</ul>\n" % "".join("<li>%s</li>\n" % i for i in items))
            if rich and rng.random() < 0.25:
                q = _sentence(rng, 10, 20)
                gold.append(q)
                chunk.append("<blockquote><p>%s</p></blockquote>\n" % q)
            if rich and rng.random() < 0.2:
                head = ["col%s" % rng.choice(_VOCAB) for _ in range(4)]
                rows = [[rng.choice(_VOCAB) + str(rng.randint(10, 999)) for _ in range(4)] for _ in range(rng.randint(3, 8))]
                gold.append(" ".join(head))
                gold.extend(" ".join(r) for r in rows)
                chunk.append("<table><thead><tr>%s</tr></thead><tbody>\n%s</tbody></table>\n" % (
                    "".join("<th>%s</th>" % h for h in head),
                    "".join("<tr>%s</tr>\n" % "".join("<td>%s</td>" % c for c in r) for r in rows)))
            if rich and rng.random() < 0.15:
                lines = ["%s%d = compute(%s%d, %d)" % (rng.choice(_VOCAB), i, rng.choice(_VOCAB), i, rng.randint(1, 99))
                         for i in range(rng.randint(3, 7))]
                gold.extend(lines)
                chunk.append("<pre><code>%s</code></pre>\n" % "\n".join(_esc(l) for l in lines))
        text = "".join(chunk)
        body.append(text)
        size += len(text.encode("utf-8"))
    return "".join(body), "\n\n".join(gold) + "\n"


def make_large():
    """The big page of the set: ~4.2 MiB of valid HTML with a header, nav, sidebar, article body of mixed structure, footer."""
    art, _ = make_article(20261113, 4 * MiB + 200 * 1024, True)
    nav = "".join('<li><a href="/p/%d">Section %d</a></li>' % (i, i) for i in range(40))
    side = "".join("<li><a href=\"/t/%d\">Topic %d</a></li>" % (i, i) for i in range(30))
    return ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><title>Large synthetic page</title>"
            "<meta name=\"author\" content=\"Fixture Author\"></head><body>\n"
            "<header><nav><ul>%s</ul></nav></header>\n<aside class=\"sidebar\"><ul>%s</ul></aside>\n"
            "<main><article><h1>Large synthetic page</h1>\n%s</article></main>\n"
            "<footer><p>Footer links and notices.</p></footer></body></html>\n") % (nav, side, art)


# ------------------------------------------------------------------------------------------------------- Job Object runner

NONET_PRELOAD = r"""const net = require('node:net'), dns = require('node:dns'), http = require('node:http'), https = require('node:https'), tls = require('node:tls'), dgram = require('node:dgram');
const hits = [];
const trip = (n) => (...a) => { hits.push(n); throw new Error('NONET:' + n); };
net.Socket.prototype.connect = trip('net.Socket.connect');
net.connect = trip('net.connect'); net.createConnection = trip('net.createConnection');
tls.connect = trip('tls.connect'); dgram.createSocket = trip('dgram.createSocket');
http.request = trip('http.request'); https.request = trip('https.request');
dns.lookup = trip('dns.lookup'); dns.promises.lookup = trip('dns.promises.lookup');
globalThis.fetch = (...a) => { hits.push('fetch'); return Promise.reject(new Error('NONET:fetch')); };
process.on('exit', () => { console.error('NONET_HITS=' + JSON.stringify(hits)); });
"""

DEFUDDLE_MJS = r"""import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const [, , htmlPath, textPath, entry] = process.argv;
const { Defuddle } = await import(pathToFileURL(entry).href);
const html = fs.readFileSync(htmlPath, 'utf8');
const t0 = performance.now();
let r;
try {
  r = await Defuddle(html, 'https://example.test/', { useAsync: false, markdown: true, removeImages: true,
    fetch: async () => { throw new Error('blocked'); } });
} catch (e) { console.log(JSON.stringify({ error: String(e).slice(0, 300) })); process.exit(3); }
const secs = (performance.now() - t0) / 1000;
fs.writeFileSync(textPath, r.content || '', 'utf8');
console.log(JSON.stringify({ secs, none: !r.content, chars: (r.content || '').length }));
"""


def run_in_job(cmd, timeout):
    """Run cmd suspended -> assign to a Job Object -> resume (as TK1/TK2 did); return exit, stdout, stderr, peak MiB, wall s, timed_out."""
    t0 = time.perf_counter()
    if os.name != "nt":
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
            return p.returncode, p.stdout, p.stderr, None, time.perf_counter() - t0, False
        except subprocess.TimeoutExpired:
            return None, b"", b"", None, time.perf_counter() - t0, True
    import ctypes
    import ctypes.wintypes as wt
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")
    k.CreateJobObjectW.restype = wt.HANDLE
    k.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    k.OpenProcess.restype = wt.HANDLE
    k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    k.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
    k.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    k.CloseHandle.argtypes = [wt.HANDLE]
    nt.NtResumeProcess.argtypes = [wt.HANDLE]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in "abcdef"]

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wt.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wt.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD),
                    ("SchedulingClass", wt.DWORD)]

    class Ext(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    job = k.CreateJobObjectW(None, None)
    e = Ext()
    e.Basic.LimitFlags = 0x2000   # kill on close
    if not k.SetInformationJobObject(job, 9, ctypes.byref(e), ctypes.sizeof(e)):
        raise OSError("SetInformationJobObject failed")
    p = subprocess.Popen(cmd, creationflags=0x4 | 0x8000000, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    h = k.OpenProcess(0x1F0FFF, False, p.pid)
    if not k.AssignProcessToJobObject(job, h):
        p.kill()
        raise OSError("AssignProcessToJobObject failed %d" % ctypes.get_last_error())
    nt.NtResumeProcess(h)
    timed_out = False
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        k.TerminateJobObject(job, 99)
        out, err = p.communicate()
    wall = time.perf_counter() - t0
    q = Ext()
    k.QueryInformationJobObject(job, 9, ctypes.byref(q), ctypes.sizeof(q), None)
    k.CloseHandle(h)
    k.CloseHandle(job)
    return p.returncode, out, err, q.PeakJobMemoryUsed / MiB, wall, timed_out


# ------------------------------------------------------------------------------------------------------------- the workers

def worker_python(engine, html_path, text_path):
    """Runs in its own process. Patches the network functions (after importing the engine: patching socket.socket itself breaks ssl)."""
    import socket
    hits = []

    def boom(*a, **k):
        hits.append(1)
        raise RuntimeError("network used")

    with open(html_path, "r", encoding="utf-8") as fh:
        data = fh.read()
    try:
        if engine == "trafilatura":
            import trafilatura
        socket.socket.connect = boom
        socket.socket.connect_ex = boom
        socket.create_connection = boom
        socket.getaddrinfo = boom
        t0 = time.perf_counter()
        if engine == "trafilatura":
            out = trafilatura.extract(data, url=EXTRACT_URL, include_links=False, include_images=False, include_comments=False)
        else:
            out = floor_extract(data)
        secs = time.perf_counter() - t0
    except Exception as exc:   # report, do not hide
        print(json.dumps({"error": "%s: %s" % (type(exc).__name__, str(exc)[:200]), "net_hits": len(hits)}))
        return 3
    with open(text_path, "w", encoding="utf-8") as fh:
        fh.write(out or "")
    print(json.dumps({"secs": secs, "none": out is None, "chars": len(out or ""), "net_hits": len(hits)}))
    return 0


# ---------------------------------------------------------------------------------------------------------------- the driver

def engine_version(engine, node_dir):
    if engine == "trafilatura":
        out = subprocess.run([sys.executable, "-I", "-c", "import trafilatura;print(trafilatura.__version__)"],
                             capture_output=True, text=True).stdout.strip()
        return "trafilatura " + (out or "UNKNOWN")
    if engine == "defuddle":
        with open(os.path.join(node_dir, "defuddle", "package.json"), encoding="utf-8") as fh:
            v = json.load(fh)["version"]
        node = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip()
        return "defuddle %s (node %s)" % (v, node)
    return "baseline html.parser (Python %s)" % sys.version.split()[0]


def parse_json_line(raw):
    for line in reversed(raw.decode("utf-8", "replace").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                pass
    return {}


def run_engine_once(engine, html_path, work, node_dir, timeout, tag):
    text_path = os.path.join(work, "out-%s.txt" % tag)
    if engine == "defuddle":
        entry = os.path.join(node_dir, "defuddle", "dist", "node.js")
        cmd = ["node", "--require", os.path.join(work, "nonet-preload.cjs"), os.path.join(work, "run-defuddle.mjs"),
               html_path, text_path, entry]
    else:
        cmd = [sys.executable, "-I", "-X", "utf8", os.path.abspath(__file__), "--worker", engine, "--html", html_path, "--text-out", text_path]
    code, out, err, peak, wall, timed_out = run_in_job(cmd, timeout)
    info = parse_json_line(out)
    res = {"wall_ms": round(wall * 1000), "peak_mib": None if peak is None else round(peak, 1), "ms": None, "error": None,
           "none": bool(info.get("none")), "net_hits": info.get("net_hits", 0), "text": ""}
    if engine == "defuddle":
        m = re.search(rb"NONET_HITS=(\[.*?\])", err)
        res["net_hits"] = len(json.loads(m.group(1))) if m else None   # None = the preload did not report
    if info.get("secs") is not None:
        res["ms"] = round(info["secs"] * 1000)
    if timed_out:
        res["error"] = "timeout after %ds" % timeout
    elif code != 0 or info.get("error"):
        res["error"] = info.get("error") or ("exit %s: %s" % (code, err.decode("utf-8", "replace").strip().splitlines()[-1:] or ""))
    elif os.path.exists(text_path):
        with open(text_path, "r", encoding="utf-8") as fh:
            res["text"] = fh.read()
    if os.path.exists(text_path):
        os.remove(text_path)
    return res


def decode_fixture(path, charset):
    with open(path, "rb") as fh:
        return fh.read().decode(charset, errors="replace")


def drive(args):
    with open(os.path.join(args.fixtures, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    work = tempfile.mkdtemp(prefix="bench-", dir=args.workdir)
    made = []

    def put(name, content):
        path = os.path.join(work, name)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        made.append(path)
        return path

    put("nonet-preload.cjs", NONET_PRELOAD)
    put("run-defuddle.mjs", DEFUDDLE_MJS)
    result = {"engine": args.engine, "version": engine_version(args.engine, args.node_dir), "pages": []}
    try:
        for ent in manifest:
            name = ent["file"]
            if ent.get("generate") == "large":
                raw = make_large()
                result.setdefault("generated", {})[name] = {"bytes": len(raw.encode("utf-8")),
                                                            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}
            else:
                raw = decode_fixture(os.path.join(args.fixtures, name), ent["charset"])
            gold = []
            if ent.get("gold"):
                with open(os.path.join(args.fixtures, ent["gold"]), "r", encoding="utf-8") as fh:
                    gold = normalize(fh.read())
            for variant, content in (("raw", raw), ("stripped", strip_hidden(raw))):
                path = put("in-%s.html" % variant, content)
                res = run_engine_once(args.engine, path, work, args.node_dir, args.timeout, variant)
                words = normalize(res.pop("text"))
                flat = " ".join(words)
                sc, pr, rc = f1(words, gold)
                row = {"file": name, "input": variant, "expect": ent["expect"], "html_bytes": len(content.encode("utf-8")),
                       "words": len(words), "score": sc, "precision": pr, "recall": rc,
                       "must_absent_found": [m for m in ent.get("must_absent", []) if phrase(m) in flat],
                       "hidden_found": [m for m in ent.get("hidden", []) if phrase(m) in flat],
                       "no_text_ok": (len(words) == 0) if ent["expect"] == "no_text" else None}
                row.update(res)
                result["pages"].append(row)
                os.remove(path)
                made.remove(path)
                print("%-22s %-8s score=%s ms=%s peak=%s err=%s" % (name, variant, None if sc is None else round(sc, 3),
                                                                  res["ms"], res["peak_mib"], res["error"]), file=sys.stderr)
    finally:
        for path in made:
            if os.path.exists(path):
                os.remove(path)
        os.rmdir(work)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    return 0


# ---------------------------------------------------------------------------------------------------------------- report

def fmt(x, nd=3):
    return "-" if x is None else ("%.*f" % (nd, x))


def report(paths):
    data = {}
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        data[d["engine"]] = d
    order = [e for e in ("trafilatura", "defuddle", "baseline") if e in data]
    idx = {e: {(r["file"], r["input"]): r for r in data[e]["pages"]} for e in order}
    files = []
    for r in data[order[0]]["pages"]:
        if r["file"] not in files:
            files.append(r["file"])
    scored = [f for f in files if idx[order[0]][(f, "stripped")]["score"] is not None]
    print("Versions: " + "; ".join(data[e]["version"] for e in order) + "\n")
    for variant in ("stripped", "raw"):
        print("### Score per page, input = %s\n" % variant)
        print("| page | " + " | ".join(order) + " |")
        print("|---|" + "---|" * len(order))
        for f in files:
            cells = []
            for e in order:
                r = idx[e][(f, variant)]
                cells.append("ERR " + r["error"][:30] if r["error"] else (fmt(r["score"]) if r["score"] is not None
                             else ("no_text ok" if r["no_text_ok"] else ("no_text FAIL (%d words)" % r["words"] if r["no_text_ok"] is False else "-"))))
            print("| %s | %s |" % (f, " | ".join(cells)))
        mean = {e: sum(idx[e][(f, variant)]["score"] or 0 for f in scored) / len(scored) for e in order}
        print("| **mean of %d pages** | %s |\n" % (len(scored), " | ".join("**%s**" % fmt(mean[e]) for e in order)))
    print("### Wins (score higher by more than 0.03, input = stripped, %d pages with text)\n" % len(scored))
    print("| A | B | A wins | B wins | ties |\n|---|---|---|---|---|")
    for i, a in enumerate(order):
        for b in order[i + 1:]:
            wa = sum(idx[a][(f, "stripped")]["score"] - idx[b][(f, "stripped")]["score"] > 0.03 for f in scored)
            wb = sum(idx[b][(f, "stripped")]["score"] - idx[a][(f, "stripped")]["score"] > 0.03 for f in scored)
            print("| %s | %s | %d | %d | %d |" % (a, b, wa, wb, len(scored) - wa - wb))
    print("\n### Junk, hidden strings, errors, no-network\n")
    print("| engine | input | must_absent found | hidden found | no_text failures | errors | min page score | net hits |\n|---|---|---|---|---|---|---|---|")
    for e in order:
        for variant in ("raw", "stripped"):
            rows = [r for r in data[e]["pages"] if r["input"] == variant]
            mins = min((r["score"] for r in rows if r["score"] is not None), default=None)
            hits = [r["net_hits"] for r in rows]
            print("| %s | %s | %d | %d | %d | %d | %s | %s |" % (
                e, variant, sum(len(r["must_absent_found"]) for r in rows), sum(len(r["hidden_found"]) for r in rows),
                sum(r["no_text_ok"] is False for r in rows), sum(bool(r["error"]) for r in rows), fmt(mins),
                "unreported" if any(h is None for h in hits) else sum(hits)))
    print("\n### Per page detail (input = stripped): precision / recall / junk / hidden / ms / peak MiB\n")
    print("| page | engine | precision | recall | must_absent found | hidden found | ms | peak MiB | error |\n|---|---|---|---|---|---|---|---|---|")
    for f in files:
        for e in order:
            r = idx[e][(f, "stripped")]
            print("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (f, e, fmt(r["precision"]), fmt(r["recall"]),
                  ", ".join(r["must_absent_found"]) or "0", ", ".join(r["hidden_found"]) or "0", r["ms"], r["peak_mib"], r["error"] or "-"))
    print("\n### Hidden strings still present, raw vs stripped input\n")
    print("| page | engine | raw | stripped |\n|---|---|---|---|")
    for f in files:
        for e in order:
            a, b = idx[e][(f, "raw")], idx[e][(f, "stripped")]
            if a["hidden_found"] or b["hidden_found"]:
                print("| %s | %s | %d | %d |" % (f, e, len(a["hidden_found"]), len(b["hidden_found"])))
    print("\n### Largest page (memory and time), input = raw and stripped\n")
    big = max(files, key=lambda f: idx[order[0]][(f, "raw")]["html_bytes"])
    print("| page | engine | input | HTML bytes | ms in engine | wall ms | peak job MiB | error |\n|---|---|---|---|---|---|---|---|")
    for e in order:
        for variant in ("raw", "stripped"):
            r = idx[e][(big, variant)]
            print("| %s | %s | %s | %d | %s | %d | %s | %s |" % (big, e, variant, r["html_bytes"], r["ms"], r["wall_ms"], r["peak_mib"], r["error"] or "-"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures")
    ap.add_argument("--engine", choices=("trafilatura", "defuddle", "baseline"))
    ap.add_argument("--node-dir", help="the node_modules folder that holds defuddle")
    ap.add_argument("--out")
    ap.add_argument("--workdir", default=None, help="folder for the temporary files (default: the system temp folder)")
    ap.add_argument("--timeout", type=int, default=300, help="seconds allowed to one engine run on one page")
    ap.add_argument("--make-large", metavar="PATH")
    ap.add_argument("--report", nargs="+", metavar="JSON")
    ap.add_argument("--worker", choices=("trafilatura", "baseline"), help=argparse.SUPPRESS)
    ap.add_argument("--html", help=argparse.SUPPRESS)
    ap.add_argument("--text-out", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.worker:
        return worker_python(a.worker, a.html, a.text_out)
    if a.make_large:
        with open(a.make_large, "w", encoding="utf-8", newline="") as fh:
            fh.write(make_large())
        return 0
    if a.report:
        return report(a.report)
    if not (a.fixtures and a.engine and a.out) or (a.engine == "defuddle" and not a.node_dir):
        ap.error("--fixtures, --engine and --out are required (and --node-dir for defuddle)")
    return drive(a)


if __name__ == "__main__":
    sys.exit(main())
