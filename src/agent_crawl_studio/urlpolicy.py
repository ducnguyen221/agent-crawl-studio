"""URL and IP policy (profile `strict`, the only one; SPEC FR2.1-FR2.3, FR5.8).

Pure functions: `check_url` never resolves a name and never opens a socket. Everything that can fail
is a `Blocked` (code URL_BLOCKED, a reason from a closed list), never another exception.
The policy object is injected by code only (tests); nothing here reads the environment."""

import hashlib
import ipaddress
import re
import socket
import threading
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote

import httpx
import idna

DNS_OK, DNS_FAIL, DNS_BLOCKED = "ok", "unresolved", "blocked"
DNS_TIMEOUT = 5
MAX_URL_LENGTH = 2048
MAX_HOST_LENGTH = 253
MAX_LABEL_LENGTH = 63

BLOCK_REASONS = (
    "not_a_string", "too_long", "bad_char", "scheme", "empty_host", "userinfo", "bad_authority", "bad_port",
    "port_not_allowed", "percent_in_host", "bad_ip_literal", "ip_not_public", "idna", "bad_label", "host_too_long",
    "ambiguous_host", "single_label", "local_name", "parser_mismatch", "parser_error", "media_host",
)

LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".localdomain")

_PORT = re.compile(r"[0-9]{1,5}")
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
_QUAD_PART = re.compile(r"0|[1-9][0-9]{0,2}")
_AUTHORITY = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)([^/?]*)(.*)$", re.DOTALL)

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIXTOFOUR = ipaddress.ip_network("2002::/16")
_GLOBAL_UNICAST = ipaddress.ip_network("2000::/3")


class Blocked(Exception):
    """The URL (or address) is refused by the strict profile."""

    code = "URL_BLOCKED"

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def is_public_ip(address):
    """True only for a globally routable address. An IPv4 embedded in an IPv6 address is stripped out and
    judged as an IPv4 (Python's own `is_global` of a raw IPv6 such as 64:ff9b::7f00:1 is True, so it is not
    trusted on its own); the IPv4-compatible range ::/96 is refused whole."""
    if not isinstance(address, str) or "%" in address:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return _public(ip)


def _embedded_v4(ip):
    """The IPv4 inside an IPv6 address, "deny" for a range that is refused whole, or None."""
    number = int(ip)
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if number >> 32 == 0xFFFF0000:  # ::ffff:0:0/96 (SIIT)
        return ipaddress.IPv4Address(number & 0xFFFFFFFF)
    if number >> 32 == 0:  # ::/96, IPv4-compatible and ::1 / ::
        return "deny"
    if ip in _NAT64:
        return ipaddress.IPv4Address(number & 0xFFFFFFFF)
    if ip in _SIXTOFOUR:
        return ipaddress.IPv4Address((number >> 80) & 0xFFFFFFFF)
    return None


def _public(ip):
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_v4(ip)
        if inner == "deny":
            return False
        if inner is not None:
            ip = inner
    if isinstance(ip, ipaddress.IPv4Address):
        return bool(ip.is_global) and not (ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_reserved
                                           or ip.is_link_local or ip.is_private)
    if ip not in _GLOBAL_UNICAST:
        return False
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_reserved
                                       or ip.is_link_local or ip.is_site_local or ip.is_private)


@dataclass(frozen=True)
class Policy:
    """Injected by code, never by option, environment, config or request (SPEC FR2.8)."""

    ports: tuple = (80, 443)
    ip_ok: object = is_public_ip
    resolver: object = socket.getaddrinfo


@dataclass(frozen=True)
class NormalizedUrl:
    url: str
    scheme: str
    host: str
    port: int
    target: str
    is_ip: bool


def _check_raw(text):
    if not isinstance(text, str):
        raise Blocked("not_a_string")
    if len(text) > MAX_URL_LENGTH:
        raise Blocked("too_long")
    for ch in text:
        if ch in "\\ " or unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zs", "Zl", "Zp"):
            raise Blocked("bad_char")


def _split(text):
    """(scheme, authority, target) from a raw URL whose fragment is already gone."""
    scheme, sep, rest = text.partition("://")
    if not sep or scheme.lower() not in ("http", "https"):
        raise Blocked("scheme")
    cut = len(rest)
    for mark in "/?":
        at = rest.find(mark)
        if at != -1:
            cut = min(cut, at)
    authority, target = rest[:cut], rest[cut:]
    if not authority:
        raise Blocked("empty_host")
    if "@" in authority:
        raise Blocked("userinfo")
    return scheme.lower(), authority, target


def _split_authority(authority):
    """(host text, is bracketed IPv6, port text or None)."""
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            raise Blocked("bad_authority")
        tail = authority[end + 1:]
        if tail == "":
            return authority[1:end], True, None
        if not tail.startswith(":"):
            raise Blocked("bad_authority")
        return authority[1:end], True, tail[1:]
    if authority.count(":") > 1:
        raise Blocked("bad_authority")
    host, colon, port = authority.partition(":")
    return host, False, (port if colon else None)


def _port(text, scheme, policy):
    if text is None:
        port = 443 if scheme == "https" else 80
    else:
        if not _PORT.fullmatch(text) or str(int(text)) != text:
            raise Blocked("bad_port")
        port = int(text)
    if port not in policy.ports:
        raise Blocked("port_not_allowed")
    return port


def _ascii_host(host):
    if not host.isascii():
        try:
            host = idna.encode(host, uts46=True).decode("ascii")
        except (idna.IDNAError, UnicodeError):
            raise Blocked("idna") from None
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host:
        raise Blocked("empty_host")
    if len(host) > MAX_HOST_LENGTH:
        raise Blocked("host_too_long")
    labels = host.split(".")
    for label in labels:
        if len(label) > MAX_LABEL_LENGTH or not _LABEL.fullmatch(label):
            raise Blocked("bad_label")
        if label.startswith("xn--"):
            try:
                idna.decode(label)
            except (idna.IDNAError, UnicodeError):
                raise Blocked("idna") from None
    return host, labels


def _quad(labels):
    return len(labels) == 4 and all(_QUAD_PART.fullmatch(part) and int(part) <= 255 for part in labels)


def _host(host_text, bracketed, policy):
    """(display host, bare host, is_ip). Raises Blocked."""
    if "%" in host_text:
        raise Blocked("percent_in_host")
    if bracketed:
        try:
            ip = ipaddress.IPv6Address(host_text)
        except ValueError:
            raise Blocked("bad_ip_literal") from None
        if not policy.ip_ok(str(ip)):
            raise Blocked("ip_not_public")
        return "[" + ip.compressed + "]", ip.compressed, True
    if not host_text:
        raise Blocked("empty_host")
    host, labels = _ascii_host(host_text)
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        if not _quad(labels):
            raise Blocked("ambiguous_host")
        if not policy.ip_ok(host):
            raise Blocked("ip_not_public")
        return host, host, True
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        raise Blocked("local_name")
    if len(labels) < 2:
        raise Blocked("single_label")
    return host, host, False


def _same_host(ours, theirs):
    if ":" in ours:
        try:
            return ipaddress.ip_address(theirs) == ipaddress.ip_address(ours)
        except ValueError:
            return False
    return ours == theirs


def check_url(raw, policy=None):
    """Normalise a URL and apply the strict profile. Returns NormalizedUrl or raises Blocked."""
    policy = policy or Policy()
    _check_raw(raw)
    text = raw.split("#", 1)[0]
    scheme, authority, target = _split(text)
    host_text, bracketed, port_text = _split_authority(authority)
    port = _port(port_text, scheme, policy)
    display, bare, is_ip = _host(host_text, bracketed, policy)
    if target == "":
        target = "/"
    elif target.startswith("?"):
        target = "/" + target
    target = quote(target, safe="%/?&=:@!$'()*+,;~-._[]")
    default_port = 443 if scheme == "https" else 80
    netloc = display if port == default_port else "%s:%d" % (display, port)
    url = "%s://%s%s" % (scheme, netloc, target)
    if len(url) > MAX_URL_LENGTH:  # percent-encoding can make a short raw text long
        raise Blocked("too_long")
    try:
        parsed = httpx.URL(url)
        theirs = parsed.raw_host.decode("ascii").lower()
        their_port = parsed.port
        their_scheme = parsed.scheme
        their_userinfo = parsed.userinfo
    except Exception:  # noqa: BLE001 - any parse failure of the second parser is a block, never a crash
        raise Blocked("parser_error") from None
    if (not _same_host(bare, theirs) or their_port != (None if port == default_port else port)
            or their_scheme != scheme or their_userinfo):
        raise Blocked("parser_mismatch")
    return NormalizedUrl(url, scheme, bare, port, target, is_ip)


def dns_probe(host, timeout=DNS_TIMEOUT, resolver=None, ip_ok=None, port=443):
    """DNS_OK when the host resolves and EVERY address is public; DNS_BLOCKED when any is not;
    DNS_FAIL when it does not resolve (or not within `timeout` seconds)."""
    if not host:
        return DNS_FAIL
    resolver = resolver or socket.getaddrinfo
    ip_ok = ip_ok or is_public_ip
    box = {}

    def lookup():
        try:
            box["infos"] = resolver(host, port, proto=socket.IPPROTO_TCP)
        except Exception:  # noqa: BLE001 - UnicodeError, ValueError... from the resolver are "did not resolve"
            box["infos"] = None

    worker = threading.Thread(target=lookup, daemon=True)
    worker.start()
    worker.join(timeout)
    infos = box.get("infos")
    if not infos:
        return DNS_FAIL
    try:
        return DNS_OK if all(ip_ok(info[4][0]) for info in infos) else DNS_BLOCKED
    except Exception:  # noqa: BLE001 - a malformed answer is never trusted
        return DNS_BLOCKED


def _clean(text):
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Zl", "Zp"))


_PARAM_NAME = re.compile(r"[A-Za-z0-9_.\[\]-]{1,32}")
_PREFIX = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//")
_USER_PASSWORD = re.compile(r"[^:]*:(?!\d*$)")


def _strip_userinfo(text):
    """`text` without any userinfo, decided on the WHOLE string before it is cut at ?, / or #: a password may
    hold those characters. Returns None when an "@" is there but cannot be understood."""
    found = _PREFIX.match(text)
    if not found:
        return None if "@" in text else text
    prefix, rest = found.group(0), text[found.end():]
    ends = [i for i in (rest.find(mark) for mark in "/?#") if i != -1]
    naive = rest[:min(ends)] if ends else rest
    if "@" in naive:
        if "@" in rest[len(naive):]:  # a password holding "@" and a delimiter: where it ends cannot be known
            return None
        rest = rest[naive.rfind("@") + 1:]
    elif "@" in rest and _USER_PASSWORD.match(naive):  # "user:pa?ss@host": the delimiter is inside the password
        rest = rest[rest.rfind("@") + 1:]
    else:
        return text
    cut = [i for i in (rest.find(mark) for mark in "/?#") if i != -1]
    if not rest[:min(cut)] if cut else not rest:
        return None
    return prefix + rest


def redact(url):
    """A URL safe to print: no userinfo, no fragment, no control or bidi characters, and every query
    parameter's value (or the whole parameter when it has no "=") replaced by REDACTED, at most 2048
    characters. A userinfo that cannot be parsed gives scheme://REDACTED/ (or REDACTED). A parameter NAME is
    printed only when it is short and plain (letters, digits, _ . [ ] -, at most 32); otherwise the whole
    parameter is REDACTED. Documented limit: a token inside the PATH, including a path parameter such as
    ";jsessionid=..." (the part after ";"), cannot be hidden."""
    text = getattr(url, "url", url)
    if not isinstance(text, str):
        return ""
    text = _strip_userinfo(_clean(text))
    if text is None:
        found = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*:)?//", _clean(getattr(url, "url", url)))
        return (found.group(0) + "REDACTED/") if found else "REDACTED"
    head, mark, query = text.split("#", 1)[0].partition("?")
    if mark and query:
        parts = []
        for part in query.split("&"):
            name, equals, _value = part.partition("=")
            parts.append(name + "=REDACTED" if equals and _PARAM_NAME.fullmatch(name) else "REDACTED")
        head = head + "?" + "&".join(parts)
    return head[:MAX_URL_LENGTH]


def url_sha256(url, policy=None):
    """SHA-256 (hex) of the normalised URL; a URL the policy refuses is hashed as written (without fragment),
    so a blocked item still has a stable identifier."""
    if isinstance(url, NormalizedUrl):
        text = url.url
    elif isinstance(url, str):
        try:
            text = check_url(url, policy).url
        except Blocked:
            stripped = _strip_userinfo(_clean(url))  # never hash a password
            text = "REDACTED" if stripped is None else stripped.split("#", 1)[0]
    else:
        text = ""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
