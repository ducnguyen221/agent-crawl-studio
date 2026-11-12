# Agent Crawl Studio

Safe web and media crawling for agents: plan, fetch and extract with a manifest and security policy.

**Incubating build (bản ươm), not a release.** Private, local git only, no remote. SPR-01 delivers the skeleton: a pinned engine venv installed by `opcos-runtime ensure`, the `capabilities` command and a download smoke test. Replace the sample skill, fill in `LICENSE`, review every file for private data, and only then consider publishing.

## Layout

- `skills/example-skill/SKILL.md` is a minimal sample skill. Rename it, or add skills beside it.
- `.claude-plugin/plugin.json` is the source of the plugin declaration; keep `.claude-plugin/marketplace.json` and `.codex-plugin/plugin.json` consistent with it.
- `tests/test_scaffold.py` checks the structure (manifests, skill front matter, links).
- `AGENTS.md` holds the rules for agents working in this repository; `ECOSYSTEM.md` states its role and hand-offs.

## Commands (SPR-01)

- `scripts/crawl-studio.ps1 capabilities` prints one JSON line (engine versions, ffmpeg, node, work root). If the core venv is missing it prints a `DEPENDENCY_MISSING` line with the `ensure` hint and exits 0; it never installs anything.
- `python scripts/smoke_download.py --url <url> --work <dir>` (core venv Python) downloads one approved video and reports `ok`, `BLOCKED_ENV`, `FAIL` or `NOT_RUN`. `--work` is a PARENT folder that must resolve to a place under `<work_root>/jobs` (`work_root` = `CRAWL_STUDIO_WORK` or `<OpcOS>/workspaces/agent-crawl-studio`); anything else is `OUTPUT_PATH_BLOCKED` and nothing is created. The script makes its own job folder `<dir>/smoke-<yyyymmdd-hhmmss>-<6 random chars>/{files,.tmp,logs}` (never reused, no nesting). Network profile: https, port 443, no userinfo, host in the allow-list constant `ALLOWLIST` in the script (the only source in SPR-01; `config.example.json` mirrors it and a test keeps them equal), every resolved address must be public. One media item may run 45 minutes; running out of time is `FAIL` with reason `timeout`.
- **Residual risk (RK, later sprint, egress proxy):** the allow-list covers the URL we pass and one DNS lookup; yt-dlp may follow a redirect to another host or re-resolve a name. Not blocked in SPR-01.
- Engine and pins: `requirements/core.txt`, `locks/core-windows.txt`, `opcos-runtime.json` (venv `core`, own scope, no `install.package`). Config sample: `config.example.json`.

## Local use

Use the skills from this checkout with an agent that supports skill directories. For Claude-compatible plugin tooling, add the local marketplace `.claude-plugin/marketplace.json` and install `agent-crawl-studio@agent-crawl-studio`. Codex-compatible metadata is in `.codex-plugin/plugin.json`. Installation and routing across harnesses are not validated by the scaffold.

## Verify

Run `python -X utf8 -m pytest tests -q -p no:cacheprovider --cov=src` with the system Python (needs `pytest` and `pytest-cov`; set `PYTHONDONTWRITEBYTECODE=1`). Tests use only the standard library, inject every collaborator, touch no network and create no `.venv` in the checkout.

## License

See `LICENSE` (NONE). Copyright (c) 2026 Nguyễn Quang Đức.
