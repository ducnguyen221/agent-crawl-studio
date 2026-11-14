"""Which adapter takes a URL (SPEC FR8). The router reads only the normalised URL, the mode and the media-host
allowlist: it resolves no name, opens no socket and never imports `yt_dlp` (FR8.4)."""

from . import urlpolicy

ADAPTER_MEDIA = "yt-dlp"
ADAPTER_DIRECT = "direct-http"
ADAPTER_EXTRACT = "extract"
MEDIA_PORT = 443


def host_allowed(host, media_hosts):
    """True when `host` equals an allowlist entry or is a sub-domain of one (whole labels only)."""
    host = (host or "").lower()
    return any(host == entry or host.endswith("." + entry) for entry in media_hosts if entry)


def _media_ok(norm, media_hosts):
    return norm.scheme == "https" and norm.port == MEDIA_PORT and host_allowed(norm.host, media_hosts)


def route(norm, mode, media_hosts):
    """The adapter for one URL, or None (`UNSUPPORTED_SOURCE`). Raises `urlpolicy.Blocked("media_host")` when the
    mode is `media` and the URL is not an https:443 URL of an allowlisted host."""
    if mode in ("gallery", "crawl"):
        return None
    if mode == "extract":  # every URL of the mode is a page to read as text, a media host included (FR18.1)
        return ADAPTER_EXTRACT
    if mode == "media":
        if not _media_ok(norm, media_hosts):
            raise urlpolicy.Blocked("media_host")
        return ADAPTER_MEDIA
    if mode == "auto" and _media_ok(norm, media_hosts):
        return ADAPTER_MEDIA
    return ADAPTER_DIRECT
