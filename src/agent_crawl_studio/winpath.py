"""Windows path rules (SPEC FR4.4, FR4.6): the names Windows treats specially, plus length limits.

Pure string functions, no file system access. Every refusal is a PathBlocked whose code is
OUTPUT_PATH_BLOCKED and whose `reason` comes from the closed list REASONS (shared with outroot)."""

import re
import unicodedata

MAX_COMPONENT = 120
MAX_PATH = 240

REASONS = (
    "empty", "dot", "colon", "trailing", "bad_char", "device", "too_long", "short_name", "prefix", "drive_relative",
    "rooted", "dotdot", "separator", "not_absolute",
    # added by outroot
    "not_in_allow_list", "forbidden_place", "cloud", "reparse", "outside_root",
)

_DEVICES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + ["COM%d" % n for n in range(1, 10)] + ["LPT%d" % n for n in range(1, 10)]
    + ["COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"]
)
_FORBIDDEN_CHARS = frozenset('<>"|?*')
_SHORT = re.compile(r"~[0-9]")


class PathBlocked(Exception):
    code = "OUTPUT_PATH_BLOCKED"

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def check_component(name, user_supplied=True):
    """Refuse a file or folder name Windows would treat specially. The 8.3 short-name rule (`~` and a
    digit) only applies to names the user supplied, never to ancestors such as DUCNGU~1."""
    if not name:
        raise PathBlocked("empty")
    if name in (".", ".."):
        raise PathBlocked("dot")
    if len(name) > MAX_COMPONENT:
        raise PathBlocked("too_long")
    if "/" in name or "\\" in name:
        raise PathBlocked("separator")
    if ":" in name:
        raise PathBlocked("colon")
    if name.endswith(".") or name.endswith(" "):
        raise PathBlocked("trailing")
    for ch in name:
        if ch in _FORBIDDEN_CHARS or unicodedata.category(ch) == "Cc":
            raise PathBlocked("bad_char")
    if name.split(".", 1)[0].rstrip(" ").upper() in _DEVICES:
        raise PathBlocked("device")
    if user_supplied and _SHORT.search(name):
        raise PathBlocked("short_name")


def check_relative(text):
    """Every part of a user-supplied relative path, with the user rules."""
    for part in re.split(r"[\\/]", text):
        check_component(part)


def check_path(path, user_parts=()):
    """An absolute, already normalised path: no \\\\?\\ or \\\\.\\ prefix, a drive and a root, no `.`/`..`
    left, every part legal, at most 240 characters. Parts whose name is in `user_parts` also get the
    8.3 rule."""
    if not isinstance(path, str) or not path:
        raise PathBlocked("empty")
    flat = path.replace("/", "\\")
    if flat.startswith("\\\\"):  # UNC, \\?\ and \\.\ all start this way
        raise PathBlocked("prefix")
    if re.match(r"^[A-Za-z]:(?![\\])", flat):
        raise PathBlocked("drive_relative")
    if not re.match(r"^[A-Za-z]:\\", flat):
        raise PathBlocked("rooted" if flat.startswith("\\") else "not_absolute")
    if len(flat.rstrip("\\")) > MAX_PATH:
        raise PathBlocked("too_long")
    mine = {name.lower() for name in user_parts}
    for part in flat[3:].split("\\"):
        if part == "":
            continue
        if part in (".", ".."):
            raise PathBlocked("dotdot")
        check_component(part, user_supplied=part.lower() in mine)


def names_collide(left, right):
    """Windows names that differ only by case are the same name."""
    return left.lower() == right.lower()


def find_collision(existing, name):
    for item in existing:
        if names_collide(item, name):
            return item
    return None
