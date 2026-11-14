# Ecosystem

- Role: a local-first tool for agents to fetch documents, images and videos from public URLs into an organised folder and to extract the text of one static HTML page, returning a structured JSON manifest (SHA-256, coded errors); security first (SSRF guard, confined writes, untrusted-content framing). For the owner of this machine and the agents he works with.
- Upstream: author-owned code; engines used as libraries or child processes, pinned in locks/core-windows.txt: yt-dlp (media), httpx (direct HTTP), trafilatura (extraction); FFmpeg and Node already on the machine are reused, not bundled.
- Downstream: agents in Claude Code (and other harnesses when installed) through the skill crawl-studio.
- Bundles: none registered. Neighbouring skills it hands work to are listed in skills/crawl-studio/SKILL.md (web-crawler, watch, defuddle, agent-reach).
- Handoff: the caller supplies a request JSON (URLs, mode, output root inside the configured output_roots, limits) and runs plan, then run; it gets one JSON line equal to manifest.json, the files under the job folder, and coded errors with a retry hint.

Out of scope: multi-page crawling, playlists and galleries, JavaScript rendering, logins and cookies, resuming partial downloads, the trusted profile, opening or running downloaded files.
