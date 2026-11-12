"""FR5.3: the subprocess environment is rebuilt from an allow-list; no session secret leaks."""

import os

from agent_crawl_studio import envbuild

JOB_TMP = os.path.join("C:\\", "w", "jobs", "smoke-1", ".tmp")
NODE_DIR = r"C:\Program Files\nodejs"

BASE = {
    "PATH": r"C:\Windows\System32;C:\secret-tools",
    "SystemRoot": r"C:\Windows",
    "windir": r"C:\Windows",
    "PATHEXT": ".COM;.EXE;.BAT",
    "USERPROFILE": r"C:\Users\someone",
    "APPDATA": r"C:\Users\someone\AppData\Roaming",
    "TEMP": r"C:\Users\someone\AppData\Local\Temp",
    "ANTHROPIC_API_KEY": "sk-ant-leak",
    "OPENAI_API_KEY": "sk-leak",
    "GITHUB_TOKEN": "ghp_leak",
    "AWS_SECRET_ACCESS_KEY": "leak",
    "HTTP_PROXY": "http://proxy:8080",
    "HTTPS_PROXY": "http://proxy:8080",
    "YT_DLP_OPTS": "--exec calc",
}


def test_only_allowed_keys():
    env = envbuild.build_subprocess_env(BASE, JOB_TMP, node_dir=NODE_DIR)
    assert set(env) <= set(envbuild.ALLOWED_KEYS)


def test_no_secret_or_proxy_leak():
    env = envbuild.build_subprocess_env(BASE, JOB_TMP, node_dir=NODE_DIR)
    joined = "\n".join("%s=%s" % kv for kv in env.items())
    for leak in ("sk-ant-leak", "sk-leak", "ghp_leak", "leak", "proxy:8080", "calc", "secret-tools", "someone"):
        assert leak not in joined
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "HTTP_PROXY", "HTTPS_PROXY", "APPDATA", "YT_DLP_OPTS"):
        assert name not in env


def test_profile_and_temp_point_into_job_tmp():
    env = envbuild.build_subprocess_env(BASE, JOB_TMP, node_dir=NODE_DIR)
    assert env["USERPROFILE"] == JOB_TMP
    assert env["TEMP"] == JOB_TMP
    assert env["TMP"] == JOB_TMP


def test_path_is_minimal_and_has_node_dir():
    env = envbuild.build_subprocess_env(BASE, JOB_TMP, node_dir=NODE_DIR)
    parts = env["PATH"].split(os.pathsep)
    assert NODE_DIR in parts
    assert r"C:\Windows\System32" in parts
    assert not any("secret-tools" in p for p in parts)


def test_systemroot_kept():
    env = envbuild.build_subprocess_env(BASE, JOB_TMP, node_dir=NODE_DIR)
    assert env["SystemRoot"] == r"C:\Windows"


def test_missing_systemroot_falls_back_without_error():
    env = envbuild.build_subprocess_env({"PATH": "x"}, JOB_TMP, node_dir=NODE_DIR)
    assert env["SystemRoot"] == r"C:\Windows"


def test_does_not_mutate_base():
    base = dict(BASE)
    envbuild.build_subprocess_env(base, JOB_TMP, node_dir=NODE_DIR)
    assert base == BASE


def test_no_node_dir_still_builds():
    env = envbuild.build_subprocess_env(BASE, JOB_TMP)
    assert "PATH" in env and env["USERPROFILE"] == JOB_TMP


def test_every_allowed_key_is_actually_produced():
    env = envbuild.build_subprocess_env(BASE, JOB_TMP, node_dir=NODE_DIR)
    assert set(env) == set(envbuild.ALLOWED_KEYS)
