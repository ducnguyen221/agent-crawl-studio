"""SPEC FR4.4, FR4.6: Windows path names (alternate data streams, device names, trailing dot/space,
8.3 short names, \\\\?\\ prefixes, `..`, drive-relative forms, length limits)."""

import os

import pytest

from agent_crawl_studio import winpath
from agent_crawl_studio.winpath import PathBlocked, check_component, check_path

BAD_NAMES = [
    "", ".", "..", "a:b", "a.txt:stream", "x.", "x ", "x. ", "x .", "CON", "con", "Con.txt", "con.txt", "NUL", "nul.tar.gz",
    "PRN", "AUX", "COM1", "com9", "LPT1", "LPT9", "lpt5.txt", "COM\u00b9", "COM\u00b2", "COM\u00b3", "LPT\u00b9", "LPT\u00b2",
    "LPT\u00b3", "CONIN$", "CONOUT$", "conin$.txt", "a<b", "a>b", 'a"b', "a|b", "a?b", "a*b", "a\x01b", "a\x1fb", "a\x7fb",
    "a\x80b", "a" * 121, "a\\b", "a/b", "con:ads", "NUL ",
]
GOOD_NAMES = ["a", "a" * 120, "COM10", "COM0", "CONSOLE", "console.txt", "x.y", "a b", "~tilde", "a~b", "file.pdf", "d\u00e9j\u00e0",
              "CONIN", "LPT", "batch-1", "x.tar.gz", ".hidden"]


@pytest.mark.parametrize("name", BAD_NAMES, ids=[repr(n)[:30] for n in BAD_NAMES])
def test_bad_component_is_blocked(name):
    with pytest.raises(PathBlocked) as info:
        check_component(name)
    assert info.value.code == "OUTPUT_PATH_BLOCKED" and info.value.reason in winpath.REASONS


@pytest.mark.parametrize("name", GOOD_NAMES)
def test_good_component_passes(name):
    check_component(name)


@pytest.mark.parametrize("name", ["PROGRA~1", "DUCNGU~1", "a~1.txt", "x~9"])
def test_short_names_are_blocked_in_the_user_part_only(name):
    with pytest.raises(PathBlocked) as info:
        check_component(name)
    assert info.value.reason == "short_name"
    check_component(name, user_supplied=False)


def test_a_device_name_is_blocked_even_when_it_is_not_user_supplied():
    with pytest.raises(PathBlocked):
        check_component("CON", user_supplied=False)


def test_names_that_differ_only_by_case_collide():
    assert winpath.names_collide("Report.PDF", "report.pdf") is True
    assert winpath.names_collide("a", "b") is False
    assert winpath.find_collision(["A.txt", "b"], "a.TXT") == "A.txt"
    assert winpath.find_collision(["A.txt"], "c") is None


BAD_PATHS = [
    "\\\\?\\C:\\x", "\\\\.\\pipe\\x", "//?/C:/x", "//./pipe/x", "\\\\?\\UNC\\srv\\share", "C:foo", "C:", "\\foo", "/foo", "a\\b", "",
    "C:\\a\\..\\b", "C:\\a\\.\\b", "C:\\a\\b:ads", "C:\\a\\CON\\b", "C:\\a\\x.\\b", "C:\\" + "a" * 121,
    "C:\\" + "a" * 100 + "\\" + "b" * 100 + "\\" + "c" * 100,
]


@pytest.mark.parametrize("path", BAD_PATHS, ids=[repr(p)[:40] for p in BAD_PATHS])
def test_bad_absolute_path_is_blocked(path):
    with pytest.raises(PathBlocked) as info:
        check_path(path)
    assert info.value.code == "OUTPUT_PATH_BLOCKED"


def test_path_of_exactly_240_is_ok_and_241_is_blocked():
    path = "C:\\" + "a" * 100 + "\\" + "b" * 100 + "\\" + "c" * 35
    assert len(path) == 240
    check_path(path)
    with pytest.raises(PathBlocked) as info:
        check_path(path + "c")
    assert info.value.reason == "too_long"


def test_a_short_name_in_an_ancestor_is_fine_for_check_path():
    check_path("C:\\Users\\DUCNGU~1\\AppData\\Local\\Temp")
    with pytest.raises(PathBlocked):
        check_path("C:\\Users\\DUCNGU~1\\x", user_parts=["DUCNGU~1"])


def test_check_path_accepts_forward_slashes_and_trailing_separator():
    check_path("C:/Users/x/out/")


def test_check_relative_checks_every_part_with_the_user_rules():
    winpath.check_relative("a/b\\c")
    for bad in ("a/CON", "a/b:c", "PROGRA~1/x", "a/../b", "x./y"):
        with pytest.raises(PathBlocked):
            winpath.check_relative(bad)


# --- real calls: an alternate data stream and a device name really exist on this machine ---------------------
def test_a_real_alternate_data_stream_name_is_what_we_block(tmp_path):
    target = tmp_path / "a.bin"
    target.write_bytes(b"x")
    with open(str(target) + ":Zone.Identifier", "w") as stream:
        stream.write("[ZoneTransfer]\nZoneId=3\n")
    assert os.path.exists(str(target) + ":Zone.Identifier")
    with pytest.raises(PathBlocked):
        check_component("a.bin:Zone.Identifier")
    os.remove(str(target) + ":Zone.Identifier")
    assert not os.path.exists(str(target) + ":Zone.Identifier")


def test_a_real_device_name_resolves_to_the_device_not_a_file(tmp_path):
    assert os.path.exists(os.path.join(str(tmp_path), "NUL"))  # Windows maps it to the NUL device
    with pytest.raises(PathBlocked):
        check_component("NUL")
