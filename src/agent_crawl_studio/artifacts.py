"""Artifact manager (SPEC FR5.1-FR5.5, FR4.5): `.part` files, an atomic no-overwrite rename into `files/`, hash and
size of what is on disk, MIME from the bytes, a safe extension, Mark-of-the-Web (best effort).

A name is `<item id>-<10 hex of the sha256>[-N].<extension>`: never a title, a URL segment or a Content-Disposition
name. Nothing half-written is ever in `files/`, and a finished file is never overwritten by this code."""

import contextlib
import gc
import hashlib
import os
import re
import stat
import time
import unicodedata

import httpx

from . import sniff, ytdlp
from .winpath import PathBlocked

RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4)  # five tries, the pause doubles
SHARING_VIOLATIONS = (5, 32)  # WinError: access denied / sharing violation (an anti-virus scanner, an indexer)
MAX_SUFFIX = 99
HEAD_KEPT = 512
DECLARED_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
MAX_DECLARED = 100
FINISHED_NAME = re.compile(r"^[0-9]{4}-[0-9a-f]{10}(-[0-9]+)?\.[a-z0-9]+$")  # what _place can have made
PART_NAME = re.compile(r"^[0-9]{4}\.part$")
ITEM_DIR_NAME = re.compile(r"^[0-9]{4}(-[0-9]+)?$")
REPARSE = 0x400
BLOCK = 2 ** 20


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
    host_url = "".join(ch for ch in host_url if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Zl", "Zp"))
    with open(path + ":Zone.Identifier", "w", encoding="utf-8", newline="") as handle:
        handle.write("[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=%s\r\n" % host_url)


class Part:
    """An exclusive `.part` file being filled: counts and hashes what it is given."""

    def __init__(self, item_id, path, handle):
        self.item_id, self.path, self._file = item_id, path, handle
        self.placed = None  # where the finished file now is (set the moment the rename is done)
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
        if check is not None:  # a junction in the place of either folder would send every file out of the job
            check(self.tmp_dir)
            check(self.files_dir)

    def open(self, item_id):
        path = os.path.join(self.tmp_dir, item_id + ".part")
        preexisting = os.path.lexists(path)  # a leftover .part of the same id is an error, never overwritten
        handle = None
        try:  # whatever happens in here (Ctrl+C at any line): no handle and no .part of ours are left behind
            try:
                handle = open(path, "xb")
            except OSError:
                raise ArtifactError("part") from None
            part = Part(item_id, path, handle)
            if self._check is not None:
                self._check(path)
            return part
        except BaseException:
            if handle is not None:
                with contextlib.suppress(OSError):
                    handle.close()
            if not preexisting:
                with contextlib.suppress(OSError):
                    os.remove(path)
            raise

    def _folder_ok(self, folder):
        """A cleanup walks a folder only when it is still the plain folder of this job (a junction put in its
        place would turn the cleanup into deleting somebody else's files)."""
        if self._check is None:
            return True
        try:
            self._check(folder)
        except Exception:  # noqa: BLE001 - PathBlocked or OSError: do not touch it
            return False
        return True

    def _remove_matching(self, folder, wanted):
        if not self._folder_ok(folder):
            return
        with contextlib.suppress(OSError):
            for name in os.listdir(folder):
                if wanted(name):
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(folder, name))

    def discard(self, item_ids):
        """Remove the finished files of items that never reached the manifest (their names start with the id)."""
        self._remove_matching(self.files_dir, lambda name: FINISHED_NAME.match(name) and name[:4] in item_ids)

    def reconcile(self, keep):
        """After a cancelled run: remove every finished file that is not in `keep` (the names the manifest lists).
        A handle that an interrupted write left dangling is freed first (a file in use cannot be removed); that
        gc.collect() is only worth its cost here, on an interrupted run."""
        gc.collect()
        self._remove_matching(self.files_dir, lambda name: FINISHED_NAME.match(name) and name not in keep)

    def sweep(self):
        """Remove every .part of this job (after an interrupt nobody else can be writing them)."""
        self._remove_matching(self.tmp_dir, PART_NAME.match)
        if not self._folder_ok(self.tmp_dir):
            return
        with contextlib.suppress(OSError):
            for name in os.listdir(self.tmp_dir):
                if ITEM_DIR_NAME.match(name):
                    ytdlp.clear_item_dir(os.path.join(self.tmp_dir, name), self._check)

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
                part.placed = target
            except FileExistsError:
                continue
            except OSError:
                raise ArtifactError() from None
            except BaseException:  # Ctrl+C: a rename that already happened must not leave its file behind
                if not os.path.exists(part.path):
                    with contextlib.suppress(OSError):
                        os.remove(target)
                raise
            return name, target
        raise ArtifactError("names")

    def commit(self, part, *, content_type, source_url_redacted, host_url, role=None, mime=None, extra=None):
        """Close the part and put it into files/. Returns the artifact record; raises ArtifactError (the part
        is removed) or whatever `check` raises (the file is removed).

        `role` (when given) goes into the record and `extra` adds fields, never replacing one the writer measured.
        `mime` is for a page the caller has already recognised in a way `sniff.detect` cannot (the source page of the
        extract mode: UTF-16 or XHTML): it is the record's type and gives the extension."""
        part.close()
        mime = mime or sniff.detect(part.head)
        try:
            name, target = self._place(part, sniff.extension_for(mime))
        except ArtifactError:
            self.abort(part)
            raise
        warnings = []
        try:  # whatever goes wrong from here on (even Ctrl+C) takes the placed file away again
            if os.path.getsize(target) != part.bytes:
                raise ArtifactError("size")
            if self._check is not None:
                self._check(target)
            try:
                self._motw(target, host_url)
            except OSError:
                warnings.append("motw_not_set")
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(target)
            raise
        artifact = {"path": "files/" + name, "bytes": part.bytes, "sha256": part.sha256(), "mime": mime}
        declared = (content_type or "").split(";", 1)[0].strip().lower()
        if len(declared) <= MAX_DECLARED and DECLARED_TYPE.fullmatch(declared) and declared != mime:
            artifact["mime_declared"] = declared
        artifact.update(engine="httpx", engine_version=httpx.__version__, source_url_redacted=source_url_redacted,
                        warnings=warnings)
        if role is not None:
            artifact["role"] = role
        for key, value in (extra or {}).items():
            artifact.setdefault(key, value)
        return artifact

    @staticmethod
    def _plain_source(path):
        """lstat of a source that is a plain file with one link and no reparse point; else PathBlocked."""
        try:
            info = os.lstat(path)
        except OSError:
            raise ArtifactError("part") from None
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & REPARSE):
            raise PathBlocked("not_plain")
        return info

    def _read_source(self, src_path, max_bytes):
        """(size, sha256 hex, head, lstat) of the source, read through one handle; ArtifactError("size") when it
        is bigger than `max_bytes`."""
        before = self._plain_source(src_path)
        try:
            with open(src_path, "rb") as handle:
                held = os.fstat(handle.fileno())
                if held.st_nlink != 1 or held.st_size != before.st_size:
                    raise PathBlocked("not_plain")
                if held.st_size > max_bytes:
                    raise ArtifactError("size")
                sha, head, total = hashlib.sha256(), b"", 0
                while True:
                    block = handle.read(BLOCK)
                    if not block:
                        break
                    if not head:
                        head = bytes(block[:HEAD_KEPT])
                    sha.update(block)
                    total += len(block)
        except OSError:
            raise ArtifactError("part") from None
        return total, sha.hexdigest(), head, before

    def adopt(self, item_id, src_path, *, source_url_redacted, host_url, engine, engine_version, max_bytes,
              allowed_mimes, role=None, mime=None, extra=None):
        """Take a finished file that another tool wrote (yt-dlp, with its tree already dead) into files/. The
        source is read once through one handle (no reparse point, one link), hashed, its type taken from its
        bytes; a type outside `allowed_mimes` or a size over `max_bytes` removes the source and raises
        ArtifactError("mime"/"size"). Returns the artifact record.

        `role` (when given) goes into the record and `extra` adds more fields, never replacing one the writer
        measured. `mime="text/markdown"` is the one type the caller may name: it is valid only for a file the bytes
        show as `text/plain` (an HTML comment as first line shows `text/html` and is refused), and then it is the
        record's type and gives the `.md` extension; any other `mime` raises ArtifactError("mime") as a bad type."""
        try:
            total, digest, head, before = self._read_source(src_path, max_bytes)
        except ArtifactError as exc:
            if exc.reason == "size":
                with contextlib.suppress(OSError):
                    os.remove(src_path)
            raise
        mime_seen = sniff.detect(head)
        wanted = mime_seen
        if mime is not None:  # the caller names the type of a plain text file (FR21.2)
            wanted = mime if (mime == "text/markdown" and mime_seen == "text/plain") else None
        if wanted is None or wanted not in allowed_mimes:
            with contextlib.suppress(OSError):
                os.remove(src_path)
            raise ArtifactError("mime")
        mime = wanted
        after = self._plain_source(src_path)  # swapped for a link or another file since it was read: refuse
        if (after.st_ino, after.st_size) != (before.st_ino, before.st_size):
            raise PathBlocked("changed")
        name, target = self._place(_Source(item_id, src_path, total, digest), sniff.extension_for(mime))
        warnings = []
        try:
            if os.path.getsize(target) != total:
                raise ArtifactError("size")
            if self._check is not None:
                self._check(target)
            try:
                self._motw(target, host_url)
            except OSError:
                warnings.append("motw_not_set")
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(target)
            raise
        artifact = {"path": "files/" + name, "bytes": total, "sha256": digest, "mime": mime, "engine": engine,
                    "engine_version": engine_version, "source_url_redacted": source_url_redacted, "warnings": warnings}
        if role is not None:
            artifact["role"] = role
        for key, value in (extra or {}).items():
            artifact.setdefault(key, value)
        return artifact


class _Source:
    """What `ArtifactWriter._place` needs of a file that is already on disk."""

    def __init__(self, item_id, path, size, digest):
        self.item_id, self.path, self.bytes, self._digest = item_id, path, size, digest
        self.placed = None

    def sha256(self):
        return self._digest
