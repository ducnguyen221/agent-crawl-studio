"""Configuration: `<work_root>\\config.json`, a FIXED path. No environment variable, option or request
field can point the tool at another file (SPEC FR2.8, FR4.1, ADR-004).

Missing file: the safe defaults. Broken file: the safe defaults plus the warning `config_invalid`
(the content is never printed). `output_roots` always starts with the work root itself."""

import json
import os
from dataclasses import dataclass, field

from . import capabilities

CONFIG_KEYS = ("output_roots", "media_host_allowlist")
# `media_host_allowlist` of a user's config.json is read and validated, but nothing acts on it until SPR-03 (media
# through the core); the smoke script uses this default tuple.
DEFAULT_MEDIA_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "soundcloud.com", "tiktok.com")
MAX_CONFIG_BYTES = 1024 * 1024
FILE_NAME = "config.json"


@dataclass
class Config:
    work_root: str
    output_roots: list
    media_host_allowlist: tuple
    warnings: list = field(default_factory=list)


def config_path(env):
    return os.path.join(capabilities.work_root(env), FILE_NAME)


def opcos_root(env):
    base = env.get("OPCOS_ROOT")
    if not base and env.get("USERPROFILE"):
        base = os.path.join(env["USERPROFILE"], "OpcOS")
    return base or None


def _pairs(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen[key] = value
    return seen


def _text_ok(value):
    return isinstance(value, str) and bool(value) and all(ord(ch) >= 32 and ord(ch) != 127 for ch in value)


def _read(path):
    """The parsed config object, or None when the file is missing; raises ValueError when broken."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("unreadable") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("too large")
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, RecursionError):
        raise ValueError("not json") from None
    if not isinstance(doc, dict) or any(key not in CONFIG_KEYS for key in doc):
        raise ValueError("bad shape")
    roots = doc.get("output_roots", [])
    hosts = doc.get("media_host_allowlist", list(DEFAULT_MEDIA_HOSTS))
    if not isinstance(roots, list) or not all(_text_ok(r) for r in roots):
        raise ValueError("bad output_roots")
    if not isinstance(hosts, list) or not all(_text_ok(h) for h in hosts):
        raise ValueError("bad media_host_allowlist")
    return {"output_roots": roots, "media_host_allowlist": [h.lower() for h in hosts]}


def load_config(env):
    work = capabilities.work_root(env)  # raises WorkRootError
    roots = [work]
    hosts = tuple(DEFAULT_MEDIA_HOSTS)
    warnings = []
    try:
        doc = _read(os.path.join(work, FILE_NAME))
    except ValueError:
        return Config(work, roots, hosts, ["config_invalid"])
    if doc is not None:
        base = opcos_root(env)
        for entry in doc["output_roots"]:
            if os.path.isabs(entry):
                resolved = os.path.normpath(entry)
            elif base:
                resolved = os.path.normpath(os.path.join(base, entry))
            else:
                if "root_ignored" not in warnings:
                    warnings.append("root_ignored")
                continue
            if resolved not in roots:
                roots.append(resolved)
        hosts = tuple(doc["media_host_allowlist"])
    return Config(work, roots, hosts, warnings)
