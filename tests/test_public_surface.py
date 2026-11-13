"""SPEC FR2.8: no command-line option, environment variable, config key or request field can turn the
network or output policy off or weaker. The environment variables the code reads form a CLOSED list."""

import re
from pathlib import Path

from agent_crawl_studio import cli, config, envbuild, schema

SRC = Path(__file__).resolve().parents[1] / "src" / "agent_crawl_studio"

# SPEC FR2.8: the closed list. Adding a name needs a SPEC change and an edit of this test.
CLOSED_ENV_LIST = {
    "CRAWL_STUDIO_WORK", "OPCOS_ROOT", "USERPROFILE", "FFMPEG_DIR", "PATH", "PATHEXT", "SystemRoot", "TEMP",
    "APPDATA", "ProgramData",
}
READ = re.compile(r"""(?:\benv|\bbase_env|\bos\.environ|\bbase)\s*(?:\.get\(\s*|\[\s*)["']([A-Za-z_][A-Za-z0-9_]*)["']""")
FORBIDDEN_WORDS = ("insecure", "allow_private", "allow-private", "no_ssrf", "no-ssrf", "unsafe", "skip_check", "skip-check", "bypass", "disable_policy", "verify_tls")


def sources():
    return {path.name: path.read_text(encoding="utf-8") for path in SRC.glob("*.py")}


def test_environment_variables_read_by_the_code_stay_inside_the_closed_list():
    found = set()
    for text in sources().values():
        found |= set(READ.findall(text))
    assert found, "the scan found nothing: the pattern is wrong"
    assert found == CLOSED_ENV_LIST, (sorted(found - CLOSED_ENV_LIST), sorted(CLOSED_ENV_LIST - found))


def test_environment_is_only_touched_through_the_command_line_entry_point():
    for name, text in sources().items():
        assert "os.getenv" not in text and "putenv" not in text, name
        if name not in ("cli.py", "envbuild.py"):
            assert "os.environ" not in text, name


def test_command_line_accepts_only_the_documented_options():
    assert cli.COMMANDS == {"capabilities": (), "plan": ("--request",), "run": ("--request",)}


def test_no_source_file_names_a_policy_switch():
    for name, text in sources().items():
        low = text.lower()
        for word in FORBIDDEN_WORDS:
            assert word not in low, (name, word)


def test_request_keys_are_the_documented_set_and_carry_no_policy_switch():
    assert set(schema.REQUEST_KEYS) == {"schema_version", "urls", "mode", "output_dir", "limits", "naming_policy", "existing_file_policy"}
    assert set(schema.URL_ITEM_KEYS) == {"url", "expect"}
    assert schema.NAMING_POLICIES == ("id_hash",) and schema.EXISTING_FILE_POLICIES == ("rename",)


def test_config_keys_are_the_documented_set():
    assert set(config.CONFIG_KEYS) == {"output_roots", "media_host_allowlist"}


def test_config_path_is_fixed_under_the_work_root(tmp_path):
    env = {"CRAWL_STUDIO_WORK": str(tmp_path / "w"), "CRAWL_STUDIO_CONFIG": str(tmp_path / "other.json")}
    assert config.config_path(env) == str(tmp_path / "w" / "config.json")


def test_subprocess_environment_keys_are_a_closed_list():
    """ALLOWED_KEYS is what a child process may inherit; adding a name is a SPEC change (FR2.8)."""
    assert envbuild.ALLOWED_KEYS == ("PATH", "SystemRoot", "windir", "PATHEXT", "USERPROFILE", "TEMP", "TMP", "PYTHONUTF8")
