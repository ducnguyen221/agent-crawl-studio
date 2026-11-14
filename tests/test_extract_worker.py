"""SPR-04 TK7 phase 2 (FR19.2, FR19.4, FR20.1, FR20.2, FR20.7; SC27, SC28): the child process `extract_worker.py`.

Two layers. (1) In-process tests that run in every environment: the worker module is imported like any module (this
is how its coverage is measured) and driven with a stand-in engine, so decoding, hidden-part removal, the size caps,
the exit codes and the written file are all checked without trafilatura. (2) Child-process tests that run the real
file with `python -I` and the real engine; they need a Python that has trafilatura (the core venv) and are SKIPPED
with a stated reason when none is found, never silently."""

import ast
import functools
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import types
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from agent_crawl_studio import extract

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "src" / "agent_crawl_studio" / "extract_worker.py"
FIXTURES = ROOT / "tests" / "fixtures" / "extract"
MIN_RECALL = 0.8


def load_worker():
    return importlib.import_module("agent_crawl_studio.extract_worker")


# --- the real engine, when there is a Python that has it --------------------------------------------------------
def _engine_candidates():
    root = os.environ.get("OPCOS_ROOT") or os.path.join(os.path.expanduser("~"), "OpcOS")
    return [sys.executable, os.path.join(root, "runtime", "venvs", "agent-crawl-studio", "core", "Scripts",
                                         "python.exe")]


@functools.lru_cache(maxsize=None)
def engine_python():
    for python in _engine_candidates():
        if not os.path.isfile(python):
            continue
        probe = subprocess.run([python, "-I", "-c", "import trafilatura, lxml.html"], capture_output=True,
                               timeout=120)
        if probe.returncode == 0:
            return python
    return None


@pytest.fixture
def py():
    python = engine_python()
    if python is None:
        pytest.skip("NO_ENGINE_PYTHON: no Python with trafilatura found (tried sys.executable and the core venv "
                    "under <OPCOS_ROOT>/runtime/venvs/agent-crawl-studio/core); the real-engine worker tests "
                    "did NOT run; the stand-in tests did")
    return python


def run_worker(python, tmp_path, data, content_type=None, max_chars=200000, driver=None, timeout=600):
    """Run the real file in a child process. `driver` is code run before it (for patches), receiving the worker path
    and the params path in sys.argv[1:]. Returns (CompletedProcess, parsed out.json or None)."""
    html, out, params = tmp_path / "in.html", tmp_path / "out.json", tmp_path / "params.json"
    html.write_bytes(data)
    params.write_text(json.dumps({"v": 1, "html": str(html), "out": str(out), "content_type": content_type,
                                  "max_chars": max_chars}), encoding="utf-8")
    argv = [python, "-I", "-X", "utf8"]
    argv += ["-c", driver, str(WORKER), str(params)] if driver else [str(WORKER), str(params)]
    env = {key: os.environ[key] for key in ("SystemRoot", "PATH", "TEMP", "TMP") if key in os.environ}
    done = subprocess.run(argv, capture_output=True, timeout=timeout, env=env, cwd=str(tmp_path))
    doc = json.loads(out.read_bytes().decode("utf-8")) if out.exists() else None
    return done, doc


DRIVER_NO_SOCKET = """
import sys, runpy
hits = []
def audit(event, args):
    # TK11: the hook is installed BEFORE the engine is imported (urllib3 binds a socket to ::1 on import, which is
    # not traffic); a connection, a name lookup or a datagram is a hit and is refused
    if event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"):
        hits.append(event)
        raise OSError("network blocked by the test")
sys.addaudithook(audit)
import trafilatura, lxml.html
sys.argv = [sys.argv[1], sys.argv[2]]
code = 0
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as stop:
    code = stop.code or 0
sys.stdout.write("HITS=%d" % len(hits))
sys.exit(code)
"""

DRIVER_MEMORY_ERROR = """
import sys, runpy
import trafilatura, lxml.html
def refuse(*args, **kwargs):
    raise MemoryError()
trafilatura.extract = refuse
sys.argv = [sys.argv[1], sys.argv[2]]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


# --- a stand-in engine ------------------------------------------------------------------------------------------
class StandIn:
    """What `main(tools=...)` needs: parse, extract, metadata, detect. `parse` reads well-formed XML so that
    `strip_hidden` runs on a real tree; `extract` returns the text of the tree it is given."""

    def __init__(self, text=None, meta=None, detected=None, tree=True, fail=None):
        self.text, self.meta, self.detected, self.tree, self.fail = text, meta, detected, tree, fail
        self.parsed, self.seen = None, None

    def parse(self, text):
        self.parsed = text
        return ET.fromstring(text) if self.tree else None

    def extract(self, tree):
        if self.fail:
            raise self.fail
        self.seen = ET.tostring(tree, encoding="unicode")
        return "".join(tree.itertext()) if self.text is None else self.text

    def metadata(self, tree):
        return self.meta

    def detect(self, data):
        return self.detected


def write_params(tmp_path, data=b"<html><body><p>Hello world</p></body></html>", **over):
    params = {"v": 1, "html": str(tmp_path / "in.html"), "out": str(tmp_path / "out.json"),
              "content_type": None, "max_chars": 1000}
    params.update(over)
    (tmp_path / "in.html").write_bytes(data)
    path = tmp_path / "params.json"
    path.write_text(json.dumps(params), encoding="utf-8")
    return str(path)


def run_in_process(tmp_path, tools, data=b"<html><body><p>Hello world</p></body></html>", **over):
    worker = load_worker()
    code = worker.main([write_params(tmp_path, data, **over)], tools=tools)
    out = tmp_path / "out.json"
    return code, (json.loads(out.read_bytes().decode("utf-8")) if out.exists() else None)


# --- the file itself --------------------------------------------------------------------------------------------
def test_worker_imports_only_the_standard_library_and_the_engine_never_the_package():
    tree = ast.parse(WORKER.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import needs the package"
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "import_module":
            pass
    assert "agent_crawl_studio" not in names
    assert names <= set(sys.stdlib_module_names) | {"trafilatura", "lxml", "charset_normalizer"}, names


def test_the_names_of_the_package_do_not_appear_in_the_code_of_the_worker():
    code = [node for node in ast.walk(ast.parse(WORKER.read_text(encoding="utf-8")))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert not [node.value for node in code if node.value.startswith("agent_crawl_studio")]


def test_copied_constants_and_functions_have_not_drifted_from_the_core():
    worker = load_worker()
    assert worker.ALLOWED_CHARSETS == extract.ALLOWED_CHARSETS
    assert worker.RESULT_KEYS == extract.RESULT_KEYS
    assert worker.META_KEYS == extract.META_KEYS
    assert worker.RESULT_VERSION == 1
    samples = [
        (b'<meta charset="windows-1250">', "text/html; charset=ISO-8859-2"),
        (b'<meta charset="windows-1250">', None),
        (b'<meta charset="utf-7">', "text/html; charset=utf-7"),
        (b'<meta http-equiv="Content-Type" content="text/html; charset=Shift_JIS">', ""),
        (b"<meta charset=utf-8>", 'text/html; charset="UTF-8"'),
        (b"<p>none</p>", "text/html"),
        (b"", None),
        (b"x" * 2000 + b'<meta charset="big5">', None),
        (b'<meta charset="iso-8859-12">', None),
    ]
    for head, header in samples:
        assert worker.declared_charset(head, header) == extract.declared_charset(head, header), (head, header)


def test_the_sniffing_of_utf16_without_a_bom_agrees_with_the_core_bit_for_bit():
    worker = load_worker()
    html = "<!DOCTYPE html><html><body><p>chuữ</p></body></html>"
    for data in (html.encode("utf-16-le"), html.encode("utf-16-be"), html.encode("utf-8"), b"", b"ab",
                 b"\x00\x00\x00\x00", b"<h" + b"\x00" * 8):
        guess = worker.utf16_without_bom(data)
        assert guess in (None, "utf-16le", "utf-16be")
        assert (guess is not None) == (extract._head_text(data) != data.decode("utf-8", "replace")), data


# --- decoding (FR20.1) ------------------------------------------------------------------------------------------
VI = "Chăm sóc cây trồng trong chậu phụ thuộc hoàn toàn vào người"


def nfc(text):
    return unicodedata.normalize("NFC", text)


def enc1258(text):
    """windows-1258 has no precomposed Vietnamese tone letters: base letter plus a combining tone mark."""
    tones = {"̀", "́", "̃", "̉", "̣"}
    out = bytearray()
    for ch in text:
        try:
            out += ch.encode("windows-1258")
            continue
        except UnicodeEncodeError:
            pass
        parts = unicodedata.normalize("NFD", ch)
        tone = "".join(mark for mark in parts[1:] if mark in tones)
        base = unicodedata.normalize("NFC", parts[0] + "".join(mark for mark in parts[1:] if mark not in tones))
        out += (base + tone).encode("windows-1258")
    return bytes(out)


def never_called(data):
    raise AssertionError("the library detector must not run when the page can be decoded without a guess")


@pytest.mark.parametrize("raw,header,expect_charset", [
    (("﻿<p>%s</p>" % VI).encode("utf-8"), "text/html; charset=iso-8859-1", "utf-8"),  # BOM beats a wrong header
    (enc1258("<p>%s</p>" % VI), "text/html; charset=windows-1258", "windows-1258"),
    (enc1258("<meta charset=windows-1258><p>%s</p>" % VI), None, "windows-1258"),
    (("<meta charset=windows-1258><p>%s</p>" % VI).encode("utf-8"), "text/html; charset=utf-8", "utf-8"),  # header wins
])
def test_decode_order_without_a_guess(raw, header, expect_charset):
    worker = load_worker()
    text, charset, guessed = worker.decode_html(raw, header, never_called)
    assert VI in nfc(text) and charset == expect_charset and guessed is False


def test_a_utf8_bom_is_removed_and_a_utf16_bom_picks_the_byte_order():
    worker = load_worker()
    assert worker.decode_html(b"\xef\xbb\xbfabc", None, never_called) == ("abc", "utf-8", False)
    for codec in ("utf-16-le", "utf-16-be"):
        raw = ("﻿<p>%s</p>" % VI).encode(codec)
        text, charset, guessed = worker.decode_html(raw, "text/html; charset=windows-1250", never_called)
        assert text == "<p>%s</p>" % VI and charset == "utf-16" and guessed is False


def test_utf16_without_a_bom_is_recognised_and_counts_as_a_guess():
    worker = load_worker()
    html = "<!DOCTYPE html><html><body><p>%s</p></body></html>" % VI
    for name, codec in (("utf-16le", "utf-16-le"), ("utf-16be", "utf-16-be")):
        text, charset, guessed = worker.decode_html(html.encode(codec), None, never_called)
        assert text == html and charset == name and guessed is True


def test_undeclared_valid_utf8_is_not_a_guess():
    worker = load_worker()
    assert worker.decode_html(("<p>%s</p>" % VI).encode("utf-8"), None, never_called) == (
        "<p>%s</p>" % VI, "utf-8", False)


def test_a_charset_outside_the_allowed_list_counts_as_not_declared():
    worker = load_worker()
    raw = b"<meta charset=utf-7><p>+ADw-script+AD4-alert(1)+ADw-/script+AD4-</p>"
    text, charset, guessed = worker.decode_html(raw, "text/html; charset=utf-7", never_called)
    assert charset == "utf-8" and guessed is False
    assert "<script" not in text  # the utf-7 payload stays inert text: it is never decoded as utf-7


def test_a_declaration_the_bytes_contradict_falls_through_to_the_guess():
    worker = load_worker()
    raw = enc1258("<meta charset=utf-8><p>%s</p>" % VI)
    seen = []
    text, charset, guessed = worker.decode_html(raw, None, lambda data: seen.append(data) or "windows-1258")
    assert VI in nfc(text) and charset == "windows-1258" and guessed is True and len(seen) == 1


def test_the_header_that_fails_falls_through_to_the_meta_before_any_guess():
    worker = load_worker()
    raw = enc1258("<meta charset=windows-1258><p>%s</p>" % VI)
    text, charset, guessed = worker.decode_html(raw, "text/html; charset=utf-8", never_called)
    assert VI in nfc(text) and charset == "windows-1258" and guessed is False


def test_a_charset_python_does_not_have_counts_as_undecodable():
    worker = load_worker()
    assert "iso-8859-12" in worker.ALLOWED_CHARSETS  # in the list, but Python has no such codec
    text, charset, guessed = worker.decode_html(b"<meta charset=iso-8859-12><p>abc</p>", None, lambda data: None)
    assert "abc" in text and charset == "utf-8" and guessed is False


def test_a_meta_that_names_utf16_is_read_as_utf8_like_a_browser_does():
    worker = load_worker()
    raw = ("<meta charset=utf-16><p>%s</p>" % VI).encode("utf-8")
    text, charset, guessed = worker.decode_html(raw, None, never_called)
    assert VI in nfc(text) and charset == "utf-8" and guessed is False


@pytest.mark.parametrize("detected,expected", [("cp1258", "cp1258"), (None, "windows-1252"),
                                               ("no-such-codec", "windows-1252"), ("utf_7", "windows-1252"),
                                               ("iso-8859-12", "windows-1252")])  # TK11: utf-7 is never taken
def test_bad_bytes_with_no_declaration_go_to_the_detector_and_never_raise(detected, expected):
    worker = load_worker()
    raw = b"<p>caf\xe9 \xff\xfe broken \x80\x81</p>"
    text, charset, guessed = worker.decode_html(raw, None, lambda data: detected)
    assert "broken" in text and guessed is True and charset == expected
    assert isinstance(text, str)


def test_decoding_never_raises_on_arbitrary_bytes():
    worker = load_worker()
    import random

    rng = random.Random(7)
    for size in (0, 1, 2, 3, 5, 64, 1500):
        for _ in range(30):
            raw = bytes(rng.randrange(256) for _ in range(size))
            for header in (None, "text/html; charset=shift_jis", "text/html; charset=utf-16"):
                text, charset, guessed = worker.decode_html(raw, header, lambda data: "utf-8")
                assert isinstance(text, str) and isinstance(charset, str) and isinstance(guessed, bool)


def test_the_default_detector_is_none_without_the_library_and_a_name_with_it(monkeypatch):
    worker = load_worker()
    monkeypatch.setitem(sys.modules, "charset_normalizer", None)  # `import` now raises ImportError
    assert worker.detect_charset(b"abc") is None

    fake = types.SimpleNamespace(from_bytes=lambda data: types.SimpleNamespace(
        best=lambda: types.SimpleNamespace(encoding="cp1258")))
    assert worker.detect_charset(b"abc", load=lambda name: fake) == "cp1258"
    nothing = types.SimpleNamespace(from_bytes=lambda data: types.SimpleNamespace(best=lambda: None))
    assert worker.detect_charset(b"abc", load=lambda name: nothing) is None
    seen = []
    spy = types.SimpleNamespace(from_bytes=lambda data: seen.append(len(data)) or types.SimpleNamespace(
        best=lambda: None))
    worker.detect_charset(b"x" * (3 << 20), load=lambda name: spy)
    assert seen == [1 << 20]  # the detector sees a bounded sample, not a whole page


# --- hidden parts (FR20.2) --------------------------------------------------------------------------------------
def visible_text(markup):
    worker = load_worker()
    root = ET.fromstring(markup, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    worker.strip_hidden(root)
    return "".join(root.itertext())


@pytest.mark.parametrize("element", [
    "<script>BAD</script>", "<style>BAD</style>", "<noscript>BAD</noscript>", "<template>BAD</template>",
    "<iframe>BAD</iframe>", "<object>BAD</object>", "<embed>BAD</embed>", "<svg>BAD</svg>",
    "<SCRIPT>BAD</SCRIPT>",
    "<!--BAD-->",
    '<p hidden="">BAD</p>', "<p HIDDEN='hidden'>BAD</p>", '<p aria-hidden="true">BAD</p>',
    '<p aria-hidden=" TRUE ">BAD</p>',
    '<p style="display:none">BAD</p>', '<p style="color:red; DISPLAY : NONE">BAD</p>',
    '<p style="visibility:hidden">BAD</p>', '<p style="Visibility : HIDDEN !important">BAD</p>',
    '<p style="font-size:0">BAD</p>', '<p style="font-size: 0px;">BAD</p>', '<p style="FONT-SIZE:0pt">BAD</p>',
    '<p style="opacity:0">BAD</p>', '<p style="opacity : 0;color:red">BAD</p>', '<p STYLE="opacity:0.0">BAD</p>',
    '<p style="/*x*/display/*y*/:/*z*/none">BAD</p>',
    '<div><p style="display:none"><b>BAD</b><i hidden="">BAD</i></p></div>',  # nested hidden inside hidden
])
def test_hidden_parts_are_removed(element):
    assert visible_text("<body><p>keep1</p>%s<p>keep2</p></body>" % element) == "keep1keep2"


@pytest.mark.parametrize("element", [
    '<p aria-hidden="false">GOOD</p>', '<p style="font-size:0.5em">GOOD</p>', '<p style="font-size:10px">GOOD</p>',
    '<p style="opacity:0.5">GOOD</p>', '<p style="opacity:1">GOOD</p>', '<p style="display:block">GOOD</p>',
    '<p style="visibility:visible">GOOD</p>', '<p class="hidden">GOOD</p>', "<p>GOOD</p>",
    '<p data-style="display:none">GOOD</p>',
])
def test_visible_parts_are_kept(element):
    assert visible_text("<body>%s</body>" % element) == "GOOD"


def test_the_text_after_a_removed_element_is_kept():
    assert visible_text("<body><p>a<script>BAD</script> tail one <b hidden=''>BAD</b> tail two</p></body>") == (
        "a tail one  tail two")


def test_a_hidden_comment_is_removed_and_its_tail_is_kept():
    assert visible_text("<body><p>a<!--BAD--> tail</p></body>") == "a tail"


def test_a_hidden_root_leaves_nothing():
    assert visible_text("<div hidden=''><p>BAD</p></div>") == ""  # TK11: `html` and `body` are exempt (FR20.2 b)


# --- the stand-in pipeline --------------------------------------------------------------------------------------
def test_the_pipeline_writes_one_result_file_with_the_agreed_shape(tmp_path):
    tools = StandIn(meta={"title": "T", "author": None, "date": "2026-01-01", "sitename": "S", "lang": "en"})
    html = b"<html><body><p>Hello <b>world</b></p><p hidden='1'>SECRET</p><!--SECRET2--></body></html>"
    code, doc = run_in_process(tmp_path, tools, html)
    assert code == 0
    assert tuple(doc) == extract.RESULT_KEYS and doc["v"] == 1 and doc["ok"] is True
    assert doc["text"] == "Hello world"
    assert doc["meta"] == {"title": "T", "author": None, "date": "2026-01-01", "sitename": "S", "lang": "en"}
    assert doc["charset"] == "utf-8" and doc["guessed"] is False
    assert "SECRET" not in tools.seen and "SECRET2" not in tools.seen  # gone BEFORE the engine saw the tree
    assert sorted(os.listdir(tmp_path)) == ["in.html", "out.json", "params.json"]  # no temp file left


def test_the_written_file_passes_the_core_reader(tmp_path):
    code, _ = run_in_process(tmp_path, StandIn(meta={"title": "T"}))
    assert code == 0
    result = extract.read_result(str(tmp_path / "out.json"), 1000, None)
    assert result["ok"] is True and result["text"] == "Hello world" and result["meta"] == {"title": "T"}


def test_the_decoded_page_loses_its_control_characters_before_the_parser_sees_it(tmp_path):
    tools = StandIn()
    page = "<html><body><p>a\x00b\x1bc\x7fd\x85e￾</p></body></html>"
    run_in_process(tmp_path, tools, page.encode("utf-8"))
    assert tools.parsed == "<html><body><p>abcde</p></body></html>"


def test_an_xml_declaration_is_dropped_before_the_parser_sees_it(tmp_path):
    tools = StandIn()
    data = b'  <?xml version="1.0" encoding="utf-8"?>\n<html><body><p>x</p></body></html>'
    run_in_process(tmp_path, tools, data)
    assert tools.parsed.lstrip().startswith("<html")


def test_control_characters_leave_the_text_and_the_meta_but_not_tab_or_newline(tmp_path):
    tools = StandIn(text="a\x1b b\u0085c\td\ne\r", meta={"title": "t\x00i\x1btle", "author": "a\x7f"})
    _, doc = run_in_process(tmp_path, tools)
    assert doc["text"] == "a bc\td\ne" and doc["meta"]["title"] == "title" and doc["meta"]["author"] == "a"


def test_no_text_is_ok_false_with_an_empty_text(tmp_path):
    for text in (None, "", " \n\t \x1b "):
        tools = StandIn(text=text if text is not None else "")
        if text is None:
            tools.extract = lambda tree: None
        code, doc = run_in_process(tmp_path, tools)
        assert code == 0 and doc["ok"] is False and doc["text"] == ""


def test_a_page_the_parser_cannot_build_a_tree_from_is_ok_false(tmp_path):
    code, doc = run_in_process(tmp_path, StandIn(tree=False), b"   ")
    assert code == 0 and doc["ok"] is False and doc["text"] == "" and doc["meta"] == dict.fromkeys(
        extract.META_KEYS)


def test_metadata_may_be_missing_or_odd(tmp_path):
    for meta in (None, {}, {"title": 5, "bogus": "x", "date": ""}):
        _, doc = run_in_process(tmp_path, StandIn(meta=meta))
        assert set(doc["meta"]) == set(extract.META_KEYS)
        assert all(value is None or isinstance(value, str) for value in doc["meta"].values())
        assert "bogus" not in doc["meta"]


def test_the_text_is_cut_at_two_times_max_chars_and_each_meta_field_at_1000(tmp_path):
    tools = StandIn(text="x" * 100, meta={"title": "m" * 5000, "sitename": "s" * 1000})
    _, doc = run_in_process(tmp_path, tools, max_chars=10)
    assert doc["text"] == "x" * 21 and doc["ok"] is True  # TK11: 2 x max_chars + 1
    assert doc["meta"]["title"] == "m" * 1000 and doc["meta"]["sitename"] == "s" * 1000


@pytest.mark.parametrize("ch", ["漢", "\U00020000", '"', "x\n", "a"])
def test_a_text_at_the_ceiling_fits_the_size_ceiling_whatever_the_character(tmp_path, ch):
    """RF6: CJK is 3 bytes and an astral character 4 in UTF-8 (a \\u escape would be 6 or 12); a quote or a line
    feed is 2 bytes escaped. The worst case 2 x max_chars characters stays inside 8 x max_chars + 65 536."""
    max_chars = 5000
    tools = StandIn(text=ch * (3 * max_chars), meta=dict.fromkeys(extract.META_KEYS, "\U00020000" * 1000))
    code, _ = run_in_process(tmp_path, tools, max_chars=max_chars)
    raw = (tmp_path / "out.json").read_bytes()
    assert code == 0 and len(raw) <= 8 * max_chars + 65536
    assert "\\u" not in raw.decode("utf-8")  # written as characters, not escapes
    result = extract.read_result(str(tmp_path / "out.json"), max_chars, None)  # the core reads it
    assert result["ok"] is True


def test_serialize_cuts_the_text_by_encoded_size_at_a_character_boundary():
    worker = load_worker()
    doc = {"v": 1, "ok": True, "text": "\U00020000" * 400 + '"' * 400, "meta": dict.fromkeys(worker.META_KEYS),
           "charset": "utf-8", "guessed": False}
    for limit in (300, 1000, 1201, 5000):
        data = worker.serialize(dict(doc), limit)
        assert len(data) <= limit
        back = json.loads(data.decode("utf-8"))  # whole characters only: valid UTF-8 and valid JSON
        assert doc["text"].startswith(back["text"]) and back["ok"] is True
    big = worker.serialize(dict(doc), 10**6)
    assert json.loads(big.decode("utf-8"))["text"] == doc["text"]  # nothing is cut when it fits


def test_serialize_keeps_the_key_order_of_the_result():
    worker = load_worker()
    doc = {"v": 1, "ok": False, "text": "", "meta": dict.fromkeys(worker.META_KEYS), "charset": "utf-8",
           "guessed": False}
    assert tuple(json.loads(worker.serialize(doc, 10000))) == extract.RESULT_KEYS


def test_a_memory_error_from_the_engine_is_not_caught_and_nothing_is_written(tmp_path):
    with pytest.raises(MemoryError):
        run_in_process(tmp_path, StandIn(fail=MemoryError()))
    assert sorted(os.listdir(tmp_path)) == ["in.html", "params.json"]


def test_any_other_engine_error_also_escapes_and_nothing_is_written(tmp_path):
    with pytest.raises(RuntimeError):
        run_in_process(tmp_path, StandIn(fail=RuntimeError("boom")))
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("v"), lambda p: p.update(v=2), lambda p: p.update(v=True), lambda p: p.update(extra=1),
    lambda p: p.pop("out"), lambda p: p.update(html=5), lambda p: p.update(out=""), lambda p: p.update(html=""),
    lambda p: p.update(content_type=5), lambda p: p.update(max_chars=0), lambda p: p.update(max_chars=-3),
    lambda p: p.update(max_chars=True), lambda p: p.update(max_chars="10"), lambda p: p.update(max_chars=1.5),
])
def test_bad_parameters_exit_2_with_a_fixed_message_and_write_nothing(tmp_path, capsys, mutate):
    worker = load_worker()
    params = {"v": 1, "html": str(tmp_path / "in.html"), "out": str(tmp_path / "out.json"), "content_type": None,
              "max_chars": 10}
    mutate(params)
    (tmp_path / "in.html").write_bytes(b"<p>x</p>")
    path = tmp_path / "params.json"
    path.write_text(json.dumps(params), encoding="utf-8")
    assert worker.main([str(path)], tools=StandIn()) == 2
    assert not (tmp_path / "out.json").exists()
    assert capsys.readouterr().err.strip() == worker.BAD_PARAMS


@pytest.mark.parametrize("content", [b"not json", b"[]", b"\xff\xfe", b"", b'"s"'])
def test_a_params_file_that_is_not_an_object_exits_2(tmp_path, capsys, content):
    worker = load_worker()
    path = tmp_path / "params.json"
    path.write_bytes(content)
    assert worker.main([str(path)], tools=StandIn()) == 2
    assert capsys.readouterr().err.strip() == worker.BAD_PARAMS


def test_a_missing_params_file_or_argument_exits_2(tmp_path, capsys):
    worker = load_worker()
    assert worker.main([str(tmp_path / "nope.json")], tools=StandIn()) == 2
    assert worker.main([], tools=StandIn()) == 2
    assert worker.main(["a", "b"], tools=StandIn()) == 2
    assert capsys.readouterr().err.count(worker.BAD_PARAMS) == 3


def test_an_unreadable_page_or_output_exits_3_with_a_fixed_message_and_no_traceback(tmp_path, capsys):
    worker = load_worker()
    path = write_params(tmp_path)
    os.remove(tmp_path / "in.html")
    assert worker.main([path], tools=StandIn()) == 3
    path = write_params(tmp_path, out=str(tmp_path / "no-such-dir" / "out.json"))
    assert worker.main([path], tools=StandIn()) == 3
    err = capsys.readouterr().err
    assert err.count(worker.IO_FAILED) == 2 and "Traceback" not in err and str(tmp_path) not in err


def test_the_default_engine_is_built_from_the_imported_modules():
    worker = load_worker()
    calls = []

    class Parser:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    class ParserError(Exception):
        pass

    def document_fromstring(text, parser=None):
        if not text.strip():
            raise ParserError("empty")
        return ("tree", text)

    meta_doc = types.SimpleNamespace(title="T", author="A", date="D", sitename="S", language="vi")
    modules = {
        "lxml.html": types.SimpleNamespace(HTMLParser=Parser, document_fromstring=document_fromstring),
        "lxml.etree": types.SimpleNamespace(ParserError=ParserError),
        "trafilatura": types.SimpleNamespace(extract=lambda tree, **kw: calls.append(kw) or "markdown"),
        "trafilatura.metadata": types.SimpleNamespace(extract_metadata=lambda tree: meta_doc),
        "charset_normalizer": None,
    }
    tools = worker.engine(load=modules.__getitem__)
    assert calls[0] == {"no_network": True, "huge_tree": False, "recover": True, "remove_comments": True,
                        "remove_pis": True}
    assert tools.parse("<p>x</p>") == ("tree", "<p>x</p>") and tools.parse("  ") is None
    assert tools.extract("tree") == "markdown"
    assert calls[1] == {"include_links": False, "include_images": False, "include_comments": False,
                        "include_tables": True, "output_format": "markdown"}
    assert tools.metadata("tree") == {"title": "T", "author": "A", "date": "D", "sitename": "S", "lang": "vi"}
    modules["trafilatura.metadata"] = types.SimpleNamespace(extract_metadata=lambda tree: None)
    assert worker.engine(load=modules.__getitem__).metadata("tree") is None
    assert callable(tools.detect)


def test_the_script_entry_point_runs_main_and_exits_with_its_code(tmp_path):
    done = subprocess.run([sys.executable, "-I", str(WORKER)], capture_output=True, timeout=60)
    assert done.returncode == 2 and done.stdout == b"" and load_worker().BAD_PARAMS in done.stderr.decode()


# --- the real engine, in a child process (needs a Python with trafilatura) -------------------------------------
def load_bench():
    spec = importlib.util.spec_from_file_location("bench_extract", ROOT / "scripts" / "bench_extract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_entries():
    return json.loads((FIXTURES / "manifest.json").read_bytes().decode("utf-8"))


@pytest.mark.parametrize("entry", fixture_entries(), ids=lambda entry: entry["file"])
def test_the_ten_fixtures_through_the_real_child_process(py, tmp_path, entry):
    bench = load_bench()
    if entry.get("generate") == "large":
        data = bench.make_large().encode("utf-8")
    else:
        data = (FIXTURES / entry["file"]).read_bytes()
    done, doc = run_worker(py, tmp_path, data)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-800:]
    assert done.stdout == b"" and tuple(doc) == extract.RESULT_KEYS and doc["v"] == 1
    assert doc["guessed"] is False  # every fixture declares its charset or is valid UTF-8
    flat = bench.phrase(doc["text"])
    if entry["expect"] == "no_text":
        assert doc["ok"] is False and doc["text"] == ""
        return
    assert doc["ok"] is True and doc["text"].strip()
    if entry["expect"] == "memory_only":
        return
    if entry["file"] != "table-layout.html":  # trafilatura keeps the left menu and counter of that page: ADR-006
        for junk in entry["must_absent"]:
            assert bench.phrase(junk) not in flat, junk
    for marker in entry["hidden"]:
        assert marker not in doc["text"], marker
    gold = bench.normalize((FIXTURES / entry["gold"]).read_text(encoding="utf-8"))
    _, _, recall = bench.f1(bench.normalize(doc["text"]), gold)
    assert recall >= MIN_RECALL, (entry["file"], recall)
    assert len(doc["text"]) <= 2 * 200000
    result = extract.read_result(str(tmp_path / "out.json"), 200000, None)  # and the core accepts it
    assert result["ok"] is True


def test_the_charset_of_each_fixture_is_the_one_the_manifest_names(py, tmp_path):
    for name, expected in (("vi-windows1258.html", "windows-1258"), ("table-layout.html", "iso-8859-1"),
                           ("vi-utf8.html", "utf-8")):
        done, doc = run_worker(py, tmp_path, (FIXTURES / name).read_bytes())
        assert done.returncode == 0 and doc["charset"] == expected, (name, doc and doc["charset"])


VI_PAGE = ("<html><head>%s<title>Chăm sóc cây</title></head><body><article><h1>Chăm sóc "
           "cây trồng</h1><p>" + VI + " chăm sóc. Một chậu có lỗ thoát "
           "nước tốt và đất tơi xốp là điều kiện "
           "đầu tiên để cây khoẻ mạnh.</p><p>Hãy tưới khi "
           "lớp đất mặt khô khoảng một đốt ngón tay. Tưới "
           "đầu quanh gốc cho đến khi nước chảy ra ở đáy "
           "chậu, rồi đổ bỏ nước đọng trong đĩa lót."
           "</p></article></body></html>")


def vi_page(meta=""):
    return VI_PAGE % meta


ENCODING_CASES = [
    # id, bytes, Content-Type, expected charset (None = any), guessed
    ("bom-utf8-wrong-header", b"\xef\xbb\xbf" + vi_page().encode("utf-8"), "text/html; charset=iso-8859-1", "utf-8",
     False),
    ("bom-utf16", vi_page().encode("utf-16"), "text/html; charset=windows-1250", "utf-16", False),
    ("header", enc1258(vi_page()), "text/html; charset=windows-1258", "windows-1258", False),
    ("meta", enc1258(vi_page('<meta charset="windows-1258">')), None, "windows-1258", False),
    ("declared-wrong-bytes-contradict", enc1258(vi_page('<meta charset="utf-8">')), None, None, True),
    ("utf7-declared", vi_page('<meta charset="utf-7">').encode("utf-8"), "text/html; charset=utf-7", "utf-8", False),
    ("broken-bytes", vi_page().encode("utf-8").replace(b"<p>", b"<p>\xff\xfe\x80 ", 1), None, None, True),
    ("undeclared-utf8", vi_page().encode("utf-8"), None, "utf-8", False),
    ("undeclared-windows-1258", enc1258(vi_page()), None, None, True),
    ("utf16le-no-bom", vi_page().encode("utf-16-le"), None, "utf-16le", True),
    ("utf16be-no-bom", vi_page().encode("utf-16-be"), None, "utf-16be", True),
    ("xhtml-declaration", b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n' + vi_page().encode("utf-8"),
     "application/xhtml+xml", "utf-8", False),
]


@pytest.mark.parametrize("data,header,charset,guessed", [case[1:] for case in ENCODING_CASES],
                         ids=[case[0] for case in ENCODING_CASES])
def test_encoding_cases_never_crash_and_flag_only_the_guesses(py, tmp_path, data, header, charset, guessed):
    done, doc = run_worker(py, tmp_path, data, header)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-800:]
    assert doc["guessed"] is guessed
    if charset:
        assert doc["charset"] == charset
    assert doc["ok"] is True
    if not guessed:
        assert "người" in nfc(doc["text"])  # a declared charset gives the right text; a guess may not


def test_utf16_and_xhtml_pages_that_the_core_recognises_as_html_are_extracted(py, tmp_path):
    for data in (vi_page().encode("utf-16"), vi_page().encode("utf-16-le"),
                 b'<?xml version="1.0"?>\n<!DOCTYPE html>\n' + vi_page().encode("utf-8")):
        assert extract.is_html(data)
        done, doc = run_worker(py, tmp_path, data)
        assert done.returncode == 0 and doc["ok"] is True and "người" in doc["text"]


def test_the_injection_page_loses_every_hidden_marker_before_the_engine(py, tmp_path):
    entry = next(item for item in fixture_entries() if item["file"] == "injection.html")
    done, doc = run_worker(py, tmp_path, (FIXTURES / entry["file"]).read_bytes())
    assert done.returncode == 0 and doc["ok"] is True
    blob = json.dumps(doc, ensure_ascii=False)
    for marker in entry["hidden"]:
        assert marker not in blob, marker
    assert "attacker.invalid/x.sh" not in blob


def test_the_extraction_is_the_same_with_the_network_blocked(py, tmp_path):
    data = (FIXTURES / "blog.html").read_bytes()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    plain_done, plain = run_worker(py, tmp_path / "a", data)
    blocked_done, blocked = run_worker(py, tmp_path / "b", data, driver=DRIVER_NO_SOCKET)
    assert plain_done.returncode == 0 and blocked_done.returncode == 0, blocked_done.stderr.decode()[-600:]
    assert blocked_done.stdout.decode().endswith("HITS=0")
    assert blocked == plain and blocked["ok"] is True


def test_broken_html_is_repaired_not_thrown(py, tmp_path):
    for data in (b"<p>unclosed <b>bold <i>x", b"<<<>>><p", b"</div></div><p>after stray ends</p>", b"<!--", b"",
                 b"   \n ", b"<p>" + b"<div>" * 3000 + b"deep text that is long enough to matter" + b"</div>" * 10,
                 b"\x00\x01\x02<p>ctl\x1b</p>"):
        done, doc = run_worker(py, tmp_path, data)
        assert done.returncode == 0, (data[:30], done.stderr.decode("utf-8", "replace")[-500:])
        assert doc["v"] == 1 and isinstance(doc["ok"], bool)
    done, doc = run_worker(py, tmp_path, (FIXTURES / "broken.html").read_bytes())
    assert doc["ok"] is True and "bicycle stays reliable" in doc["text"]


def test_no_text_page_is_ok_false_not_a_crash(py, tmp_path):
    for data in (b"<html><body></body></html>", b"<html><script>var x=1;</script></html>",
                 b"<html><body><p style='display:none'>hidden only</p></body></html>"):
        done, doc = run_worker(py, tmp_path, data)
        assert done.returncode == 0 and doc["ok"] is False and doc["text"] == ""


def _wide_page(first, count):
    """Distinct paragraphs of wide characters, so that the engine's own de-duplication keeps all of them."""
    paragraphs = []
    for index in range(count):
        paragraphs.append("<p>" + "".join(chr(first + (index * 37 + step * 11) % 2000) for step in range(120))
                          + "</p>")
    return "<html><head><meta charset=utf-8><title>wide</title></head><body><article>%s</article></body></html>" % (
        "".join(paragraphs))


@pytest.mark.parametrize("first,label", [(0x4E00, "cjk"), (0x20000, "astral")])
def test_a_wide_character_page_up_to_the_ceiling_stays_inside_the_file_ceiling(py, tmp_path, first, label):
    """RF6, SC28: CJK (3 bytes) and characters outside the BMP (4 bytes) up to the ceiling `max_chars` still
    give a result file within 8 x max_chars + 65 536 bytes, written as characters, and the core takes it."""
    max_chars = 20000
    done, doc = run_worker(py, tmp_path, _wide_page(first, 400).encode("utf-8"), max_chars=max_chars)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-600:]
    raw = (tmp_path / "out.json").read_bytes()
    assert doc["ok"] is True and len(doc["text"]) >= max_chars  # at least the ceiling of characters came through
    assert len(doc["text"]) <= 2 * max_chars + 1  # TK11: one over the ceiling tells the core "cut"
    assert len(raw) <= 8 * max_chars + 65536
    assert b"\\u" not in raw
    result = extract.read_result(str(tmp_path / "out.json"), max_chars, None)
    assert result["ok"] is True and 0 < len(result["text"]) <= max_chars, label


def test_a_memory_error_in_the_engine_exits_nonzero_and_never_writes_ok_true(py, tmp_path):
    done, doc = run_worker(py, tmp_path, (FIXTURES / "blog.html").read_bytes(), driver=DRIVER_MEMORY_ERROR)
    assert done.returncode != 0 and doc is None
    assert b"MemoryError" in done.stderr
    assert sorted(os.listdir(tmp_path)) == ["in.html", "params.json"]


def test_a_missing_input_file_exits_3_in_the_real_child_process(py, tmp_path):
    done, doc = run_worker(py, tmp_path, b"x")
    os.remove(tmp_path / "in.html")
    os.remove(tmp_path / "out.json")
    again = subprocess.run([py, "-I", str(WORKER), str(tmp_path / "params.json")], capture_output=True, timeout=120)
    assert again.returncode == 3 and again.stdout == b""


def test_the_real_engine_loader_builds_working_tools(py, tmp_path):
    code = ("import importlib.util,sys;spec=importlib.util.spec_from_file_location('w',sys.argv[1]);"
            "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);t=m.engine();"
            "r=t.parse('<html><head><title>Hi</title></head><body><p>Some words here</p></body></html>');"
            "print(r.tag, t.parse('  '), t.metadata(r)['title'], t.detect(b'caf\\xe9 au lait ' * 40))")
    done = subprocess.run([py, "-I", "-c", code, str(WORKER)], capture_output=True, timeout=120)
    assert done.returncode == 0, done.stderr.decode()[-600:]
    tag, nothing, title, detected = done.stdout.decode().split()[:4]
    assert (tag, nothing, title) == ("html", "None", "Hi") and detected
