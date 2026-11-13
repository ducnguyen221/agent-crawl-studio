"""SPEC FR2.1, FR2.2, FR2.3, FR5.8 (pure part), FR6.5: URL and IP policy. NO network and no socket here:
`check_url` never resolves a name (that happens at connection time, Slice B). The only DNS call is the
real `socket.getaddrinfo("localhost")`, answered by the local hosts file."""

import hashlib
import socket
import time

import httpx
import pytest

from agent_crawl_studio import urlpolicy
from agent_crawl_studio.urlpolicy import Blocked, Policy, check_url, dns_probe, is_public_ip, redact, url_sha256

LONG_OK = "https://example.com/" + "a" * (2048 - len("https://example.com/"))

# --- allowed URLs: (raw, normalised url, host, port, is_ip) --------------------------------------------------
ALLOWED = [
    ("https://example.com/a.pdf", "https://example.com/a.pdf", "example.com", 443, False),
    ("HTTPS://Example.COM:443/A?x=1#frag", "https://example.com/A?x=1", "example.com", 443, False),
    ("http://example.com", "http://example.com/", "example.com", 80, False),
    ("https://example.com?x=1", "https://example.com/?x=1", "example.com", 443, False),
    ("https://example.com:80/", "https://example.com:80/", "example.com", 80, False),
    ("http://example.com:443/", "http://example.com:443/", "example.com", 443, False),
    ("https://example.com./x", "https://example.com/x", "example.com", 443, False),
    ("https://ｅｘａｍｐｌｅ.com/", "https://example.com/", "example.com", 443, False),
    ("https://bücher.example/", "https://xn--bcher-kva.example/", "xn--bcher-kva.example", 443, False),
    ("https://example.com/ü?q=é", "https://example.com/%C3%BC?q=%C3%A9", "example.com", 443, False),
    ("https://example.com/p%20q", "https://example.com/p%20q", "example.com", 443, False),
    ("https://example.com/a@b", "https://example.com/a@b", "example.com", 443, False),
    ("https://a-b.example.co.uk/x", "https://a-b.example.co.uk/x", "a-b.example.co.uk", 443, False),
    ("https://8.8.8.8/", "https://8.8.8.8/", "8.8.8.8", 443, True),
    ("https://[2606:4700:4700::1111]/", "https://[2606:4700:4700::1111]/", "2606:4700:4700::1111", 443, True),
    ("https://[2606:4700:4700:0:0:0:0:1111]/x", "https://[2606:4700:4700::1111]/x", "2606:4700:4700::1111", 443, True),
    (LONG_OK, LONG_OK, "example.com", 443, False),
]

# --- blocked URLs: grouped as in SPEC FR6.5 (SSRF group) ---------------------------------------------------
SCHEME = [
    "file:///etc/passwd", "ftp://example.com/", "data:text/plain,x", "javascript:alert(1)", "gopher://example.com/",
    "http:example.com", "//example.com/", "example.com/path", "", "https:///path", "https://", "ws://example.com/",
    "https:example.com", "HTTPS//example.com",
]
USERINFO = [
    "https://user:pass@example.com/", "https://user@example.com/", "https://example.com@evil.com/",
    "https://@example.com/", "https://a:b@127.0.0.1/", "https://example.com:443@evil.com/",
]
CHARACTERS = [
    "https://example.com\\@evil.com/", "https:\\\\example.com", "https://example.com/a\\b", "https://exa\tmple.com/",
    "https://example.com/\n", "https://example.com/ x", "https://example.com/\x00", "https://exa mple.com/",
    "https://example.com/\x7f", "https://example.com/　", "https://example.com/ ", "https://example.com/\r\nHost: evil",
    "https://example.com/" + "a" * 2029, "https://example.com/ ",
]
PORTS = [
    "https://example.com:22/", "https://example.com:8080/", "https://example.com:0/", "https://example.com:65535/",
    "https://example.com:99999/", "https://example.com:-1/", "https://example.com:/", "https://example.com:４４３/",
    "https://example.com:443abc/", "https://example.com:0443/", "https://example.com:+443/", "https://example.com:4_43/",
    "https://example.com:443:443/", "http://127.0.0.1:22/", "https://[::1]:8080/",
]
LITERALS = [
    "http://127.0.0.1/", "http://127.0.0.1:80/", "https://[::1]/", "http://0.0.0.0/", "http://10.0.0.1/",
    "http://172.16.0.1/", "http://172.31.255.255/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/", "http://224.0.0.1/", "https://[ff02::1]/", "https://[fec0::1]/", "https://[fe80::1]/",
    "https://[fd00::1]/", "http://255.255.255.255/", "http://192.0.2.1/", "http://198.18.0.1/", "https://[::]/",
    "http://127.255.255.254/", "http://8.8.8.8.:22/",
]
EMBEDDED = [
    "https://[::ffff:127.0.0.1]/", "https://[::ffff:7f00:1]/", "https://[::ffff:0:127.0.0.1]/", "https://[::7f00:1]/",
    "https://[::a00:1]/", "https://[64:ff9b::7f00:1]/", "https://[64:ff9b::a00:1]/", "https://[2002:7f00:1::]/",
    "https://[2002:a00:1::]/", "https://[::ffff:169.254.169.254]/", "https://[::ffff:10.0.0.1]/", "https://[::808:808]/",
    "https://[1::1]/", "https://[4000::1]/", "https://[fc00::1]/", "https://[::ffff:192.168.0.1]/",
    "https://[64:ff9b::c0a8:1]/", "https://[2002:c0a8:1::]/",
]
AMBIGUOUS = [
    "http://2130706433/", "http://0177.0.0.1/", "http://0x7f.0.0.1/", "http://0x7f000001/", "http://127.1/",
    "http://127.0.1/", "http://017700000001/", "http://1.1.1/", "http://01.1.1.1/", "http://1.1.1.256/",
    "http://1.1.1.1.1/", "http://example.123/", "http://0xff/", "http://1/", "http://127.0.0.1./", "http://0/",
    "http://4294967295/", "http://0x7f.1/", "http://00.0.0.0/", "http://0x/", "http://0X7F.0.0.1/", "http://:443/", "http://./",
]
NAMES = [
    "http://localhost/", "http://LOCALHOST/", "http://localhost./", "http://foo.localhost/", "http://a.b.localhost/",
    "http://printer.local/", "http://intranet/", "http://a/", "http://example/", "http://exa_mple.com/",
    "http://-bad.example.com/", "http://bad-.example.com/", "http://a..b/", "http://.example.com/", "http://example.com../",
    "http://" + "a" * 64 + ".com/", "http://" + ".".join(["abcdefghi"] * 30) + ".com/", "http://exämple\u0000.com/",
    "http://exa%6Dple.com/", "http://exa%2emple.com/", "http://example.com%2f@evil.com/", "http://example.com:80%2f/",
]
UNICODE = [
    "http://127。0。0。1/", "http://127．0．0．1/", "http://ｌｏｃａｌｈｏｓｔ/",
    "http://１２７.0.0.1/", "http://①②⑦.0.0.1/", "http://ｌｏｃａｌｈｏｓｔ./",
    "http://ｅｘａｍｐｌｅ/", "http://xn--/", "http://xn--a.com/", "http://​.example.com/",
]
IPV6_FORMS = [
    "http://[::1%25eth0]/", "http://[fe80::1%eth0]/", "http://[::1/", "http://::1/", "http://[example.com]/", "http://[1.2.3.4]/",
    "http://[::1]x/", "http://[::1]:80x/", "http://[]/", "http://[:::1]/", "http://[::ffff:1.2.3]/", "http://[g::1]/",
]
CHARACTERS += ["https://exa­mple.com/", "https://example.com/⁠", "https://example.com/‮", "https://example.com/‎",
               "https://example.com/﻿", "https://exa‍mple.com/"]  # RF55: format characters (Cf) in the raw string
NAMES += ["http://db.internal/", "http://x.home.arpa/", "http://nas.localdomain/", "http://A.INTERNAL./"]  # RF56
BLOCKED_GROUPS = {
    "scheme": SCHEME, "userinfo": USERINFO, "characters": CHARACTERS, "ports": PORTS, "literals": LITERALS,
    "embedded": EMBEDDED, "ambiguous": AMBIGUOUS, "names": NAMES, "unicode": UNICODE, "ipv6_forms": IPV6_FORMS,
}
GROUP_REASONS = {  # RF54: the reason must be the right one for the group, not just any reason
    "scheme": {"scheme", "empty_host"}, "userinfo": {"userinfo"}, "characters": {"bad_char", "too_long"},
    "ports": {"bad_port", "port_not_allowed", "bad_authority"}, "literals": {"ip_not_public", "port_not_allowed"},
    "embedded": {"ip_not_public"}, "ambiguous": {"ambiguous_host", "empty_host", "ip_not_public"},
    "names": {"local_name", "single_label", "bad_label", "host_too_long", "bad_char", "percent_in_host", "userinfo", "bad_port"},
    "unicode": {"ip_not_public", "local_name", "single_label", "idna", "bad_label", "bad_char"},
    "ipv6_forms": {"bad_ip_literal", "bad_authority", "percent_in_host", "bad_port"},
}
BLOCKED = [(group, url) for group, urls in BLOCKED_GROUPS.items() for url in urls]


@pytest.mark.parametrize("raw,url,host,port,is_ip", ALLOWED, ids=[a[0][:50] for a in ALLOWED])
def test_allowed_url_is_normalised(raw, url, host, port, is_ip):
    got = check_url(raw)
    assert (got.url, got.host, got.port, got.is_ip) == (url, host, port, is_ip)
    assert got.url.isascii() and "#" not in got.url


@pytest.mark.parametrize("group,raw", BLOCKED, ids=["%s:%s" % (g, u[:45]) for g, u in BLOCKED])
def test_ssrf_group_is_blocked_with_url_blocked_and_a_fixed_reason(group, raw):
    with pytest.raises(Blocked) as info:
        check_url(raw)
    assert info.value.code == "URL_BLOCKED" and info.value.reason in GROUP_REASONS[group], info.value.reason


def test_the_ssrf_groups_are_big_enough():
    assert sum(len(urls) for urls in BLOCKED_GROUPS.values()) >= 150


def test_a_url_of_exactly_2048_chars_is_allowed_and_2049_is_blocked():
    assert len(LONG_OK) == 2048 and check_url(LONG_OK).url == LONG_OK
    with pytest.raises(Blocked):
        check_url(LONG_OK + "a")


@pytest.mark.parametrize("bad", [None, 5, b"https://example.com/", ["https://example.com/"], object()])
def test_a_non_string_is_blocked_not_an_exception(bad):
    with pytest.raises(Blocked):
        check_url(bad)


def test_check_url_never_raises_anything_but_blocked():
    nasty = ["http://" + "\U0001f600" * 40 + ".com/", "https://\ud800.com/", "http://[::1\ud800]/", "https://example.com/\ud800",
             "http://xn--" + "a" * 70 + ".com/", "https://" + "é" * 100 + ".com/", "http://1̸.com/"]
    for raw in nasty:
        try:
            check_url(raw)
        except Blocked:
            pass


# --- the two parsers must agree (ours and httpx.URL) ---------------------------------------------------------
def test_ours_and_httpx_agree_on_host_and_port_for_every_allowed_url():
    for raw, url, host, port, is_ip in ALLOWED:
        got = check_url(raw)
        parsed = httpx.URL(got.url)
        assert parsed.raw_host.decode("ascii") == host, raw
        default = 443 if got.scheme == "https" else 80
        assert parsed.port == (None if port == default else port), raw


def test_for_ascii_urls_httpx_on_the_raw_text_reads_the_same_host_as_we_do():
    for raw, url, host, port, is_ip in ALLOWED:
        if not raw.isascii():
            continue
        try:
            parsed = httpx.URL(raw)
        except httpx.InvalidURL:
            continue
        theirs = parsed.raw_host.decode("ascii").lower().rstrip(".")
        if ":" in host:  # IPv6: compare as addresses, spellings differ
            import ipaddress
            assert ipaddress.ip_address(theirs) == ipaddress.ip_address(host), raw
        else:
            assert theirs == host, raw


def test_a_parser_mismatch_is_url_blocked_not_a_crash(monkeypatch):
    class Other(httpx.URL):
        @property
        def raw_host(self):
            return b"evil.example"

    monkeypatch.setattr(urlpolicy.httpx, "URL", Other)
    with pytest.raises(Blocked) as info:
        check_url("https://example.com/")
    assert info.value.code == "URL_BLOCKED" and info.value.reason == "parser_mismatch"


def test_a_port_mismatch_is_url_blocked(monkeypatch):
    class Other(httpx.URL):
        @property
        def port(self):
            return 8081

    monkeypatch.setattr(urlpolicy.httpx, "URL", Other)
    with pytest.raises(Blocked) as info:
        check_url("https://example.com/")
    assert info.value.reason == "parser_mismatch"


def test_an_ipv6_host_read_as_something_else_is_a_mismatch(monkeypatch):
    class Other(httpx.URL):
        @property
        def raw_host(self):
            return b"not-an-address"

    monkeypatch.setattr(urlpolicy.httpx, "URL", Other)
    with pytest.raises(Blocked) as info:
        check_url("https://[2606:4700:4700::1111]/")
    assert info.value.reason == "parser_mismatch"


def test_an_httpx_parse_failure_is_url_blocked(monkeypatch):
    def boom(_text):
        raise httpx.InvalidURL("x")

    monkeypatch.setattr(urlpolicy.httpx, "URL", boom)
    with pytest.raises(Blocked) as info:
        check_url("https://example.com/")
    assert info.value.reason == "parser_error"


# --- policy injection (tests only; never reachable from the command line) -----------------------------------
def test_default_policy_is_strict():
    policy = Policy()
    assert policy.ports == (80, 443) and policy.ip_ok is is_public_ip and policy.resolver is socket.getaddrinfo
    with pytest.raises(Exception):
        policy.ports = (1,)  # frozen


def test_default_policy_blocks_a_loopback_fixture_address_and_port():
    with pytest.raises(Blocked):
        check_url("http://127.0.0.1:34567/x")


def test_an_injected_policy_can_allow_the_fixture_address_and_port():
    policy = Policy(ports=(34567,), ip_ok=lambda address: True)
    got = check_url("http://127.0.0.1:34567/x", policy)
    assert (got.host, got.port, got.is_ip, got.url) == ("127.0.0.1", 34567, True, "http://127.0.0.1:34567/x")


def test_an_injected_policy_keeps_the_scheme_default_port_rule():
    policy = Policy(ports=(5555,), ip_ok=lambda address: True)
    with pytest.raises(Blocked):
        check_url("https://example.com/", policy)  # 443 is not in (5555,)
    assert check_url("https://example.com:5555/", policy).port == 5555


def test_an_injected_ip_ok_that_refuses_blocks_a_public_literal():
    with pytest.raises(Blocked):
        check_url("https://8.8.8.8/", Policy(ip_ok=lambda address: False))


def test_names_like_localhost_stay_blocked_even_when_ip_ok_allows_everything():
    for raw in ("http://localhost/", "http://x.localhost/", "http://printer.local/", "http://intranet/"):
        with pytest.raises(Blocked):
            check_url(raw, Policy(ports=(80,), ip_ok=lambda address: True))


# --- is_public_ip --------------------------------------------------------------------------------------------
PUBLIC = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808"]
NOT_PUBLIC = [
    "127.0.0.1", "10.1.2.3", "172.16.5.5", "192.168.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "255.255.255.255",
    "224.0.0.1", "239.255.255.250", "192.0.2.1", "198.51.100.1", "203.0.113.1", "198.18.0.1", "240.0.0.1",
    "::1", "::", "fe80::1", "fec0::1", "fc00::1", "fd12::1", "ff02::1", "ff05::2", "2001:db8::1", "2001::1",
    "::ffff:127.0.0.1", "::ffff:7f00:1", "::ffff:10.0.0.1", "::ffff:0:127.0.0.1", "::7f00:1", "::a00:1", "::808:808",
    "64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::a9fe:a9fe", "2002:7f00:1::", "2002:a00:1::", "2002:a9fe:a9fe::",
    "1::1", "4000::1", "fe80::1%eth0", "::1%1", "",
    "not-an-ip", "127.1", "1.2.3", "0x7f.1", "2130706433", "1.2.3.4.5", "01.2.3.4", " 8.8.8.8", "8.8.8.8 ", "[::1]", "8.8.8.8/32",
]


@pytest.mark.parametrize("address", PUBLIC)
def test_public_addresses(address):
    assert is_public_ip(address) is True


@pytest.mark.parametrize("address", NOT_PUBLIC)
def test_non_public_addresses(address):
    assert is_public_ip(address) is False


@pytest.mark.parametrize("bad", [None, 5, b"8.8.8.8", ["8.8.8.8"], 134744072])
def test_is_public_ip_of_a_non_string_is_false(bad):
    assert is_public_ip(bad) is False


def test_ipv4_embedded_in_ipv6_is_judged_as_the_ipv4_with_python_own_flags_not_trusted_raw():
    """Measured 13/11/2026: ipaddress.ip_address("64:ff9b::7f00:1").is_global is True, so the raw flag cannot be used."""
    import ipaddress
    assert ipaddress.ip_address("64:ff9b::7f00:1").is_global is True
    assert is_public_ip("64:ff9b::7f00:1") is False


# --- dns_probe -----------------------------------------------------------------------------------------------
def infos(*addresses):
    return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in addresses]


def test_dns_probe_all_public_is_ok():
    assert dns_probe("example.com", resolver=lambda *a, **k: infos("8.8.8.8", "2606:4700:4700::1111")) == urlpolicy.DNS_OK


def test_dns_probe_one_non_public_address_blocks_the_host():
    assert dns_probe("example.com", resolver=lambda *a, **k: infos("8.8.8.8", "10.0.0.1")) == urlpolicy.DNS_BLOCKED
    assert dns_probe("example.com", resolver=lambda *a, **k: infos("64:ff9b::7f00:1")) == urlpolicy.DNS_BLOCKED


def test_dns_probe_failure_or_empty_answer_is_unresolved():
    def boom(*a, **k):
        raise OSError("no such host")

    assert dns_probe("example.com", resolver=boom) == urlpolicy.DNS_FAIL
    assert dns_probe("example.com", resolver=lambda *a, **k: []) == urlpolicy.DNS_FAIL
    assert dns_probe("", resolver=lambda *a, **k: infos("8.8.8.8")) == urlpolicy.DNS_FAIL


def test_dns_probe_gives_up_after_the_timeout_without_hanging():
    def slow(*a, **k):
        time.sleep(1.0)
        return infos("8.8.8.8")

    started = time.monotonic()
    assert dns_probe("example.com", timeout=0.05, resolver=slow) == urlpolicy.DNS_FAIL
    assert time.monotonic() - started < 0.8


def test_dns_probe_uses_the_injected_address_check():
    assert dns_probe("example.com", resolver=lambda *a, **k: infos("10.0.0.1"), ip_ok=lambda a: True) == urlpolicy.DNS_OK


def test_real_getaddrinfo_of_localhost_is_blocked():
    """A real library call (no fake): the hosts file answers loopback, which must never be public."""
    assert dns_probe("localhost") == urlpolicy.DNS_BLOCKED


# --- redact and url_sha256 -----------------------------------------------------------------------------------
def test_redact_hides_every_query_value_and_drops_fragment():
    assert redact("https://a.com/p?token=SECRET&x=1#frag") == "https://a.com/p?token=REDACTED&x=REDACTED"
    assert "SECRET" not in redact("https://a.com/p?token=SECRET#SECRET")


def test_redact_hides_parameters_without_an_equals_sign_too():
    out = redact("https://a.com/p?SECRETTOKEN")
    assert "SECRET" not in out and out.endswith("?REDACTED")
    assert redact("https://a.com/p?a&b=2&c=") == "https://a.com/p?REDACTED&b=REDACTED&c=REDACTED"


def test_redact_removes_userinfo():
    out = redact("https://user:pw@a.com/x")
    assert "pw" not in out and "user" not in out and out == "https://a.com/x"


def test_redact_strips_control_characters_and_bidi_marks():
    out = redact("https://a.com/p\nINJECT‮?x=\r\n1")
    assert "\n" not in out and "\r" not in out and "‮" not in out


def test_redact_is_length_bounded_and_survives_garbage():
    assert len(redact("https://a.com/" + "a" * 5000)) <= 2048
    assert redact(None) == "" and redact(5) == ""
    assert redact("not a url at all?x=SECRET").count("SECRET") == 0


def test_redact_accepts_a_normalised_url():
    assert redact(check_url("https://a.com/p?k=SECRET")) == "https://a.com/p?k=REDACTED"


def test_redact_known_limit_a_token_inside_the_path_is_not_hidden():
    assert "TOKEN123" in redact("https://a.com/download/TOKEN123/file.pdf")


def test_redact_also_covers_a_location_header_value():
    assert "pw" not in redact("https://u:pw@evil.example/next?sig=ABC") and "ABC" not in redact("https://u:pw@evil.example/next?sig=ABC")


def test_url_sha256_is_stable_over_equivalent_spellings():
    a = url_sha256("HTTPS://EXAMPLE.com:443/a#x")
    assert a == url_sha256("https://example.com/a") == url_sha256(check_url("https://example.com/a"))
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)
    assert a == hashlib.sha256(b"https://example.com/a").hexdigest()


def test_url_sha256_differs_for_different_queries_and_hashes_blocked_text_too():
    assert url_sha256("https://example.com/a?x=1") != url_sha256("https://example.com/a?x=2")
    blocked = url_sha256("http://127.0.0.1/")
    assert len(blocked) == 64 and blocked != url_sha256("http://127.0.0.2/")
    assert len(url_sha256("\ud800 not a url")) == 64 and len(url_sha256(None)) == 64


# --- review round: RF50, RF51, RF52, RF53, RF57, RF59 ---------------------------------------------------------
@pytest.mark.parametrize("raw", [
    "https://user:pa?ss@a.com/x",
    "https://user:pa/ss@a.com/x",
    "https://user:pa#ss@a.com/x",
    "https://user:p?a/s#s@a.com/x?k=v",
    "https://user:pass@a.com/x",
    "//user:pw@a.com/x",
    "https://us@er:pw@a.com/x",
])
def test_redact_never_prints_a_password_even_when_it_holds_a_delimiter(raw):
    out = redact(raw)
    for secret in ("pa", "ss", "pw", "user", "us", "er"):
        assert secret not in out.replace("a.com", "").replace("https", "").replace("/x", "").replace("REDACTED", ""), (raw, out)
    assert "a.com" in out


def test_redact_exact_outputs_for_the_delimiter_cases():
    assert redact("https://user:pa?ss@a.com/x") == "https://a.com/x"
    assert redact("https://user:pa/ss@a.com/x") == "https://a.com/x"
    assert redact("https://user:pa#ss@a.com/x") == "https://a.com/x"
    assert redact("//user:pw@a.com/x") == "//a.com/x"
    assert redact("https://user:pw@a.com/x?k=v") == "https://a.com/x?k=REDACTED"


def test_redact_keeps_a_legitimate_at_sign_in_the_path_or_query_and_a_port():
    assert redact("https://a.com/@user/x") == "https://a.com/@user/x"
    assert redact("https://a.com:443/p?e=x@y") == "https://a.com:443/p?e=REDACTED"
    assert redact("https://a.com/p?e=x@y") == "https://a.com/p?e=REDACTED"


def test_redact_gives_up_on_an_at_sign_it_cannot_parse():
    assert redact("https://u:pw@/x") == "https://REDACTED/"
    assert redact("user:pw@a.com/x") == "REDACTED"
    assert "pw" not in redact("ftp:user:pw@a.com")


def test_url_sha256_of_a_blocked_url_never_depends_on_its_password():
    one = url_sha256("http://user:one@127.0.0.1/x")
    assert one == url_sha256("http://user:two@127.0.0.1/x") == hashlib.sha256(b"http://127.0.0.1/x").hexdigest()
    assert url_sha256("https://user:pa?ss@10.0.0.1/x") == url_sha256("https://user:other?word@10.0.0.1/x")
    assert url_sha256("user:pw1@a.com/x") == url_sha256("user:pw2@a.com/x")


def test_the_length_limit_is_checked_again_after_normalisation():
    raw = "https://example.com/" + "\u00fc" * 500
    assert len(raw) < 2048
    with pytest.raises(Blocked) as info:
        check_url(raw)
    assert info.value.reason == "too_long"
    assert len(check_url("https://example.com/" + "\u00fc" * 300).url) < 2048


def test_the_normalised_url_is_the_only_text_ever_given_to_httpx(monkeypatch):
    """RF57 (for Slice B, TK13): connect to NormalizedUrl.url, never to the raw text."""
    seen = []
    real = httpx.URL

    def spy(text):
        seen.append(text)
        return real(text)

    monkeypatch.setattr(urlpolicy.httpx, "URL", spy)
    got = check_url("HTTPS://Example.COM:443/A?x=\u00e9#frag")
    assert seen == [got.url] and got.url == "https://example.com/A?x=%C3%A9"


def test_dns_probe_survives_errors_that_are_not_oserror():
    def boom(*a, **k):
        raise UnicodeError("label too long")

    assert dns_probe("example.com", resolver=boom) == urlpolicy.DNS_FAIL
    assert dns_probe("a" * 64 + ".com") == urlpolicy.DNS_FAIL  # a real resolver call: the IDNA codec refuses first
    assert dns_probe("bad\ud800.com") == urlpolicy.DNS_FAIL


def test_dns_probe_treats_a_malformed_answer_as_blocked():
    assert dns_probe("example.com", resolver=lambda *a, **k: [("x",)]) == urlpolicy.DNS_BLOCKED
    assert dns_probe("example.com", resolver=lambda *a, **k: [(0, 0, 0, "", None)]) == urlpolicy.DNS_BLOCKED
    assert dns_probe("example.com", resolver=lambda *a, **k: [(0, 0, 0, "", ())]) == urlpolicy.DNS_BLOCKED


def test_redact_with_a_normalised_url_prints_its_normalised_host():
    """SPR-03 FR16.2: the host in `url_redacted` is the normalised one when the URL normalised."""
    raw = "https://WWW.Example.COM./a?t=1"
    assert redact(raw, check_url(raw)) == "https://www.example.com/a?t=REDACTED"
    assert redact(raw) == "https://WWW.Example.COM./a?t=REDACTED"  # without `norm` the old way
