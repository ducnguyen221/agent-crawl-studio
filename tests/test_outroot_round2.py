"""Review round 2 of outroot (RF60-RF68): level-by-level creation, job collisions, length reserve,
Startup through the known-folder API, unknown Cloud Files answers, quoted PATH entries, broad roots,
real 8.3 and symlink calls."""

import ctypes
import os
import subprocess
import threading

import _winapi
import pytest
from test_outroot import FAKES, blocked, cfg, resolve, world  # noqa: F401 - `world` is a fixture

from agent_crawl_studio import outroot, winpath
from agent_crawl_studio.winpath import PathBlocked

JOB = "20261113-101010-abcdef"


def rmdir_link(path):
    """Remove a junction or directory symlink without following it."""
    subprocess.run(["cmd", "/c", "rmdir", path], check=True, capture_output=True)


# --- RF60: one level at a time, check after every level --------------------------------------------------------
def test_a_junction_in_a_middle_level_stops_create_job_before_anything_is_made_behind_it(world, tmp_path):
    got = resolve(world, "a/b/c")
    os.makedirs(os.path.join(world["work"], "jobs"))
    outside = tmp_path / "outside"
    outside.mkdir()
    link = os.path.join(world["work"], "jobs", "a")
    _winapi.CreateJunction(str(outside), link)
    try:
        with pytest.raises(PathBlocked) as info:
            outroot.create_job(got, JOB)
        assert info.value.reason == "reparse"
        assert list(outside.iterdir()) == [], "no `b`, `b\\c` or job folder may be left outside the root"
    finally:
        rmdir_link(link)
    assert outside.is_dir()


def test_every_level_is_checked_right_after_it_is_created_and_the_levels_made_are_removed_on_a_block(world):
    got = resolve(world, "a/b/c")
    seen = []

    def attrs(path):
        seen.append(path)
        return (0x410, 0xA0000003) if path.endswith(os.path.join("a", "b")) else None

    with pytest.raises(PathBlocked) as info:
        outroot.create_job(got, JOB, attrs_of=attrs)
    assert info.value.reason == "reparse"
    assert not os.path.exists(os.path.join(world["work"], "jobs", "a", "b", "c")), "stopped at b"
    assert not os.path.exists(world["work"]), "every level this call created is removed again"
    levels = [os.path.relpath(p, world["work"]) for p in seen if p.startswith(world["work"])]
    assert levels[:4] == [".", "jobs", os.path.join("jobs", "a"), os.path.join("jobs", "a", "b")]


def test_create_job_keeps_levels_that_already_existed(world):
    os.makedirs(os.path.join(world["work"], "jobs", "keep"))
    got = resolve(world, "keep/x")
    outroot.create_job(got, JOB, **FAKES)
    assert os.path.isdir(os.path.join(world["work"], "jobs", "keep", "x", JOB))
    with pytest.raises(PathBlocked):
        outroot.create_job(got, "CON", **FAKES)
    assert os.path.isdir(os.path.join(world["work"], "jobs", "keep"))


# --- RF66: a taken job id and other OS errors are our own types ----------------------------------------------------
def test_a_taken_job_id_is_job_exists_a_write_blocked(world):
    got = resolve(world)
    outroot.create_job(got, JOB, **FAKES)
    with pytest.raises(outroot.JobExists) as info:
        outroot.create_job(got, JOB, **FAKES)
    assert isinstance(info.value, outroot.WriteBlocked) and info.value.code == "OUTPUT_WRITE_FAILED"
    assert info.value.reason == "exists" and info.value.missing_gib == 0


def test_four_parallel_creations_of_the_same_job_give_one_success_and_three_job_exists(world):
    got = resolve(world)
    barrier = threading.Barrier(4)
    results = []

    def attempt():
        barrier.wait()
        try:
            results.append(outroot.create_job(got, JOB, **FAKES))
        except Exception as exc:  # noqa: BLE001 - the type is what is asserted
            results.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    good = [r for r in results if isinstance(r, str)]
    bad = [r for r in results if not isinstance(r, str)]
    assert len(good) == 1 and len(bad) == 3 and all(type(r) is outroot.JobExists for r in bad), results


def test_another_os_error_while_creating_is_write_blocked_not_a_raw_oserror(world, monkeypatch):
    got = resolve(world)
    real = os.mkdir

    def refuse(path, *args, **kwargs):
        if str(path).endswith(JOB):
            raise PermissionError("denied")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", refuse)
    with pytest.raises(outroot.WriteBlocked) as info:
        outroot.create_job(got, JOB, **FAKES)
    assert info.value.reason == "os_error" and not isinstance(info.value, outroot.JobExists)


def test_an_os_error_while_creating_a_level_is_write_blocked(world, monkeypatch):
    got = resolve(world, "lvl")
    real = os.mkdir

    def refuse(path, *args, **kwargs):
        if str(path).endswith("lvl"):
            raise PermissionError("denied")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", refuse)
    with pytest.raises(outroot.WriteBlocked) as info:
        outroot.create_job(got, JOB, **FAKES)
    assert info.value.reason == "os_error"


# --- RF63: the 240 limit leaves room for job id, \files\ and a file name -----------------------------------------------
FIXED_WORK = "C:\\crawl-len\\w"  # 14 characters, never created: the length tests must not depend on pytest's folder


@pytest.fixture
def fixed_world(world):
    """`world` with a work root of a fixed length, so that a path of exactly N characters is one on every machine."""
    assert len(FIXED_WORK) == 14 and not os.path.exists(FIXED_WORK)
    return dict(world, work=FIXED_WORK, env=dict(world["env"], CRAWL_STUDIO_WORK=FIXED_WORK))


def parent_of_length(world, length):
    base = world["work"] + "\\jobs"
    pad = length - len(base) - 1
    parts = []
    while pad > 0:
        take = min(100, pad - 1) if pad > 100 else pad
        parts.append("d" * take)
        pad -= take + 1
    return base + "\\" + "\\".join(parts)


def test_the_reserve_constants():
    assert winpath.JOB_RESERVE == 1 + 22 + len("\\files\\") + 32


def test_a_parent_of_239_chars_is_blocked_and_the_real_limit_is_178(fixed_world):
    assert 240 - winpath.JOB_RESERVE == 178
    for length in (239, 179):
        parent = parent_of_length(fixed_world, length)
        assert len(parent) == length
        assert blocked(fixed_world, parent) == "too_long"
    parent = parent_of_length(fixed_world, 178)
    assert len(parent) == 178 and resolve(fixed_world, parent).parent == parent


def test_create_job_checks_the_length_of_the_whole_job_path(fixed_world):
    short = resolve(fixed_world)
    long_root = outroot.Root(short.root, parent_of_length(fixed_world, 200), [])
    with pytest.raises(PathBlocked) as info:
        outroot.create_job(long_root, JOB, **FAKES)
    assert info.value.reason == "too_long" and not os.path.exists(fixed_world["work"])


# --- RF62: Startup folders from the known-folder API, not from APPDATA ---------------------------------------------------
def test_startup_is_blocked_even_when_appdata_is_missing_from_the_environment(world, tmp_path):
    startup = str(tmp_path / "real-startup")
    for key in ("APPDATA", "ProgramData"):
        world["env"].pop(key, None)
    for dest in (startup, os.path.join(startup, "x")):
        with pytest.raises(PathBlocked) as info:
            outroot.resolve_output(dest, cfg(world, str(tmp_path)), world["env"], startup_folders_of=lambda: [startup], **FAKES)
        assert info.value.reason == "forbidden_place"


def test_a_root_that_is_a_startup_folder_is_dropped(world, tmp_path):
    startup = str(tmp_path / "real-startup")
    roots, warnings = outroot.usable_roots(cfg(world, startup), world["env"], drive_type_of=lambda p: 3, startup_folders_of=lambda: [startup])
    assert startup not in roots and warnings == ["root_ignored"]


def test_real_known_folder_api_gives_the_two_startup_folders():
    folders = outroot.default_startup_folders()
    assert len(folders) == 2 and all(f.lower().endswith("startup") and os.path.isabs(f) for f in folders)
    assert folders[0].lower() != folders[1].lower()


def test_the_real_user_startup_folder_is_blocked_with_an_empty_environment(tmp_path):
    user = outroot.default_startup_folders()[0]
    from agent_crawl_studio import config
    work = str(tmp_path / "w")
    config_obj = config.Config(work, [work, os.path.dirname(user)], (), [])
    with pytest.raises(PathBlocked) as info:
        outroot.resolve_output(user + "\\x", config_obj, {"CRAWL_STUDIO_WORK": work})
    assert info.value.reason == "forbidden_place"


# --- RF64: only the documented "not a sync root" answers are trusted -----------------------------------------------------
def test_an_unknown_answer_of_the_sync_root_check_blocks_as_cloud_unknown(world):
    assert blocked(world, None, sync_root_of=lambda path: "unknown") == "cloud_unknown"


@pytest.mark.parametrize("hresult,expected", [(0, True), (0x80070186, False), (0x80070002, False), (0x80070003, False),
                                              (0x80070005, "unknown"), (0x8007017C, "unknown"), (0x80004005, "unknown")])
def test_default_sync_root_of_maps_each_hresult(monkeypatch, hresult, expected):
    monkeypatch.setattr(outroot, "_cf_hresult", lambda path: hresult)
    assert outroot.default_sync_root_of("C:\\x") == expected


def test_real_hresults_of_plain_and_missing_folders_are_in_the_trusted_set(tmp_path):
    assert outroot._cf_hresult(str(tmp_path)) in outroot.NOT_CLOUD_HRESULTS
    assert outroot._cf_hresult(str(tmp_path / "missing")) in outroot.NOT_CLOUD_HRESULTS


# --- RF65: quoted PATH entries, broad roots ----------------------------------------------------------------------------
def test_a_quoted_path_entry_still_blocks_by_equality(world):
    world["env"]["PATH"] = '"%s"' % world["pathdir"] if "pathdir" in world else '"%s"' % os.path.join(world["work"], "jobs", "pathdir")
    blocked(world, os.path.join(world["work"], "jobs", "pathdir"))


def test_a_home_root_and_a_drive_root_give_a_broad_root_warning_but_are_allowed(world):
    got = resolve(world, os.path.join(world["home"], "Downloads"), world["home"])
    assert got.root == world["home"] and "broad_root" in got.warnings
    drive_root = os.path.splitdrive(world["work"])[0] + "\\"
    got = resolve(world, drive_root + "crawl-x", drive_root)
    assert got.root == drive_root and "broad_root" in got.warnings
    assert "broad_root" not in resolve(world).warnings


# --- RF68: real 8.3 names and a real symlink -----------------------------------------------------------------------------
def test_a_real_short_name_of_a_real_folder_is_refused_in_the_user_part(tmp_path):
    long_dir = tmp_path / "a-fairly-long-folder-name"
    long_dir.mkdir()
    buffer = ctypes.create_unicode_buffer(300)
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetShortPathNameW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    kernel.GetShortPathNameW(str(long_dir), buffer, 300)
    short = os.path.basename(buffer.value)
    if "~" not in short:
        pytest.skip("BLOCKED_ENV: this volume does not create 8.3 short names (GetShortPathNameW returned %r)" % short)
    with pytest.raises(PathBlocked) as info:
        winpath.check_component(short)
    assert info.value.reason == "short_name"
    winpath.check_component(short, user_supplied=False)


def test_a_real_directory_symlink_is_a_reparse_point_and_is_blocked(world, tmp_path):
    outside = tmp_path / "outside-sym"
    outside.mkdir()
    jobs = os.path.join(world["work"], "jobs")
    os.makedirs(jobs)
    link = os.path.join(jobs, "sym")
    try:
        os.symlink(str(outside), link, target_is_directory=True)
    except OSError as exc:
        pytest.skip("BLOCKED_ENV: this account cannot create symbolic links without elevation (%s)" % exc)
    try:
        attrs, tag = outroot.default_attrs_of(link)
        assert attrs & 0x400 and tag == 0xA000000C
        for bad in ("sym", "sym/x"):
            with pytest.raises(PathBlocked):
                outroot.resolve_output(bad, cfg(world), world["env"])
        assert list(outside.iterdir()) == []
    finally:
        rmdir_link(link)


# --- remaining branches ------------------------------------------------------------------------------------------------
def test_without_a_userprofile_only_the_drive_root_counts_as_broad(world):
    world["env"].pop("USERPROFILE")
    assert "broad_root" not in resolve(world).warnings


def test_an_os_error_above_the_root_is_write_blocked(world, monkeypatch):
    got = resolve(world)

    def refuse(path, *args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(os, "makedirs", refuse)
    with pytest.raises(outroot.WriteBlocked) as info:
        outroot.create_job(got, JOB, **FAKES)
    assert info.value.reason == "os_error"


def test_a_cleanup_that_cannot_remove_a_level_still_reports_the_original_block(world, monkeypatch):
    got = resolve(world, "a/b")

    def stuck(path):
        raise OSError("in use")

    monkeypatch.setattr(os, "rmdir", stuck)
    with pytest.raises(PathBlocked) as info:
        outroot.create_job(got, JOB, attrs_of=lambda p: (0x410, 0xA0000003) if p.endswith("a") else None)
    assert info.value.reason == "reparse"
