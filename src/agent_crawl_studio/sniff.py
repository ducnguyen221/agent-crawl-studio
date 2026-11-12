"""What the first bytes and the URL path say about a download (SPEC FR3.4). Only the part that decides
CONTENT_MISMATCH lives here; the full signature table for artifacts belongs to the artifact manager."""

import codecs

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
}
_OCTET = "application/octet-stream"


def extension_for(mime):
    """The extension for a MIME type from a fixed table of harmless ones; `bin` for anything else. Never an
    executable extension, whatever the URL or a header claims."""
    return EXTENSIONS.get(mime, "bin")


def _is_text(head):
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(bytes(head), final=False)
    except UnicodeDecodeError:
        return False
    return bool(text) and all(ch.isprintable() or ch in "\t\n\r\x0c" for ch in text)


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
    if looks_like_html(head):
        return "text/html"
    if _is_text(head):
        return "application/json" if head.lstrip()[:1] in (b"{", b"[") else "text/plain"
    return _OCTET
