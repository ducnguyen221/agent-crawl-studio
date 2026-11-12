"""RF67: names the user puts in `output_dir` may not hold format characters, line/paragraph separators,
look-alikes of path separators or anything NFKC would rewrite. (A user-chosen folder name, not a URL.)"""

import pytest

from agent_crawl_studio import winpath
from agent_crawl_studio.winpath import PathBlocked, check_component

LOOKALIKES = [
    "a\u200eb", "a\u200fb", "a\u202eb", "a\u202ab", "a\u2066b", "a\u00adb", "a\u2060b", "a\ufeffb", "a\u200bb", "a\u200db",
    "a\u2028b", "a\u2029b", "a\uff3cb", "a\u2215b", "a\u2044b", "a\uff0fb", "a\uff1ab", "a\u29f5b", "a\u2216b", "a\ufe68b",
    "\ufb01le", "a\u2460", "a\u2122", "a\uff21b", "e\u0301", "a\u00a0b", "a\u3000b", "a\ud800b",
]
NFKC_ONLY = ["ﬁle", "a①", "a™", "aＡb", "é", "a b", "a　b"]  # only the user-supplied part is held to NFKC
GOOD = ["caf\u00e9", "\u65e5\u672c\u8a9e", "a b", "\u0645\u0631\u062d\u0628\u0627", "ma\u00f1ana", "x-y_z", "r\u00e9sum\u00e9.pdf"]


@pytest.mark.parametrize("name", LOOKALIKES, ids=[ascii(n) for n in LOOKALIKES])
def test_format_separator_and_lookalike_characters_are_blocked(name):
    with pytest.raises(PathBlocked) as info:
        check_component(name)
    assert info.value.reason in ("bad_char", "lookalike") and info.value.reason in winpath.REASONS


@pytest.mark.parametrize("name", [n for n in LOOKALIKES if n not in NFKC_ONLY], ids=ascii)
def test_format_and_separator_characters_are_blocked_in_ancestors_too(name):
    with pytest.raises(PathBlocked):
        check_component(name, user_supplied=False)


@pytest.mark.parametrize("name", GOOD)
def test_ordinary_unicode_names_still_pass(name):
    check_component(name)


def test_check_path_blocks_a_lookalike_in_any_part():
    with pytest.raises(PathBlocked):
        winpath.check_path("C:\\a\\b\uff3cc\\d")
