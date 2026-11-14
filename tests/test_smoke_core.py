"""FR6.1 / FR17.1: `scripts/smoke_download.py` uses the core for everything; it carries no copy of the policy and no
engine path of its own (SPR-03 TK13)."""

import re

from test_smoke_args import ROOT

# the exact pattern of SPEC SC8 / RF6: any of these in scripts/ is a second copy of the policy
OWN_COPY = re.compile(r"def _public\b|def default_dns_probe\b|def _confined_work\b|^ALLOWLIST =", re.M)
SECOND_ENGINE_PATH = re.compile(r"yt_dlp|fixed_ytdlp_flags|subprocess|system_run|_host_allowed")


def test_no_script_carries_its_own_copy_of_the_policy():
    found = [(path.name, match.group(0)) for path in (ROOT / "scripts").glob("*.py")
             for match in OWN_COPY.finditer(path.read_text(encoding="utf-8"))]
    assert found == []


def test_the_smoke_script_has_no_media_path_of_its_own():
    text = (ROOT / "scripts" / "smoke_download.py").read_text(encoding="utf-8")
    assert SECOND_ENGINE_PATH.findall(text) == []
    assert "runner.run" in text and "runner.plan" in text
