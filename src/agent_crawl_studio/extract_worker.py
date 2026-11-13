"""Child process of the extract mode (SPR-04 TK7, FR19.2, FR19.4, FR20.1, FR20.2, FR20.7).

Run as `python -I extract_worker.py <params.json>`, with the Python of the core venv. It reads two files (the params
file and the HTML the core downloaded) and writes ONE result file; it opens no socket and calls no download function.
It does NOT import `agent_crawl_studio`: only the standard library and the engine (trafilatura, with lxml and
charset_normalizer, which come with it). So the few things it shares with `extract.py` are copied here with a note,
and `tests/test_extract_worker.py` fails when a copy drifts.

Exit codes: 0 the result file was written; 2 bad parameters; 3 the page cannot be read or the result cannot be
written; anything else (a traceback, 1) the engine or the interpreter failed. A `MemoryError` from the engine is
NOT caught: the process dies with a non-zero code and never writes `ok: true`, because under the memory limit of the
Job Object an allocation fails instead of the process being killed and the engine may swallow the failure (the core
reads the peak memory of the job itself, FR19.4)."""

import importlib
import json
import os
import re
import sys
import types

# --- shared with extract.py (copies; the test compares them) ----------------------------------------------------
ALLOWED_CHARSETS = frozenset(
    ["utf-8", "utf-16", "utf-16le", "utf-16be", "shift_jis", "euc-jp", "iso-2022-jp", "gb18030", "gbk", "big5",
     "euc-kr", "koi8-r"]
    + ["windows-%d" % number for number in range(1250, 1259)]
    + ["iso-8859-%d" % number for number in range(1, 17)])  # no utf-7: it can hide markup from a filter
RESULT_VERSION = 1
RESULT_KEYS = ("v", "ok", "text", "meta", "charset", "guessed")
META_KEYS = ("title", "author", "date", "sitename", "lang")

_CT_CHARSET = re.compile(r"charset\s*=\s*[\"']?\s*([^\s\"';,]+)", re.IGNORECASE)
_META_TAG = re.compile(rb"<meta\b[^>]*>", re.IGNORECASE)
_META_CHARSET = re.compile(rb"charset\s*=\s*[\"']?\s*([^\s\"';/>]+)", re.IGNORECASE)
_META_WINDOW = 1024
_SNIFF = 2048  # the head `extract.is_html` looks at; the 0-byte pattern of UTF-16 is read from its first 64 bytes

BAD_PARAMS = "extract_worker: bad parameters"
IO_FAILED = "extract_worker: cannot read the page or write the result"

_TEXT_FACTOR = 2  # the text is cut at 2 x max_chars characters: the core cuts again and reads at most 8 x max_chars
_META_MAX = 1000
_FILE_BASE = 65536
_DETECT_SAMPLE = 1 << 20
_FALLBACK_CODEC = "windows-1252"

# the Cc characters except tab and line feed, and the two non-characters an XML parser refuses
_DROP = dict.fromkeys([cp for cp in list(range(0, 32)) + list(range(127, 160)) if cp not in (9, 10)]
                      + [0xFFFE, 0xFFFF])


def _allowed(name):
    name = name.strip().lower()
    return name if name in ALLOWED_CHARSETS else None


def _declared_all(head, content_type):
    """The allowed charsets the page declares, in the order FR20.1 tries them: header first, then each `<meta>` in
    the first 1 024 bytes. Each item is (name, from_meta)."""
    found, names = _CT_CHARSET.search(content_type or ""), []
    if found and _allowed(found.group(1)):
        names.append((_allowed(found.group(1)), False))
    for tag in _META_TAG.finditer(bytes(head[:_META_WINDOW])):
        found = _META_CHARSET.search(tag.group())
        if found and _allowed(found.group(1).decode("latin-1")):
            names.append((_allowed(found.group(1).decode("latin-1")), True))
    return names


def declared_charset(head, content_type):
    """The first usable declaration: same answer as `extract.declared_charset`."""
    names = _declared_all(head, content_type)
    return names[0][0] if names else None


def utf16_without_bom(data):
    """`utf-16le` or `utf-16be` when the first bytes show the 0-byte pattern of UTF-16 without a BOM, else None.
    Same rule as `extract._head_text`."""
    sample = bytes(data[:_SNIFF])[:64]
    even, odd = sample[0::2], sample[1::2]
    if len(sample) >= 4:
        if odd.count(0) * 2 >= len(odd) and even.count(0) == 0:
            return "utf-16le"
        if even.count(0) * 2 >= len(even) and odd.count(0) == 0:
            return "utf-16be"
    return None


# --- decoding (FR20.1) ------------------------------------------------------------------------------------------
def detect_charset(data, load=importlib.import_module):
    """The library's guess for the charset of `data` (a sample of at most 1 MiB), or None."""
    try:
        detector = load("charset_normalizer")
    except ImportError:
        return None
    best = detector.from_bytes(bytes(data[:_DETECT_SAMPLE])).best()
    return best.encoding if best else None


def decode_html(data, content_type, detect):
    """`(text, charset, guessed)`. Order of FR20.1: BOM, the `charset` of the header, `<meta charset>` in the first
    1 024 bytes, UTF-8, the library's guess. A name outside `ALLOWED_CHARSETS` counts as not declared; a declared
    charset the bytes contradict (strict decoding fails, or Python has no such codec) is passed over, so a page that
    lies about its charset ends at the guess. Decoding replaces what it cannot read and never raises. `guessed` is
    True only for the last step and for UTF-16 recognised without a BOM."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8", False
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace"), "utf-16", False
    for name, from_meta in _declared_all(data, content_type):
        if from_meta and name.startswith("utf-16"):
            name = "utf-8"  # what a browser does with a meta that names UTF-16: the meta itself was read as ASCII
        try:
            return data.decode(name), name, False
        except (UnicodeDecodeError, LookupError):
            continue
    wide = utf16_without_bom(data)
    if wide:
        return data.decode(wide, "replace"), wide, True
    try:
        return data.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError:
        pass
    guess = detect(data)
    try:
        return data.decode(guess or _FALLBACK_CODEC, "replace"), (guess or _FALLBACK_CODEC), True
    except LookupError:
        return data.decode(_FALLBACK_CODEC, "replace"), _FALLBACK_CODEC, True


# --- hidden parts (FR20.2) --------------------------------------------------------------------------------------
_DROP_TAGS = frozenset(["script", "style", "noscript", "template", "iframe", "object", "embed", "form", "svg"])
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:\.0*)?(?![\d.])"
    r"|opacity\s*:\s*0(?:\.0*)?(?![\d.])")
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _is_hidden(element):
    tag = element.tag
    if not isinstance(tag, str):
        return True  # a comment or a processing instruction (the tag is a function in lxml and in ElementTree)
    if tag.lower() in _DROP_TAGS:
        return True
    for key, value in element.attrib.items():
        key = key.lower()
        if key == "hidden":
            return True
        if key == "aria-hidden" and (value or "").strip().lower() == "true":
            return True
        if key == "style" and _HIDDEN_STYLE.search(_CSS_COMMENT.sub("", value or "").lower()):
            return True
    return False


def strip_hidden(root):
    """Empty every element FR20.2 names, in place; the text after it (its tail) stays. Emptying instead of
    detaching needs no parent link and no sibling search, so the cost is linear in the number of elements. Works on
    lxml and on ElementTree trees."""
    for element in [item for item in root.iter() if _is_hidden(item)]:
        tail = element.tail
        if isinstance(element.tag, str):
            element.clear()
        else:
            element.text = ""
        element.tail = tail


# --- the engine -------------------------------------------------------------------------------------------------
def engine(load=importlib.import_module):
    """The real tools: a fixed-parameter HTML parser (FR20.7), the extract call and the metadata call of
    trafilatura, and the charset detector. `load` is only there so that the test can pass stand-in modules."""
    lxml_html, etree = load("lxml.html"), load("lxml.etree")
    trafilatura, tmeta = load("trafilatura"), load("trafilatura.metadata")
    # no network, no huge-tree mode, repair broken markup, drop comments and processing instructions; the HTML
    # parser has no DTD or entity loading to switch off
    parser = lxml_html.HTMLParser(no_network=True, huge_tree=False, recover=True, remove_comments=True,
                                  remove_pis=True)

    def parse(text):
        try:
            return lxml_html.document_fromstring(text, parser=parser)
        except etree.ParserError:  # "Document is empty"
            return None

    def extract(tree):
        # include_comments defaults to True in the engine: it must be switched off
        return trafilatura.extract(tree, include_links=False, include_images=False, include_comments=False,
                                   include_tables=True, output_format="markdown")

    def metadata(tree):
        found = tmeta.extract_metadata(tree)
        if found is None:
            return None
        return {"title": found.title, "author": found.author, "date": found.date, "sitename": found.sitename,
                "lang": found.language}

    return types.SimpleNamespace(parse=parse, extract=extract, metadata=metadata, detect=detect_charset)


# --- the result file --------------------------------------------------------------------------------------------
_XML_DECLARATION = re.compile(r"\A\s*<\?xml[^>]*\?>")


def _clean(value, limit):
    if not isinstance(value, str):
        return None
    return value.translate(_DROP)[:limit]


def serialize(doc, limit):
    """The result as UTF-8 JSON written with characters (not `\\uXXXX` escapes, which would turn a CJK character into
    6 bytes and an astral one into 12), cut BY ENCODED SIZE at a character boundary so that the file is at most
    `limit` bytes. Each character of the text weighs at least one byte in the file, so dropping as many characters
    as there are bytes over the limit always fits; a second pass covers a cut that leaves less to drop."""
    while True:
        data = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        over = len(data) - limit
        if over <= 0 or not doc["text"]:
            return data
        doc["text"] = doc["text"][:max(0, len(doc["text"]) - over)]


def _read_params(argv):
    if len(argv) != 1:
        return None
    try:
        with open(argv[0], "rb") as handle:
            params = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError):  # a JSON or UTF-8 error is a ValueError
        return None
    if not isinstance(params, dict) or set(params) != {"v", "html", "out", "content_type", "max_chars"}:
        return None
    ok = (type(params["v"]) is int and params["v"] == RESULT_VERSION
          and isinstance(params["html"], str) and params["html"]
          and isinstance(params["out"], str) and params["out"]
          and (params["content_type"] is None or isinstance(params["content_type"], str))
          and type(params["max_chars"]) is int and params["max_chars"] >= 1)
    return params if ok else None


def main(argv=None, tools=None):
    """Read, decode, parse, strip, extract, write. Returns the exit code."""
    params = _read_params(sys.argv[1:] if argv is None else argv)
    if params is None:
        sys.stderr.write(BAD_PARAMS + "\n")
        return 2
    try:
        with open(params["html"], "rb") as handle:
            data = handle.read()
    except OSError:
        sys.stderr.write(IO_FAILED + "\n")
        return 3
    tools = tools or engine()
    max_chars = params["max_chars"]
    page, charset, guessed = decode_html(data, params["content_type"], tools.detect)
    page = _XML_DECLARATION.sub("", page.translate(_DROP))
    tree = tools.parse(page)
    text, meta = "", {}
    if tree is not None:
        strip_hidden(tree)
        meta = tools.metadata(tree) or {}
        text = tools.extract(tree) or ""  # a MemoryError or any other error here ends the process: nothing is written
        text = text.translate(_DROP)
    ok = bool(text.strip())
    doc = {"v": RESULT_VERSION, "ok": ok, "text": text[:_TEXT_FACTOR * max_chars] if ok else "",
           "meta": {key: _clean(meta.get(key), _META_MAX) for key in META_KEYS},
           "charset": charset, "guessed": guessed}
    payload = serialize(doc, 8 * max_chars + _FILE_BASE)
    part = params["out"] + ".part"
    try:
        with open(part, "wb") as handle:
            handle.write(payload)
        os.replace(part, params["out"])
    except OSError:
        sys.stderr.write(IO_FAILED + "\n")
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover - run in a child process by the entry point test, not measured
    sys.exit(main())
