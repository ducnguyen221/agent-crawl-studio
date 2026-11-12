"""SPEC FR4.1 / CQ2: `<work_root>\\config.json` is the only place `output_roots` and
`media_host_allowlist` come from; missing or broken file means the safe defaults, never a crash.
(Slice A, TK3: parsing. The hard-floor filtering of `output_roots` is added with TK7/TK8.)"""

import json
import os

import pytest

from agent_crawl_studio import capabilities, config

DEFAULT_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "soundcloud.com", "tiktok.com")


def env_for(tmp_path, **extra):
    env = {"CRAWL_STUDIO_WORK": str(tmp_path / "w"), "OPCOS_ROOT": str(tmp_path / "opcos")}
    env.update(extra)
    return env


def put(tmp_path, content):
    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    path = work / "config.json"
    path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode("utf-8"))
    return path


def test_missing_file_gives_only_the_work_root_and_the_default_hosts(tmp_path):
    cfg = config.load_config(env_for(tmp_path))
    assert cfg.work_root == str(tmp_path / "w")
    assert cfg.output_roots == [str(tmp_path / "w")]
    assert tuple(cfg.media_host_allowlist) == DEFAULT_HOSTS and cfg.warnings == []
    assert not (tmp_path / "w").exists(), "loading the config must not create anything"


def test_default_hosts_equal_config_example_json():
    path = os.path.join(os.path.dirname(config.__file__), "..", "..", "config.example.json")
    with open(path, encoding="utf-8") as handle:
        example = json.load(handle)
    assert tuple(example["media_host_allowlist"]) == DEFAULT_HOSTS == tuple(config.DEFAULT_MEDIA_HOSTS)


def test_valid_file_adds_roots_after_the_work_root_and_resolves_relative_to_the_opcos_root(tmp_path):
    put(tmp_path, {"output_roots": ["workspaces/extra", str(tmp_path / "abs")], "media_host_allowlist": ["Example.com", "vimeo.com"]})
    cfg = config.load_config(env_for(tmp_path))
    assert cfg.output_roots == [
        str(tmp_path / "w"),
        os.path.normpath(str(tmp_path / "opcos" / "workspaces" / "extra")),
        os.path.normpath(str(tmp_path / "abs")),
    ]
    assert tuple(cfg.media_host_allowlist) == ("example.com", "vimeo.com") and cfg.warnings == []


def test_a_root_equal_to_the_work_root_is_not_listed_twice(tmp_path):
    put(tmp_path, {"output_roots": [str(tmp_path / "w")]})
    assert config.load_config(env_for(tmp_path)).output_roots == [str(tmp_path / "w")]


def test_relative_root_without_an_opcos_base_is_ignored_with_a_warning(tmp_path):
    put(tmp_path, {"output_roots": ["workspaces/extra"]})
    env = {"CRAWL_STUDIO_WORK": str(tmp_path / "w")}
    cfg = config.load_config(env)
    assert cfg.output_roots == [str(tmp_path / "w")] and cfg.warnings == ["root_ignored"]


@pytest.mark.parametrize("content", [
    b"{", b"[]", b"null", b"", b"\xef\xbb\xbf{}", b"\xff\xfe", b'{"output_roots": "x"}', b'{"output_roots": [5]}',
    b'{"output_roots": [""]}', b'{"media_host_allowlist": [1]}', b'{"media_host_allowlist": "x"}',
    b'{"unknown_key": 1}', b'{"output_roots": [], "output_roots": []}', b"x" * (1024 * 1024 + 1),
    b'{"output_roots": ["a\\u0000b"]}',
])
def test_broken_file_gives_the_defaults_and_one_warning_without_echoing_content(content, tmp_path):
    put(tmp_path, content)
    cfg = config.load_config(env_for(tmp_path))
    assert cfg.output_roots == [str(tmp_path / "w")] and tuple(cfg.media_host_allowlist) == DEFAULT_HOSTS
    assert cfg.warnings == ["config_invalid"]


def test_config_json_that_is_a_directory_is_invalid(tmp_path):
    (tmp_path / "w" / "config.json").mkdir(parents=True)
    assert config.load_config(env_for(tmp_path)).warnings == ["config_invalid"]


def test_no_environment_variable_can_move_the_config_file(tmp_path):
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"output_roots": [str(tmp_path / "evil")]}), encoding="utf-8")
    env = env_for(tmp_path, CRAWL_STUDIO_CONFIG=str(other), CONFIG=str(other), OUTPUT_ROOTS=str(tmp_path / "evil"))
    assert config.config_path(env) == str(tmp_path / "w" / "config.json")
    assert config.load_config(env).output_roots == [str(tmp_path / "w")]


def test_unknown_work_root_propagates_the_work_root_error():
    with pytest.raises(capabilities.WorkRootError):
        config.load_config({})
    with pytest.raises(capabilities.WorkRootError):
        config.load_config({"CRAWL_STUDIO_WORK": "relative\\path"})


def test_opcos_root_comes_from_opcos_root_or_userprofile():
    assert config.opcos_root({"OPCOS_ROOT": "C:\\x"}) == "C:\\x"
    assert config.opcos_root({"USERPROFILE": "C:\\Users\\u"}) == os.path.join("C:\\Users\\u", "OpcOS")
    assert config.opcos_root({}) is None
