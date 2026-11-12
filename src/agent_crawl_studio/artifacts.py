"""Artifact manager (SPEC FR5.1-FR5.5, FR4.5): `.part` files, an atomic no-overwrite rename into `files/`, hash and
size of what is on disk, MIME from the bytes, a safe extension, Mark-of-the-Web (best effort).

A name is `<item id>-<10 hex of the sha256>[-N].<extension>`: never a title, a URL segment or a Content-Disposition
name. Nothing half-written is ever in `files/`, and a finished file is never overwritten by this code."""

import contextlib
import hashlib
import os
import re
import time

import httpx

from . import sniff

RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4)  # five tries, the pause doubles
SHARING_VIOLATIONS = (5, 32)  # WinError: access denied / sharing violation (an anti-virus scanner, an indexer)
MAX_SUFFIX = 99
HEAD_KEPT = 512
DECLARED_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
MAX_DECLARED = 100


class ArtifactError(Exception):
    """The file cannot be written (OUTPUT_WRITE_FAILED). `reason` is a code, never text from the source."""

    code = "OUTPUT_WRITE_FAILED"

    def __init__(self, reason="os_error"):
        super().__init__(reason)
        self.reason = reason


def retry_io(operation, sleep):
    """Run `operation`; a sharing violation is retried after growing pauses, anything else is raised at once."""
    for delay in RETRY_DELAYS:
        try:
            return operation()
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in SHARING_VIOLATIONS:
                raise
            sleep(delay)
    return operation()


def set_motw(path, host_url):
    """Mark-of-the-Web: the Zone.Identifier stream, ZoneId=3 (Internet)."""
    with open(path + ":Zone.Identifier", "w", encoding="utf-8", newline="") as handle:
        handle.write("[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=%s\r\n" % host_url)


class Part:
    """An exclusive `.part` file being filled: counts and hashes what it is given."""

    def __init__(self, item_id, path, handle):
        self.item_id, self.path, self._file = item_id, path, handle
        self.bytes = 0
        self.head = b""
        self._sha = hashlib.sha256()

    def write(self, chunk):
        self._file.write(chunk)
        self._file.flush()  # what a reader of the .part sees is what was received
        self._sha.update(chunk)
        self.bytes += len(chunk)
        if len(self.head) < HEAD_KEPT:
            self.head = (self.head + bytes(chunk))[:HEAD_KEPT]

    def sha256(self):
        return self._sha.hexdigest()

    def close(self):
        if not self._file.closed:
            self._file.close()


class ArtifactWriter:
    def __init__(self, job_dir, *, rename=os.rename, motw=set_motw, sleep=time.sleep, check=None):
        self._rename, self._motw, self._sleep, self._check = rename, motw, sleep, check
        self.tmp_dir = os.path.join(job_dir, ".tmp")
        self.files_dir = os.path.join(job_dir, "files")
        try:
            os.makedirs(self.tmp_dir, exist_ok=True)
            os.makedirs(self.files_dir, exist_ok=True)
        except OSError:
            raise ArtifactError() from None

    def open(self, item_id):
        path = os.path.join(self.tmp_dir, item_id + ".part")
        try:
            handle = open(path, "xb")  # exclusive: a leftover .part of the same id is an error, never overwritten
        except OSError:
            raise ArtifactError("part") from None
        return Part(item_id, path, handle)

    def abort(self, part):
        part.close()
        try:
            os.remove(part.path)
        except OSError:
            pass

    def _place(self, part, ext):
        digest = part.sha256()[:10]
        for number in range(1, MAX_SUFFIX + 1):
            name = "%s-%s%s.%s" % (part.item_id, digest, "" if number == 1 else "-%d" % number, ext)
            target = os.path.join(self.files_dir, name)
            try:
                retry_io(lambda: self._rename(part.path, target), self._sleep)  # fails when the target exists
            except FileExistsError:
                continue
            except OSError:
                raise ArtifactError() from None
            return name, target
        raise ArtifactError("names")

    def commit(self, part, *, content_type, source_url_redacted, host_url):
        """Close the part and put it into files/. Returns the artifact record; raises ArtifactError (the part
        is removed) or whatever `check` raises (the file is removed)."""
        part.close()
        mime = sniff.detect(part.head)
        try:
            name, target = self._place(part, sniff.extension_for(mime))
        except ArtifactError:
            self.abort(part)
            raise
        try:
            if os.path.getsize(target) != part.bytes:
                raise ArtifactError("size")
            if self._check is not None:
                self._check(target)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(target)
            raise
        warnings = []
        try:
            self._motw(target, host_url)
        except OSError:
            warnings.append("motw_not_set")
        artifact = {"path": "files/" + name, "bytes": part.bytes, "sha256": part.sha256(), "mime": mime}
        declared = (content_type or "").split(";", 1)[0].strip().lower()
        if len(declared) <= MAX_DECLARED and DECLARED_TYPE.fullmatch(declared) and declared != mime:
            artifact["mime_declared"] = declared
        artifact.update(engine="httpx", engine_version=httpx.__version__, source_url_redacted=source_url_redacted,
                        warnings=warnings)
        return artifact
