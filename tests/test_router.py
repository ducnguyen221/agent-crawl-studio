"""SPR-03 FR8.1-FR8.4: the router picks an adapter from the normalised URL and the allowlist only. It opens no
socket and never imports `yt_dlp`; the suffix rule is on whole labels."""

import subprocess
import sys

import pytest

from agent_crawl_studio import config, router, urlpolicy

HOSTS = ("youtube.com", "youtu.be")


def norm(url):
    return urlpolicy.check_url(url)


def test_adapter_names():
    assert router.ADAPTER_MEDIA == "yt-dlp" and router.ADAPTER_DIRECT == "direct-http"


@pytest.mark.parametrize("host, expected", [
    ("youtube.com", True), ("m.youtube.com", True), ("a.b.youtube.com", True), ("youtu.be", True),
    ("youtube.com.evil.com", False), ("evilyoutube.com", False), ("com", False), ("", False),
    ("example.com", False),
])
def test_host_allowed_suffix_only(host, expected):
    assert router.host_allowed(host, HOSTS) is expected


def test_host_allowed_with_an_empty_allowlist_is_false():
    assert router.host_allowed("youtube.com", ()) is False


@pytest.mark.parametrize("url, mode, expected", [
    ("https://www.youtube.com/watch?v=x", "media", "yt-dlp"),
    ("https://www.youtube.com/watch?v=x", "auto", "yt-dlp"),
    ("https://www.youtube.com/watch?v=x", "direct", "direct-http"),
    ("https://example.com/a.pdf", "auto", "direct-http"),
    ("https://example.com/a.pdf", "direct", "direct-http"),
    ("https://www.youtube.com/watch?v=x", "gallery", None),
    ("https://example.com/a.pdf", "crawl", None),
])
def test_route_table(url, mode, expected):
    assert router.route(norm(url), mode, HOSTS) == expected


def made(url, scheme, host, port):
    return urlpolicy.NormalizedUrl(url=url, scheme=scheme, host=host, port=port, target="/", is_ip=False)


@pytest.mark.parametrize("value", [
    made("https://example.com/a.pdf", "https", "example.com", 443),           # host outside the allowlist
    made("http://youtube.com/", "http", "youtube.com", 80),                   # not https
    made("https://youtube.com:8443/", "https", "youtube.com", 8443),          # not port 443
    made("https://youtube.com.evil.com/x", "https", "youtube.com.evil.com", 443),  # suffix trick
])
def test_route_media_outside_the_allowlist_or_not_https_443_is_blocked(value):
    with pytest.raises(urlpolicy.Blocked) as caught:
        router.route(value, "media", HOSTS)
    assert caught.value.args[0] == "media_host"


def test_route_auto_with_a_non_https_allowlisted_host_goes_direct():
    assert router.route(made("http://youtube.com/", "http", "youtube.com", 80), "auto", HOSTS) == "direct-http"


def test_media_host_is_a_known_block_reason():
    assert "media_host" in urlpolicy.BLOCK_REASONS


def test_route_opens_no_socket_and_never_imports_yt_dlp(no_socket):
    assert router.route(norm("https://www.youtube.com/watch?v=x"), "media", HOSTS) == "yt-dlp"
    assert "yt_dlp" not in sys.modules


def test_importing_the_router_does_not_import_yt_dlp():
    code = "import sys; from agent_crawl_studio import router; assert 'yt_dlp' not in sys.modules"
    done = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


# --- config.normalize_media_host -------------------------------------------------------------------------------------
@pytest.mark.parametrize("entry, expected", [
    (".YouTube.com.", "youtube.com"),
    ("YouTube.COM", "youtube.com"),
    ("bücher.de", "xn--bcher-kva.de"),
    ("com", None), ("10.0.0.1", None), ("localhost", None), ("a.local", None), ("", None),
    ("a b.com", None), ("https://youtube.com", None),
])
def test_normalize_media_host_table(entry, expected):
    assert config.normalize_media_host(entry) == expected
