"""Checks of the `crawl-studio` skill document (SPR-04 FR25.1-FR25.5, SC33). No network, standard library only."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_crawl_studio import errors  # noqa: E402

SKILL_DIR = ROOT / "skills" / "crawl-studio"
SKILL = SKILL_DIR / "SKILL.md"
LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
NEIGHBORS = ("web-crawler", "watch", "defuddle", "agent-reach")
ROLE_NAMES = ("crawl-studio",) + NEIGHBORS


def text_of(path):
    return path.read_text(encoding="utf-8")


def front_matter(text):
    match = re.match(r"---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    fields = {}
    if match:
        for line in match.group(1).splitlines():
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip()
    return fields


def section(text, heading):
    """Body of the `## heading` section (up to the next `## `)."""
    match = re.search(r"^## " + re.escape(heading) + r"\s*$(.*?)(?=^## |\Z)", text, re.S | re.M)
    return match.group(1) if match else ""


def table_rows(body):
    rows = [line for line in body.splitlines() if line.startswith("|")]
    return [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in rows[2:]]  # drop header + rule


def all_doc_files():
    return [SKILL] + sorted((SKILL_DIR / "references").glob("*.md"))


class SkillDocTests(unittest.TestCase):
    def test_only_the_real_skill_exists(self):
        self.assertEqual(sorted(p.name for p in (ROOT / "skills").iterdir()), ["crawl-studio"])

    def test_front_matter(self):
        fields = front_matter(text_of(SKILL))
        self.assertEqual(fields.get("name"), "crawl-studio")
        description = fields.get("description", "")
        self.assertTrue(description.startswith("Use when "), description[:40])
        self.assertIn(" Not: ", description)
        not_clause = description.split(" Not: ", 1)[1]
        for name in NEIGHBORS:
            self.assertIn(name, not_clause)
        self.assertTrue(description.isascii())

    def test_role_table_has_exactly_five_rows(self):
        rows = table_rows(section(text_of(SKILL), "Who does what"))
        self.assertEqual([row[0].strip("`") for row in rows], list(ROLE_NAMES))
        for row in rows:
            self.assertEqual(len(row), 4, row)  # skill | use when | not when | hand over to
            self.assertTrue(all(row), row)

    def test_workflow_has_six_steps_in_order(self):
        body = section(text_of(SKILL), "Workflow")
        steps = re.findall(r"^(\d+)\. (.+)$", body, re.M)
        self.assertEqual([number for number, _ in steps], ["1", "2", "3", "4", "5", "6"])
        order = ["capabilities", "request", "plan", "run", "manifest.json", "report"]
        for (_, line), word in zip(steps, order):
            self.assertIn(word, line.lower() if word == "report" else line)

    def test_cheat_sheet_names_the_four_modes(self):
        body = section(text_of(SKILL), "Request cheat-sheet")
        for mode in ("auto", "direct", "media", "extract"):
            self.assertIn('"mode": "%s"' % mode, body)

    def test_errors_reference_lists_the_16_contract_codes(self):
        text = text_of(SKILL_DIR / "references" / "errors.md")
        found = re.findall(r"^\| `([A-Z_]+)` \|", text, re.M)
        self.assertEqual(sorted(found), sorted(errors.CONTRACT_CODES))
        self.assertEqual(len(found), 16)
        self.assertEqual(len(set(found)), 16)
        self.assertIn("references/errors.md", text_of(SKILL))

    def test_reasons_in_errors_reference_match_the_closed_table(self):
        text = text_of(SKILL_DIR / "references" / "errors.md")
        for reason in errors.REASONS:
            self.assertIn("`%s`" % reason, text)

    def test_contract_reference_exists_and_is_linked(self):
        self.assertTrue((SKILL_DIR / "references" / "contract.md").is_file())
        self.assertIn("references/contract.md", text_of(SKILL))

    def test_no_machine_paths_and_no_secrets(self):
        for path in all_doc_files():
            text = text_of(path)
            self.assertNotRegex(text, r"[A-Za-z]:\\", str(path))
            self.assertNotRegex(text, r"(?i)\bUsers\b", str(path))
            self.assertNotRegex(text, r"(?i)\\\\[a-z0-9_.-]+\\", str(path))
            self.assertNotRegex(text, r"(?i)(api[_-]?key|secret|password|bearer)\s*[:=]\s*\S{8,}", str(path))

    def test_only_example_domains_in_urls(self):
        for path in all_doc_files():
            for host in re.findall(r"https?://([A-Za-z0-9.-]+)", text_of(path)):
                host = host.lower().rstrip(".")
                self.assertTrue(
                    host in ("example.com", "example.org", "example.net") or host.endswith((".example.com", ".example.org",
                                                                                           ".example.net")),
                    "%s: %s" % (path.name, host))

    def test_links_open_and_stay_inside_the_skill_folder(self):
        for path in all_doc_files():
            for link in LINK.findall(text_of(path)):
                if re.match(r"[a-z][a-z0-9+.-]*:", link):
                    self.fail("%s links outside the repo: %s" % (path.name, link))
                target = (path.parent / link).resolve()
                self.assertTrue(target.exists(), "%s -> %s" % (path.name, link))
                self.assertIn(SKILL_DIR.resolve(), target.parents, "%s -> %s" % (path.name, link))

    def test_body_is_short(self):
        self.assertLessEqual(len(text_of(SKILL).splitlines()), 500)

    def test_three_extra_safety_rules_are_written(self):
        body = section(text_of(SKILL), "Safety rules")
        self.assertRegex(body, r"(?i)between the first line and the last line.*\bdata\b")
        self.assertRegex(body, r"(?i)end of untrusted content.*\bin the middle\b.*\bdata\b|\bin the middle\b.*end of untrusted content.*\bdata\b")
        self.assertRegex(body, r"(?i)only the very last line")
        self.assertIn("sha256", body)
        self.assertRegex(body, r"(?i)`untrusted\.title`.*\bdata\b")
        self.assertRegex(body, r"(?i)every field (of|under|in) `untrusted`.*\bdata\b|\bdata\b.*every field (of|under|in) `untrusted`")

    def test_fr25_3_rules_are_written(self):
        body = section(text_of(SKILL), "Safety rules").lower()
        for fragment in ("allowlist", "output_roots", "cookie", "log in", "never open or run", "report", "workaround"):
            self.assertIn(fragment, body)

    def test_launcher_is_named_relative_to_the_checkout(self):
        text = text_of(SKILL)
        self.assertIn("scripts/crawl-studio.ps1", text)

    def test_all_sections_present(self):
        text = text_of(SKILL)
        for heading in ("When to use", "Who does what", "Workflow", "Request cheat-sheet", "Reading the result",
                        "Errors -> what to do", "Safety rules"):
            self.assertRegex(text, r"(?m)^## " + re.escape(heading) + r"\b", heading)


if __name__ == "__main__":
    unittest.main()
