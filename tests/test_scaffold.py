"""Structural checks for this repository. Extend them as the repository grows."""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = (
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".codex-plugin/plugin.json",
)
LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")


def read_json(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def front_matter(text):
    match = re.match(r"---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not match:
        return {}
    fields = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def skill_files():
    return sorted((ROOT / "skills").glob("*/SKILL.md"))


class ScaffoldTests(unittest.TestCase):
    def test_manifests_are_valid_json(self):
        for rel in MANIFESTS:
            self.assertIsInstance(read_json(rel), dict, rel)

    def test_manifest_names_and_versions_agree(self):
        claude = read_json(MANIFESTS[0])
        market = read_json(MANIFESTS[1])
        codex = read_json(MANIFESTS[2])
        self.assertEqual({claude["name"], market["name"], market["plugins"][0]["name"], codex["name"]}, {claude["name"]})
        self.assertEqual({claude["version"], market["plugins"][0]["version"], codex["version"]}, {claude["version"]})
        self.assertEqual(claude["skills"], codex["skills"])

    def test_every_skill_has_valid_front_matter(self):
        skills = skill_files()
        self.assertTrue(skills, "no skill found under skills/")
        for path in skills:
            fields = front_matter(path.read_text(encoding="utf-8"))
            self.assertEqual(fields.get("name"), path.parent.name, str(path))
            self.assertTrue(fields.get("description", "").startswith("Use when"), str(path))

    def test_links_in_skills_resolve(self):
        for path in skill_files():
            for link in LINK.findall(path.read_text(encoding="utf-8")):
                if re.match(r"[a-z][a-z0-9+.-]*:", link):
                    continue
                self.assertTrue((path.parent / link).exists(), "%s -> %s" % (path.relative_to(ROOT), link))


    def test_manifest_has_no_placeholder_email(self):
        author = read_json(".claude-plugin/plugin.json").get("author", {})
        self.assertNotIn("email", author)

    def test_the_four_versions_match(self):
        init = (ROOT / "src/agent_crawl_studio/__init__.py").read_text(encoding="utf-8")
        found = re.search(r'__version__\s*=\s*"([^"]+)"', init)
        self.assertIsNotNone(found)
        versions = {found.group(1)}
        for rel in MANIFESTS:
            data = read_json(rel)
            if rel.endswith("marketplace.json"):
                data = data["plugins"][0]
            versions.add(data["version"])
        self.assertEqual(versions, {"0.2.1"})


if __name__ == "__main__":
    unittest.main()
