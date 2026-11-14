# Notice and acknowledgements

Agent Crawl Studio's own code is released under the MIT License (see `LICENSE`). This repository does **not** copy or
bundle source code from the projects listed here.

## Runtime components

Installed into the tool's own virtual environment by `opcos-runtime ensure` (pinned in `requirements/core.txt` and
`locks/core-windows.txt`); they are not redistributed in this repository.

| Component | Role | License |
|---|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | media download engine, called as a library and child process | Unlicense |
| [httpx](https://github.com/encode/httpx) | HTTP transport for direct downloads (IP-pinned) | BSD-3-Clause |
| [Trafilatura](https://github.com/adbar/trafilatura) | main-text extraction of a static page | Apache-2.0 |
| mutagen | installed transitively by `yt-dlp[default]`; not imported by this project | GPL-2.0-or-later |

The exact set of transitive dependencies and their versions is in the lock file. Check each package's own license
before redistributing a built environment.

## Host tools reused, not bundled

- FFmpeg (merging local files) and Node.js (JavaScript runtime for yt-dlp) are taken from the host machine.

## Design inspiration only (no code copied)

- [Scrapy](https://github.com/scrapy/scrapy) (BSD-3-Clause), [crawlee-python](https://github.com/apify/crawlee-python)
  (Apache-2.0), [Scrapling](https://github.com/D4Vinci/Scrapling) (BSD-3-Clause): ideas for queues and job handling.
- [Media Downloader](https://github.com/mhogomchungu/media-downloader) (GPL-2.0): ideas for engine, preset and batch
  management only.
- [Firecrawl](https://github.com/firecrawl/firecrawl) (AGPL-3.0): product benchmark reference only; no code used.

## Considered and not used

- aria2 and Docling.

Licenses above were read from the upstream repositories' metadata and have not been verified against every release.
