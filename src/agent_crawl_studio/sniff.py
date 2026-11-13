"""What the first bytes and the URL path say about a download (SPEC FR3.4). Only the part that decides
CONTENT_MISMATCH lives here; the full signature table for artifacts belongs to the artifact manager."""

import codecs
import unicodedata

BINARY_EXTENSIONS = frozenset(
    "pdf png jpg jpeg gif webp zip mp4 webm mp3 ogg wav docx xlsx pptx".split())
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
_HTML_STARTS = (b"<!doctype html", b"<html", b"<head", b"<body", b"<title", b"<meta", b"<!--")
HEAD_BYTES = 512


def has_binary_extension(path):
    """True when the last path segment ends in a known binary extension (case does not matter)."""
    name = path.rsplit("/", 1)[-1]
    return "." in name and name.rsplit(".", 1)[1].lower() in BINARY_EXTENSIONS


def is_html_content_type(value):
    return (value or "").split(";", 1)[0].strip().lower() in HTML_CONTENT_TYPES


def looks_like_html(head):
    """True when the first bytes (after a BOM and white space) open an HTML document."""
    text = bytes(head[:HEAD_BYTES]).lstrip(b"\xef\xbb\xbf \t\r\n\x0c").lower()
    return text.startswith(_HTML_STARTS)


# --- what a finished file is (artifact manager, SPEC FR5.2); added after the review of the part above ---------------
EXTENSIONS = {
    "application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
    "application/zip": "zip", "text/html": "html", "application/json": "json", "text/plain": "txt",
    "text/markdown": "md",  # only `adopt` gives this type (to a file `detect` calls text/plain)
    "video/mp4": "mp4", "audio/mp4": "m4a", "video/webm": "webm", "video/x-matroska": "mkv", "audio/ogg": "ogg",
    "audio/mpeg": "mp3", "audio/wav": "wav",
}
MEDIA_MIMES = frozenset(
    {"video/mp4", "audio/mp4", "video/webm", "video/x-matroska", "audio/ogg", "audio/mpeg", "audio/wav"})
_OCTET = "application/octet-stream"


def extension_for(mime):
    """The extension for a MIME type from a fixed table of harmless ones; `bin` for anything else. Never an
    executable extension, whatever the URL or a header claims."""
    return EXTENSIONS.get(mime, "bin")


def _text_char(ch):
    return ch.isprintable() or ch in "\t\n\r\x0c" or unicodedata.category(ch) == "Zs"  # a no-break space is text


def _is_text(head):
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(bytes(head).removeprefix(b"\xef\xbb\xbf"), final=False)
    except UnicodeDecodeError:
        return False
    return bool(text) and all(_text_char(ch) for ch in text)


def _media(head):
    """The media MIME type the first bytes show (SPEC FR10.3), or None."""
    if head[4:8] == b"ftyp":
        return "audio/mp4" if head[8:12] == b"M4A " else "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):  # EBML: the DocType element (0x4282) says webm or matroska
        at = head.find(b"\x42\x82", 4, 64)
        if at >= 0 and at + 3 <= len(head):
            doctype = head[at + 3:at + 3 + (head[at + 2] & 0x7F)]
            return {b"webm": "video/webm", b"matroska": "video/x-matroska"}.get(doctype)
        return None
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    return None


def detect(head):
    """The MIME type of a file from its first bytes (not from what the server said)."""
    head = bytes(head[:HEAD_BYTES])
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return "application/zip"
    found = _media(head)
    if found:
        return found
    if looks_like_html(head):
        return "text/html"
    if _is_text(head):
        return "application/json" if head.removeprefix(b"\xef\xbb\xbf").lstrip()[:1] in (b"{", b"[") else "text/plain"
    return _OCTET
