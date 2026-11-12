"""manifest.json = result v1, rewritten after every item through a temp file and `os.replace` (SPEC FR5.6, FR5.7).
Any reader sees a complete, valid document at any moment. Text that came from the source is cleaned here."""

import contextlib
import json
import os
import secrets
import time
import unicodedata

from .artifacts import RETRY_DELAYS, retry_io  # noqa: F401 - RETRY_DELAYS is part of this module's contract
from .winpath import PathBlocked

MAX_UNTRUSTED = 200
STATUSES = ("succeeded", "failed", "blocked", "cancelled", "skipped")


class ManifestError(Exception):
    """manifest.json could not be rewritten (OUTPUT_WRITE_FAILED); the previous file is intact."""


def untrusted_text(value):
    """Text from the source made safe to store: no control, format, surrogate or line-separator characters
    (U+0000-001F, U+007F-009F, bidi marks, BOM...), at most 200 characters."""
    if not isinstance(value, str):
        return ""
    kept = "".join(ch for ch in value if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Zl", "Zp"))
    return kept[:MAX_UNTRUSTED]


def clean_untrusted(values):
    cleaned = {key: untrusted_text(value) for key, value in values.items()}
    return {key: value for key, value in cleaned.items() if value}


class ManifestWriter:
    def __init__(self, job_dir, *, replace=os.replace, sleep=time.sleep, check=None, token_of=None):
        self.path = os.path.join(job_dir, "manifest.json")
        self._replace, self._sleep, self._check = replace, sleep, check
        self._token_of = token_of or (lambda: secrets.token_hex(4))
        self.doc = None
        self._total = 0

    def _counts(self):
        counts = {"total": self._total}
        counts.update({status: 0 for status in STATUSES})
        for item in self.doc["items"]:
            counts[item["status"]] += 1
        return counts

    def _write(self):
        temp = "%s.%s.tmp" % (self.path, self._token_of())  # unique, and created exclusively: never someone else's file
        made = False
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            made = True
            with os.fdopen(fd, "wb") as handle:
                handle.write(json.dumps(self.doc, ensure_ascii=True).encode("ascii"))
            if self._check is not None:
                self._check(temp)
            retry_io(lambda: self._replace(temp, self.path), self._sleep)
        except (OSError, PathBlocked):
            if made:
                with contextlib.suppress(OSError):
                    os.remove(temp)
            raise ManifestError() from None

    def start(self, job_id, started_at, limits_effective, total, warnings=()):
        self._total = total
        self.doc = {"schema_version": 1, "kind": "result", "job_id": job_id, "status": None, "started_at": started_at,
                    "finished_at": None, "counts": None, "limits_effective": limits_effective, "items": [],
                    "warnings": list(warnings)}
        self.doc["counts"] = self._counts()
        self._write()

    def add_item(self, item, write=True):
        self.doc["items"].append(item)
        self.doc["counts"] = self._counts()
        if write:
            self._write()

    def finish(self, status, finished_at, error=None, warnings=(), write=True):
        self.doc["status"], self.doc["finished_at"] = status, finished_at
        if error is not None:
            self.doc["error"] = error
        self.doc["warnings"] += [warning for warning in warnings if warning not in self.doc["warnings"]]
        self.doc["counts"] = self._counts()
        if write:
            self._write()
