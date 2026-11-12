"""What the first bytes and the URL path say about a download (SPEC FR3.4). Only the part that decides
CONTENT_MISMATCH lives here; the full signature table for artifacts belongs to the artifact manager."""

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
