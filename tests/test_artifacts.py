"""Artifact manager and MIME sniffing (SPEC FR5.1-FR5.5, FR4.5; PLAN TK14). RED until TK15 writes
`agent_crawl_studio.artifacts` and the `detect` / `extension_for` functions of `sniff`.

Pinned interface:
    sniff.detect(head: bytes) -> mime ; sniff.extension_for(mime) -> ext without dot (never an executable one)
    artifacts.ArtifactWriter(job_dir, *, rename=os.rename, motw=artifacts.set_motw, sleep=time.sleep, check=None)
        .open(item_id) -> Part (.path, .write(chunk), .bytes, .sha256(), .head) ; .abort(part)
        .commit(part, *, content_type, source_url_redacted, host_url) -> artifact dict
    artifacts.ArtifactError(code="OUTPUT_WRITE_FAILED", reason) ; artifacts.RETRY_DELAYS == (0.05, 0.1, 0.2, 0.4)
    `check(path)` runs after the file is in place (outroot.post_create_check in `run`); it may raise PathBlocked."""

import hashlib
import os

import httpx
import pytest

from agent_crawl_studio import sniff
from agent_crawl_studio.winpath import PathBlocked

PDF = b"%PDF-1.7\n" + b"0" * 100
EXECUTABLE = {"exe", "dll", "bat", "cmd", "ps1", "vbs", "js", "msi", "lnk", "scr", "com", "hta", "jar"}


@pytest.fixture
def art():
    import agent_crawl_studio.artifacts as module
    return module


@pytest.fixture
def job(tmp_path):
    path = tmp_path / "job"
    path.mkdir()
    return path


def put(art_module, writer, data, item="0001", **kw):
    part = writer.open(item)
    part.write(data)
    args = dict(content_type=None, source_url_redacted="https://files.example.com/a", host_url="https://files.example.com/a")
    args.update(kw)
    return writer.commit(part, **args)


def sha10(data):
    return hashlib.sha256(data).hexdigest()[:10]


class Sleeper:
    def __init__(self, on_call=None):
        self.delays, self.on_call = [], on_call

    def __call__(self, seconds):
        self.delays.append(seconds)
        if self.on_call:
            self.on_call(len(self.delays))


# ----------------------------------------------------------------------------- sniff
@pytest.mark.parametrize("head, mime", [
    (b"%PDF-1.4\n", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n\x00", "image/png"),
    (b"\xff\xd8\xff\xe0JFIF", "image/jpeg"),
    (b"GIF87a....", "image/gif"), (b"GIF89a....", "image/gif"),
    (b"RIFF\x10\x00\x00\x00WEBPVP8 ", "image/webp"),
    (b"PK\x03\x04rest", "application/zip"), (b"PK\x05\x06rest", "application/zip"),
    (b"  <!DOCTYPE html><html>", "text/html"), (b"<html><body>", "text/html"),
    (b'{"a": 1}', "application/json"), (b"  [1, 2]", "application/json"),
    (b"just some text\nwith lines\t", "text/plain"), ("café text".encode("utf-8"), "text/plain"),
    (b"MZ\x90\x00\x03", "application/octet-stream"), (b"\x00\x01\x02binary", "application/octet-stream"),
    (b"", "application/octet-stream"),
])
def test_detect_by_signature(head, mime):
    assert sniff.detect(head) == mime


def test_extensions_come_from_a_table_and_are_never_executable():
    for mime in ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "application/zip", "text/html",
                 "application/json", "text/plain", "application/octet-stream", "application/x-msdownload", "weird/type"):
        ext = sniff.extension_for(mime)
        assert ext and ext.isalnum() and ext == ext.lower() and ext not in EXECUTABLE and len(ext) <= 5
    assert sniff.extension_for("application/pdf") == "pdf" and sniff.extension_for("image/jpeg") == "jpg"
    assert sniff.extension_for("application/x-msdownload") == "bin" and sniff.extension_for("weird/type") == "bin"


# ----------------------------------------------------------------------------- .part, names, hash
def test_the_part_file_is_exclusive_and_lives_in_tmp_never_in_files(art, job):
    writer = art.ArtifactWriter(str(job))
    part = writer.open("0001")
    assert os.path.dirname(part.path) == str(job / ".tmp") and part.path.endswith("0001.part")
    assert os.listdir(job / "files") == []
    part.write(b"abc")
    assert os.listdir(job / "files") == []  # nothing half-written is ever in files/
    with pytest.raises(art.ArtifactError) as caught:  # 'xb': a leftover .part of the same id is refused, never overwritten
        writer.open("0001")
    assert caught.value.code == "OUTPUT_WRITE_FAILED"
    assert (job / ".tmp" / "0001.part").read_bytes() == b"abc"
    writer.abort(part)


def test_part_counts_bytes_keeps_a_head_and_hashes_what_it_got(art, job):
    part = art.ArtifactWriter(str(job)).open("0001")
    for chunk in (b"%PDF", b"-1.7" + b"x" * 1000, b"tail"):
        part.write(chunk)
    data = b"%PDF-1.7" + b"x" * 1000 + b"tail"
    assert part.bytes == len(data) and part.sha256() == hashlib.sha256(data).hexdigest()
    assert part.head.startswith(b"%PDF-1.7") and len(part.head) <= 512


def test_commit_places_the_file_by_id_and_short_hash_and_the_numbers_are_the_disk_ones(art, job):
    writer = art.ArtifactWriter(str(job))
    artifact = put(art, writer, PDF)
    name = "0001-%s.pdf" % sha10(PDF)
    assert artifact["path"] == "files/" + name
    on_disk = (job / "files" / name).read_bytes()
    assert on_disk == PDF and artifact["bytes"] == len(PDF) == os.path.getsize(job / "files" / name)
    assert artifact["sha256"] == hashlib.sha256(on_disk).hexdigest()
    assert artifact["mime"] == "application/pdf" and "mime_declared" not in artifact
    assert artifact["engine"] == "httpx" and artifact["engine_version"] == httpx.__version__
    assert artifact["source_url_redacted"] == "https://files.example.com/a" and artifact["warnings"] == []
    assert os.listdir(job / ".tmp") == []


def test_an_executable_looking_download_gets_a_safe_extension(art, job):
    artifact = put(art, art.ArtifactWriter(str(job)), b"MZ\x90\x00\x03" + b"\x00" * 50)
    assert artifact["mime"] == "application/octet-stream" and artifact["path"].endswith(".bin")


def test_a_collision_gets_a_numbered_suffix_and_nothing_is_overwritten(art, job):
    writer = art.ArtifactWriter(str(job))
    first = put(art, writer, PDF)
    second = put(art, writer, PDF)
    third = put(art, writer, PDF)
    base = "files/0001-%s" % sha10(PDF)
    assert [first["path"], second["path"], third["path"]] == [base + ".pdf", base + "-2.pdf", base + "-3.pdf"]
    assert all(len(os.path.basename(a["path"])) <= 32 for a in (first, second, third))
    assert len(os.listdir(job / "files")) == 3


def test_a_collision_that_differs_only_by_case_is_a_collision(art, job):
    (job / "files").mkdir()
    (job / "files" / ("0001-%s.PDF" % sha10(PDF))).write_bytes(b"older")
    artifact = put(art, art.ArtifactWriter(str(job)), PDF)
    assert artifact["path"].endswith("-2.pdf")
    assert (job / "files" / ("0001-%s.PDF" % sha10(PDF))).read_bytes() == b"older"


def test_an_exhausted_set_of_names_is_a_write_failure(art, job):
    writer = art.ArtifactWriter(str(job), rename=lambda src, dst: (_ for _ in ()).throw(FileExistsError()))
    with pytest.raises(art.ArtifactError):
        put(art, writer, PDF)
    assert os.listdir(job / ".tmp") == []


# ----------------------------------------------------------------------------- rename failures and retries
def test_a_failing_rename_is_a_write_failure_and_the_part_is_removed(art, job):
    def boom(src, dst):
        raise OSError(5, "no")  # not a sharing violation: no retry
    sleeper = Sleeper()
    writer = art.ArtifactWriter(str(job), rename=boom, sleep=sleeper)
    with pytest.raises(art.ArtifactError) as caught:
        put(art, writer, PDF)
    assert caught.value.code == "OUTPUT_WRITE_FAILED" and os.listdir(job / ".tmp") == [] and os.listdir(job / "files") == []
    assert sleeper.delays == []


def sharing_violation():
    error = PermissionError(13, "in use")
    error.winerror = 32
    return error


def test_a_part_held_by_another_process_is_retried_with_growing_pauses_then_fails(art, job):
    calls = []

    def locked(src, dst):
        calls.append(dst)
        raise sharing_violation()
    sleeper = Sleeper()
    writer = art.ArtifactWriter(str(job), rename=locked, sleep=sleeper)
    with pytest.raises(art.ArtifactError):
        put(art, writer, PDF)
    assert len(calls) == 5 and sleeper.delays == [0.05, 0.1, 0.2, 0.4] == list(art.RETRY_DELAYS)
    assert os.listdir(job / ".tmp") == []


def test_a_real_open_handle_blocks_the_rename_until_it_is_released(art, job):
    writer = art.ArtifactWriter(str(job), sleep=Sleeper(on_call=lambda n: holder.close() if n == 2 else None))
    part = writer.open("0001")
    part.write(PDF)
    holder = open(part.path, "rb")  # an anti-virus scanner reading the .part
    artifact = writer.commit(part, content_type=None, source_url_redacted="u", host_url="u")
    assert artifact["bytes"] == len(PDF) and (job / "files" / os.path.basename(artifact["path"])).exists()


def test_a_real_open_handle_that_never_goes_away_fails_the_item(art, job):
    writer = art.ArtifactWriter(str(job), sleep=Sleeper())
    part = writer.open("0001")
    part.write(PDF)
    with open(part.path, "rb"):
        with pytest.raises(art.ArtifactError):
            writer.commit(part, content_type=None, source_url_redacted="u", host_url="u")
    assert os.listdir(job / "files") == []


def test_a_file_whose_size_changed_after_the_rename_is_refused(art, job):
    def rename_then_truncate(src, dst):
        os.rename(src, dst)
        open(dst, "wb").close()
    writer = art.ArtifactWriter(str(job), rename=rename_then_truncate)
    with pytest.raises(art.ArtifactError):
        put(art, writer, PDF)
    assert os.listdir(job / "files") == []


def test_abort_removes_the_part(art, job):
    writer = art.ArtifactWriter(str(job))
    part = writer.open("0007")
    part.write(b"x")
    writer.abort(part)
    assert os.listdir(job / ".tmp") == []
    writer.abort(part)  # twice is harmless


# ----------------------------------------------------------------------------- declared type, Mark-of-the-Web, check
@pytest.mark.parametrize("declared, expected", [
    ("image/PNG; charset=x", "image/png"), ("text/html", "text/html"), ("application/pdf", None),
    ("not a type", None), ("x/y z", None), ("image/png\r\nX: y", None), (None, None), ("", None), ("a" * 300 + "/b", None),
])
def test_mime_declared_is_normalised_or_left_out(art, job, declared, expected):
    artifact = put(art, art.ArtifactWriter(str(job)), PDF, content_type=declared)
    assert artifact["mime"] == "application/pdf"
    if expected is None:
        assert "mime_declared" not in artifact
    else:
        assert artifact["mime_declared"] == expected


def read_zone(path):
    with open(path + ":Zone.Identifier", "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def test_mark_of_the_web_is_written_on_ntfs(art, job):
    artifact = put(art, art.ArtifactWriter(str(job)), PDF, host_url="https://files.example.com/a?x=REDACTED")
    zone = read_zone(str(job / artifact["path"]))
    assert "ZoneId=3" in zone and "HostUrl=https://files.example.com/a?x=REDACTED" in zone
    assert artifact["warnings"] == []


def test_a_failing_mark_of_the_web_is_a_warning_not_an_error(art, job):
    def refuse(path, host_url):
        raise OSError("no streams here")
    artifact = put(art, art.ArtifactWriter(str(job), motw=refuse), PDF)
    assert artifact["warnings"] == ["motw_not_set"] and (job / artifact["path"]).exists()


def test_the_check_runs_for_every_file_and_a_blocked_path_removes_it(art, job):
    seen = []

    def check(path):
        seen.append(path)
        if os.sep + "files" + os.sep in path and os.path.basename(path).startswith("0002"):  # the placed second file
            raise PathBlocked("reparse")
    writer = art.ArtifactWriter(str(job), check=check)
    put(art, writer, PDF, item="0001")
    with pytest.raises(PathBlocked):
        put(art, writer, PDF + b"2", item="0002")
    placed = [p for p in seen if os.sep + "files" + os.sep in p]
    assert len(placed) == 2 and all(os.path.isabs(p) and p.startswith(str(job)) for p in seen)
    assert len(os.listdir(job / "files")) == 1 and os.listdir(job / ".tmp") == []


def test_set_motw_writes_the_documented_stream(art, tmp_path):
    target = tmp_path / "f.bin"
    target.write_bytes(b"x")
    art.set_motw(str(target), "https://a.example/x")
    assert read_zone(str(target)) == "[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=https://a.example/x\r\n"


# ------------------------------------------------- SPR-03 TK9: media signatures, adopt, item folders (FR10.1-FR10.5)
MEDIA_SAMPLES = [
    (b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40, "video/mp4", "mp4"),
    (b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 40, "audio/mp4", "m4a"),
    (b"\x1a\x45\xdf\xa3" + b"\x00" * 8 + b"\x42\x82\x84webm" + b"\x00" * 20, "video/webm", "webm"),
    (b"\x1a\x45\xdf\xa3" + b"\x00" * 8 + b"\x42\x82\x88matroska" + b"\x00" * 20, "video/x-matroska", "mkv"),
    (b"OggS\x00\x02" + b"\x00" * 40, "audio/ogg", "ogg"),
    (b"ID3\x04\x00" + b"\x00" * 40, "audio/mpeg", "mp3"),
    (b"\xff\xfb\x90\x00" + b"\x00" * 40, "audio/mpeg", "mp3"),
    (b"\xff\xf3\x90\x00" + b"\x00" * 40, "audio/mpeg", "mp3"),
    (b"\xff\xf2\x90\x00" + b"\x00" * 40, "audio/mpeg", "mp3"),
    (b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20, "audio/wav", "wav"),
]
MP4 = MEDIA_SAMPLES[0][0]


@pytest.mark.parametrize("head,mime,ext", MEDIA_SAMPLES)
def test_detect_media_signatures(head, mime, ext):
    assert sniff.detect(head) == mime and sniff.extension_for(mime) == ext
    assert mime in sniff.MEDIA_MIMES


def test_media_mimes_are_exactly_the_seven_types_and_riff_webp_is_still_an_image():
    assert sniff.MEDIA_MIMES == frozenset(
        {"video/mp4", "audio/mp4", "video/webm", "video/x-matroska", "audio/ogg", "audio/mpeg", "audio/wav"})
    assert sniff.detect(b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 20) == "image/webp"
    assert sniff.detect(b"\x1a\x45\xdf\xa3" + b"\x00" * 30) not in sniff.MEDIA_MIMES  # EBML with no DocType


def media_src(job, data=MP4, item="0001", name="media.mp4"):
    folder = job / ".tmp" / item / "dl"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return str(path)


def adopt(writer, src, item="0001", **kw):
    args = dict(source_url_redacted="https://www.youtube.com/REDACTED", host_url="https://www.youtube.com/",
                engine="yt-dlp", engine_version="2026.10.26", max_bytes=10 ** 9, allowed_mimes=sniff.MEDIA_MIMES)
    args.update(kw)
    return writer.adopt(item, src, **args)


def test_adopt_places_the_file_and_describes_it(art, job):
    data = MP4 + b"payload" * 100
    writer = art.ArtifactWriter(str(job))
    artifact = adopt(writer, media_src(job, data))
    assert artifact["path"] == "files/0001-%s.mp4" % sha10(data)
    assert (job / artifact["path"]).read_bytes() == data
    assert artifact["bytes"] == len(data) and artifact["sha256"] == hashlib.sha256(data).hexdigest()
    assert artifact["mime"] == "video/mp4" and artifact["engine"] == "yt-dlp"
    assert artifact["engine_version"] == "2026.10.26" and artifact["warnings"] == []
    assert artifact["source_url_redacted"] == "https://www.youtube.com/REDACTED"
    assert not os.path.exists(job / ".tmp" / "0001" / "dl" / "media.mp4"), "the source moved"


def test_adopt_names_a_second_identical_file_with_a_suffix(art, job):
    writer = art.ArtifactWriter(str(job))
    first = adopt(writer, media_src(job))
    second = adopt(writer, media_src(job, item="0002"), item="0001")
    assert first["path"] != second["path"] and second["path"].endswith("-2.mp4")


def test_adopt_a_page_is_a_mime_error_and_the_source_is_removed(art, job):
    src = media_src(job, b"<!doctype html><html>login</html>")
    with pytest.raises(art.ArtifactError) as err:
        adopt(art.ArtifactWriter(str(job)), src)
    assert err.value.reason == "mime" and not os.path.exists(src) and os.listdir(job / "files") == []


def test_adopt_a_too_big_file_is_a_size_error_and_the_source_is_removed(art, job):
    src = media_src(job, MP4 + b"x" * 100)
    with pytest.raises(art.ArtifactError) as err:
        adopt(art.ArtifactWriter(str(job)), src, max_bytes=50)
    assert err.value.reason == "size" and not os.path.exists(src) and os.listdir(job / "files") == []


def test_adopt_refuses_a_hard_linked_source(art, job, tmp_path):
    src = media_src(job)
    outside = tmp_path / "other.mp4"
    os.link(src, str(outside))
    with pytest.raises(PathBlocked):
        adopt(art.ArtifactWriter(str(job)), src)
    assert outside.read_bytes() == MP4 and os.listdir(job / "files") == []


def test_adopt_refuses_a_reparse_point(art, job, tmp_path):
    import _winapi
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "x.mp4").write_bytes(MP4)
    folder = job / ".tmp" / "0001"
    folder.mkdir(parents=True)
    link = folder / "dl"
    _winapi.CreateJunction(str(secret), str(link))
    try:
        with pytest.raises(PathBlocked):
            adopt(art.ArtifactWriter(str(job)), str(link))
        assert (secret / "x.mp4").exists() and os.listdir(job / "files") == []
    finally:
        os.rmdir(str(link))


def test_adopt_a_missing_source_is_a_write_error(art, job):
    with pytest.raises(art.ArtifactError):
        adopt(art.ArtifactWriter(str(job)), str(job / ".tmp" / "0001" / "dl" / "media.mp4"))


def test_adopt_runs_the_check_and_a_blocked_path_removes_the_placed_file(art, job):
    def check(path):
        if os.sep + "files" + os.sep in path:
            raise PathBlocked("reparse")
    with pytest.raises(PathBlocked):
        adopt(art.ArtifactWriter(str(job), check=check), media_src(job))
    assert os.listdir(job / "files") == []


def test_adopt_notes_a_failed_mark_of_the_web_as_a_warning(art, job):
    def refuse(path, host_url):
        raise OSError("no streams")
    artifact = adopt(art.ArtifactWriter(str(job), motw=refuse), media_src(job))
    assert artifact["warnings"] == ["motw_not_set"]


def test_adopt_a_rename_failure_is_a_write_error(art, job):
    def broken(src, dst):
        raise OSError("disk")
    with pytest.raises(art.ArtifactError):
        adopt(art.ArtifactWriter(str(job), rename=broken), media_src(job))


def test_sweep_removes_item_folders_and_keeps_tmp(art, job):
    writer = art.ArtifactWriter(str(job))
    media_src(job, item="0001")
    (job / ".tmp" / "0001" / "io").mkdir()
    (job / ".tmp" / "0001" / "io" / "stderr.bin").write_bytes(b"x")
    media_src(job, item="0002")
    (job / ".tmp" / "keep").mkdir()
    writer.sweep()
    assert sorted(os.listdir(job / ".tmp")) == ["keep"]


def test_sweep_never_follows_a_junction_in_the_place_of_an_item_folder(art, job, tmp_path):
    import _winapi
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "keep.txt").write_text("mine")
    writer = art.ArtifactWriter(str(job))
    _winapi.CreateJunction(str(secret), str(job / ".tmp" / "0003"))
    writer.sweep()
    assert (secret / "keep.txt").read_text() == "mine"
    assert not os.path.lexists(str(job / ".tmp" / "0003")), "the link itself is taken away"


def test_adopt_reads_a_file_bigger_than_one_block(art, job):
    data = MP4 + b"z" * (2 ** 20 + 5)
    artifact = adopt(art.ArtifactWriter(str(job)), media_src(job, data))
    assert artifact["sha256"] == hashlib.sha256(data).hexdigest() and artifact["bytes"] == len(data)


def test_adopt_refuses_a_handle_that_is_not_the_file_that_was_looked_at(art, job, monkeypatch):
    src = media_src(job)
    real = os.fstat

    class Held:
        st_nlink, st_size = 2, 1
    monkeypatch.setattr(os, "fstat", lambda fd: Held())
    with pytest.raises(PathBlocked):
        adopt(art.ArtifactWriter(str(job)), src)
    monkeypatch.setattr(os, "fstat", real)


def test_adopt_a_source_that_cannot_be_opened_is_a_write_error(art, job, monkeypatch):
    import builtins
    src = media_src(job)
    real = builtins.open

    def refuse(path, *args, **kw):
        if path == src:
            raise PermissionError("busy")
        return real(path, *args, **kw)
    monkeypatch.setattr(builtins, "open", refuse)
    with pytest.raises(art.ArtifactError):
        adopt(art.ArtifactWriter(str(job)), src)


def test_adopt_refuses_a_source_that_changed_between_reading_and_placing(art, job, monkeypatch):
    src = media_src(job)
    writer = art.ArtifactWriter(str(job))
    original = writer._plain_source
    calls = []

    def changing(path):
        info = original(path)
        calls.append(1)
        if len(calls) == 2:
            return os.stat_result((info.st_mode, info.st_ino + 1, info.st_dev, info.st_nlink, 0, 0, info.st_size,
                                   0, 0, 0))
        return info
    monkeypatch.setattr(writer, "_plain_source", changing)
    with pytest.raises(PathBlocked):
        adopt(writer, src)
    assert os.listdir(job / "files") == []


def test_adopt_a_file_whose_size_changed_in_the_rename_is_removed(art, job):
    def short_rename(src, dst):
        with open(dst, "wb") as out:
            out.write(b"x")
        os.remove(src)
    with pytest.raises(art.ArtifactError) as err:
        adopt(art.ArtifactWriter(str(job), rename=short_rename), media_src(job))
    assert err.value.reason == "size" and os.listdir(job / "files") == []
