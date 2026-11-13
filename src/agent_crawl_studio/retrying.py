"""The policy for trying an item again inside the job (SPEC FR14, PLAN DN13). Pure: no clock, no sleep, no network.

What is tried again is only a trouble that can pass by itself: a network or server error marked `retryable`, a
`RATE_LIMITED` answer of the direct adapter, and a `TIMEOUT` of the connect or the idle wait. A `TIMEOUT` of the item
or of the job, a `RATE_LIMITED` of the yt-dlp adapter (it stays `retryable: true` for the `retry` command), a 403, a
blocked URL, a size limit and every other code are final. An `extract` item is tried again only for the trouble of its
download (the same as `direct-http`); what goes wrong in the extraction itself (`TIMEOUT` of kind `extract`,
`TOOL_FAILED`, `CONTENT_MISMATCH`) is final."""

import email.utils
import re
from datetime import timezone

BACKOFF = (2, 4, 8, 16, 32)  # seconds to wait before attempt 2, 3, ... (the last one repeats)
RETRY_AFTER_CAP = 120  # a `Retry-After` beyond this many seconds ends the tries
RETRY_AFTER_STATUSES = (429, 503)
TRANSIENT_TIMEOUTS = ("connect", "idle")
DOWNLOAD_ADAPTERS = ("direct-http", "extract")  # the `extract` adapter downloads its page the direct way (FR18.7)
SECONDS = re.compile(r"[0-9]{1,9}")


def should_retry(entry, adapter, timeout_kind):
    """True when the item `entry` (a manifest item) ended in a trouble worth another attempt (FR14.2, FR14.5).
    `timeout_kind` says which wait ran out when the code is TIMEOUT: connect, idle, item or job (None: unknown)."""
    if entry.get("status") != "failed":
        return False
    error = entry.get("error") or {}
    code = error.get("code")
    if code == "NETWORK_ERROR":
        return error.get("retryable") is True
    if code == "RATE_LIMITED":
        return adapter in DOWNLOAD_ADAPTERS
    if code == "TIMEOUT":
        return timeout_kind in TRANSIENT_TIMEOUTS
    return False


def _retry_after(text, now):
    """Seconds from a `Retry-After` value (a whole number or an HTTP date), or None when it cannot be read."""
    text = (text or "").strip()
    if SECONDS.fullmatch(text):
        return int(text)
    try:
        moment = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max((moment - now).total_seconds(), 0)


def wait_seconds(attempt, retry_after, now):
    """Seconds to wait after attempt number `attempt` (1-based) failed, or None to stop trying (FR14.2, FR14.3).
    A `Retry-After` (`retry_after`, the text; `now`, an aware datetime) replaces the backoff when it is at most
    RETRY_AFTER_CAP seconds; a longer or unreadable one ends the tries."""
    if retry_after is None:
        return BACKOFF[min(max(attempt, 1), len(BACKOFF)) - 1]
    seconds = _retry_after(retry_after, now)
    if seconds is None or seconds > RETRY_AFTER_CAP:
        return None
    return seconds
