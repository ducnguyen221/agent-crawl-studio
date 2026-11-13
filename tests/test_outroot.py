"""SPEC FR4.1-FR4.6, FR3.7 (free space), CQ2: where files may be written. Fakes are injected for the
system calls so every branch runs on any machine; the REAL calls (GetDriveTypeW, lstat, junctions,
CfGetSyncRootInfoByPath) are exercised in the last section."""

import os

import pytest

from agent_crawl_studio import config, limits, outroot, winpath
from agent_crawl_studio.winpath import PathBlocked

GiB = 2 ** 30


@pytest.fixture
def world(tmp_path):
    """A fake machine layout under tmp_path: work root, OpcOS, home, AppData, ProgramData."""
    w = {
        "work": str(tmp_path / "w"), "opcos": str(tmp_path / "opcos"), "home": str(tmp_path / "home"),
        "appdata": str(tmp_path / "appdata"), "pdata": str(tmp_path / "pdata"), "pathdir": str(tmp_path / "w" / "jobs" / "pathdir"),
        "extra": str(tmp_path / "extra"),
    }
    w["env"] = {"CRAWL_STUDIO_WORK": w["work"], "OPCOS_ROOT": w["opcos"], "USERPROFILE": w["home"],
                "APPDATA": w["appdata"], "ProgramData": w["pdata"], "PATH": os.pathsep.join([w["pathdir"], str(tmp_path / "bin")])}
    return w


def cfg(world, *extra_roots):
    return config.Config(world["work"], [world["work"], *extra_roots], tuple(config.DEFAULT_MEDIA_HOSTS), [])


FAKES = dict(
    attrs_of=lambda path: None,
    drive_type_of=lambda path: 3,
    sync_root_of=lambda path: False,
    free_bytes_of=lambda path: 10 ** 15,
)


def resolve(world, output_dir=None, *extra_roots, required_free=0, **override):
    fakes = dict(FAKES)
    fakes.update(override)
    return outroot.resolve_output(output_dir, cfg(world, *extra_roots), world["env"], required_free=required_free, **fakes)


def blocked(world, output_dir, *extra_roots, **override):
    with pytest.raises(PathBlocked) as info:
        resolve(world, output_dir, *extra_roots, **override)
    assert info.value.code == "OUTPUT_PATH_BLOCKED" and info.value.reason in winpath.REASONS
    return info.value.reason


# --- allow-list and relative output_dir -----------------------------------------------------------------------
def test_default_parent_is_work_root_jobs(world):
    got = resolve(world)
    assert got.parent == os.path.join(world["work"], "jobs") and got.root == world["work"] and got.warnings == []


def test_relative_output_dir_is_under_work_root_jobs(world):
    assert resolve(world, "batch1").parent == os.path.join(world["work"], "jobs", "batch1")
    assert resolve(world, "a/b\\c").parent == os.path.join(world["work"], "jobs", "a", "b", "c")


def test_absolute_output_dir_inside_the_work_root_or_an_extra_root_is_fine(world):
    inside = os.path.join(world["work"], "custom")
    assert resolve(world, inside).root == world["work"]
    assert resolve(world, os.path.join(world["extra"], "x"), world["extra"]).root == world["extra"]


def test_absolute_output_dir_outside_every_root_is_blocked(world, tmp_path):
    assert blocked(world, str(tmp_path / "elsewhere")) == "not_in_allow_list"
    assert blocked(world, os.path.join(world["extra"], "x")) == "not_in_allow_list"  # the extra root is not configured here


def test_a_sibling_whose_name_starts_with_the_root_name_is_not_inside_it(world):
    assert blocked(world, world["work"] + "2\\x") == "not_in_allow_list"


def test_comparison_ignores_case(world):
    assert resolve(world, os.path.join(world["work"].upper(), "x")).root == world["work"]


@pytest.mark.parametrize("bad", ["C:foo", "C:", "\\foo", "/foo", "..", "../x", "a/../b", "..\\..\\x", "a\\..", "./..", "x/./y/.."])
def test_drive_relative_rooted_and_dotdot_forms_are_blocked(world, bad):
    blocked(world, bad)


@pytest.mark.parametrize("bad", ["\\\\?\\C:\\x", "\\\\.\\pipe\\x", "//?/C:/x", "\\\\srv\\share\\x", "//srv/share/x", "\\\\?\\UNC\\srv\\share"])
def test_prefixes_and_unc_are_blocked(world, bad):
    blocked(world, bad)


@pytest.mark.parametrize("bad", ["a:b", "a.txt:stream", "x.", "x ", "CON", "con.txt", "NUL", "COM\u00b9", "LPT\u00b3", "PROGRA~1", "a/CON/b", "a/b:c", "a\x01"])
def test_windows_names_in_the_user_part_are_blocked(world, bad):
    blocked(world, bad)


def test_a_short_name_in_an_ancestor_of_the_root_is_not_blocked(tmp_path):
    work = str(tmp_path / "DUCNGU~1" / "w")
    world = {"work": work, "opcos": str(tmp_path / "o"), "home": str(tmp_path / "h"), "appdata": "", "pdata": ""}
    world["env"] = {"CRAWL_STUDIO_WORK": work, "OPCOS_ROOT": world["opcos"], "USERPROFILE": world["home"]}
    assert resolve(world).parent == os.path.join(work, "jobs")
    blocked(world, "PROGRA~1")


def test_length_limits(world):
    blocked(world, "a" * 121)
    long = "\\".join(["d" * 100] * 3)
    assert blocked(world, long) == "too_long"


# --- hard floor -------------------------------------------------------------------------------------------------
def test_git_directories_are_blocked_even_inside_an_allowed_root(world):
    assert blocked(world, "proj/.git/hooks") == "forbidden_place"
    blocked(world, ".git")
    blocked(world, "x/.GIT/y")


def test_startup_folders_are_blocked_even_when_their_parent_is_a_configured_root(world, tmp_path):
    user = os.path.join(world["appdata"], "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    machine = os.path.join(world["pdata"], "Microsoft", "Windows", "Start Menu", "Programs", "StartUp")
    for place, root in ((user, world["appdata"]), (machine, world["pdata"])):
        assert blocked(world, place, root) == "forbidden_place"
        assert blocked(world, os.path.join(place, "sub"), root) == "forbidden_place"
    assert resolve(world, os.path.join(world["appdata"], "other"), world["appdata"]).root == world["appdata"]


@pytest.mark.parametrize("name", ["plugins", "runtime", "system", "memory", "brain"])
def test_opcos_protected_folders_are_blocked(world, name):
    assert blocked(world, os.path.join(world["opcos"], name), world["opcos"]) == "forbidden_place"
    blocked(world, os.path.join(world["opcos"], name, "deep", "x"), world["opcos"])
    assert resolve(world, os.path.join(world["opcos"], "workspaces", "x"), world["opcos"]).root == world["opcos"]


@pytest.mark.parametrize("name", [".secret", ".claude", ".codex", ".gemini", ".ssh"])
def test_home_secret_and_agent_folders_are_blocked(world, name):
    assert blocked(world, os.path.join(world["home"], name), world["home"]) == "forbidden_place"
    blocked(world, os.path.join(world["home"], name, "sub"), world["home"])
    assert resolve(world, os.path.join(world["home"], "Downloads"), world["home"]).root == world["home"]


def test_path_entries_are_blocked_by_equality_only(world):
    blocked(world, world["pathdir"])
    assert resolve(world, os.path.join(world["pathdir"], "sub")).root == world["work"]  # a child of a PATH entry
    assert resolve(world).parent == os.path.join(world["work"], "jobs")  # PATH entry below the parent does not block it


def test_a_path_entry_that_is_an_ancestor_of_the_root_does_not_block_the_root(world, tmp_path):
    world["env"]["PATH"] = str(tmp_path)  # an ANCESTOR of the work root
    assert resolve(world).root == world["work"]


def test_a_mapped_network_drive_is_blocked(world):
    assert blocked(world, None, drive_type_of=lambda drive: 4) == "forbidden_place"
    seen = []
    resolve(world, drive_type_of=lambda drive: seen.append(drive) or 3)
    assert seen and all(d.endswith("\\") and len(d) == 3 for d in seen)


def test_floor_is_checked_for_every_form_of_the_path(world):
    blocked(world, os.path.join(world["home"], ".SECRET", "x"), world["home"])


# --- usable roots (floor applied when the config is loaded) ----------------------------------------------------------
def test_floor_roots_are_dropped_with_one_warning(world):
    roots, warnings = outroot.usable_roots(
        cfg(world, os.path.join(world["opcos"], "plugins"), os.path.join(world["home"], ".secret"), "\\\\srv\\share\\x",
            os.path.join(world["opcos"], "workspaces", "ok"), world["extra"]),
        world["env"], drive_type_of=lambda p: 3)
    assert roots == [world["work"], os.path.join(world["opcos"], "workspaces", "ok"), world["extra"]]
    assert warnings == ["root_ignored"]


def test_a_remote_drive_root_is_dropped(world):
    roots, warnings = outroot.usable_roots(cfg(world, world["extra"]), world["env"], drive_type_of=lambda p: 4)
    assert roots == [] and warnings == ["root_ignored"]


def test_clean_roots_give_no_warning(world):
    assert outroot.usable_roots(cfg(world), world["env"], drive_type_of=lambda p: 3) == ([world["work"]], [])


def test_resolve_output_reports_root_ignored(world):
    got = resolve(world, None, os.path.join(world["opcos"], "runtime"))
    assert got.warnings == ["root_ignored"]


# --- cloud: sync root first, attributes second ----------------------------------------------------------------------
def test_a_sync_root_in_any_ancestor_blocks(world, tmp_path):
    asked = []

    def sync(path):
        asked.append(path)
        return os.path.normcase(path) == os.path.normcase(str(tmp_path))

    assert blocked(world, None, sync_root_of=sync) == "cloud"
    assert any(os.path.normcase(p) == os.path.normcase(world["work"]) for p in asked)


def test_a_sync_root_that_is_the_target_or_the_root_blocks(world):
    blocked(world, "batch1", sync_root_of=lambda p: os.path.normcase(p).endswith("batch1"))
    blocked(world, None, sync_root_of=lambda p: os.path.normcase(p) == os.path.normcase(world["work"]))


def attrs_map(table):
    norm = {os.path.normcase(k): v for k, v in table.items()}
    return lambda path: norm.get(os.path.normcase(path))


@pytest.mark.parametrize("attrs,tag", [(0x400000, 0), (0x40000, 0), (0x1000, 0), (0x400, 0x9000001A), (0x400, 0x9000F01A), (0x400400, 0)])
def test_cloud_attribute_masks_block_when_a_sync_root_call_says_no(world, tmp_path, attrs, tag):
    fake = attrs_map({str(tmp_path): (attrs, tag)})
    assert blocked(world, None, attrs_of=fake) == "cloud"


def test_plain_directory_attributes_pass(world, tmp_path):
    fake = attrs_map({str(tmp_path): (0x10, 0), world["work"]: (0x10, 0)})
    assert resolve(world, None, attrs_of=fake).root == world["work"]


def test_a_non_cloud_reparse_point_above_the_root_is_legitimate(world, tmp_path):
    fake = attrs_map({str(tmp_path): (0x410, 0xA0000003)})
    assert resolve(world, None, attrs_of=fake).root == world["work"]


@pytest.mark.parametrize("where", ["root", "middle", "target"])
def test_a_reparse_point_at_or_below_the_root_blocks(world, where):
    path = {"root": world["work"], "middle": os.path.join(world["work"], "jobs", "a"), "target": os.path.join(world["work"], "jobs", "a", "b")}[where]
    assert blocked(world, "a/b", attrs_of=attrs_map({path: (0x410, 0xA0000003)})) == "reparse"


# --- free space (FR3.7) ------------------------------------------------------------------------------------------------
def test_not_enough_free_space_is_output_write_failed_with_the_missing_gib(world):
    with pytest.raises(outroot.WriteBlocked) as info:
        resolve(world, required_free=7 * GiB, free_bytes_of=lambda path: 4 * GiB + 1)
    assert info.value.code == "OUTPUT_WRITE_FAILED" and info.value.missing_gib == 3


def test_exactly_enough_free_space_passes_and_the_formula_comes_from_limits(world):
    effective, _ = limits.resolve({})
    need = limits.required_free_bytes(effective, 1)
    assert resolve(world, required_free=need, free_bytes_of=lambda path: need).root == world["work"]


def test_free_space_is_asked_of_an_existing_folder(world, tmp_path):
    asked = []
    resolve(world, required_free=1, free_bytes_of=lambda path: asked.append(path) or 10 ** 15)
    assert asked and os.path.isdir(asked[0])


# --- creating the job folder ---------------------------------------------------------------------------------------------
def test_create_job_makes_a_new_folder_and_refuses_an_existing_one(world):
    got = resolve(world)
    job = outroot.create_job(got, "20261113-101010-abcdef", **FAKES)
    assert os.path.isdir(job) and job == os.path.join(got.parent, "20261113-101010-abcdef")
    with pytest.raises(outroot.JobExists):
        outroot.create_job(got, "20261113-101010-abcdef", **FAKES)


def test_create_job_refuses_a_bad_job_id(world):
    got = resolve(world)
    for bad in ("..", "a/b", "CON", "x:y", ""):
        with pytest.raises(PathBlocked):
            outroot.create_job(got, bad, **FAKES)
    assert not os.path.exists(got.parent)


# --- REAL calls (no fake) --------------------------------------------------------------------------------------------------
def test_real_get_drive_type_of_the_system_drive_is_fixed(tmp_path):
    drive = os.path.splitdrive(str(tmp_path))[0] + "\\"
    assert outroot.default_drive_type_of(drive) == 3


def test_real_attrs_of_a_directory_and_of_a_real_junction(tmp_path):
    import _winapi
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    _winapi.CreateJunction(str(target), str(link))
    try:
        attrs, tag = outroot.default_attrs_of(str(link))
        assert attrs & 0x400 and tag == 0xA0000003, "lstat sees the reparse point"
        plain, plain_tag = outroot.default_attrs_of(str(target))
        assert plain & 0x10 and not plain & 0x400 and plain_tag == 0
        assert os.stat(str(link)).st_file_attributes & 0x400 == 0, "os.stat follows the junction, which is why lstat is used"
        assert outroot.default_attrs_of(str(tmp_path / "missing")) is None
    finally:
        os.rmdir(str(link))
    assert target.is_dir()


def test_real_cf_get_sync_root_info_of_a_plain_folder_is_not_a_sync_root(tmp_path):
    assert outroot.default_sync_root_of(str(tmp_path)) is False
    assert outroot.default_sync_root_of(str(tmp_path / "missing")) is False


def test_real_cf_get_sync_root_info_finds_a_real_onedrive_root_if_one_exists():
    home = os.environ.get("USERPROFILE")
    roots = [os.path.join(home, n) for n in os.listdir(home) if n.lower().startswith("onedrive")] if home else []
    if not roots:
        pytest.skip("BLOCKED_ENV: there is no folder named OneDrive* in the user profile of this machine")
    # RF61: the candidates come from the folder names, NOT from the function under test
    assert any(outroot.default_sync_root_of(root) is True for root in roots), roots


def test_real_free_bytes_of_the_temp_folder_is_positive(tmp_path):
    assert outroot.default_free_bytes_of(str(tmp_path / "does" / "not" / "exist")) > 0


def test_resolve_with_all_real_defaults_on_a_scratch_folder(world):
    os.makedirs(world["work"])
    got = outroot.resolve_output(None, cfg(world), world["env"])
    assert got.parent == os.path.join(world["work"], "jobs")


def test_a_real_junction_in_the_middle_of_the_path_is_blocked(world, tmp_path):
    import _winapi
    outside = tmp_path / "outside"
    outside.mkdir()
    jobs = os.path.join(world["work"], "jobs")
    os.makedirs(jobs)
    link = os.path.join(jobs, "link")
    _winapi.CreateJunction(str(outside), link)
    try:
        for bad in ("link", "link/sub"):
            with pytest.raises(PathBlocked):
                outroot.resolve_output(bad, cfg(world), world["env"])
        assert list(outside.iterdir()) == [], "nothing was created behind the junction"
    finally:
        os.rmdir(link)


def test_a_real_junction_as_the_root_itself_is_blocked(world, tmp_path):
    import _winapi
    outside = tmp_path / "outside2"
    outside.mkdir()
    _winapi.CreateJunction(str(outside), world["work"])
    try:
        with pytest.raises(PathBlocked) as info:
            outroot.resolve_output(None, cfg(world), world["env"])
        assert info.value.reason == "reparse"
    finally:
        os.rmdir(world["work"])


def test_a_junction_made_after_the_check_is_caught_after_the_job_folder_is_created(world, tmp_path):
    import _winapi
    got = outroot.resolve_output("late", cfg(world), world["env"])
    outside = tmp_path / "outside3"
    outside.mkdir()
    os.makedirs(os.path.dirname(got.parent))
    _winapi.CreateJunction(str(outside), got.parent)  # an attacker swaps the folder after the check
    try:
        with pytest.raises(PathBlocked) as info:
            outroot.create_job(got, "20261113-101010-abcdef")
        assert info.value.reason in ("reparse", "outside_root")
        assert list(outside.iterdir()) == [], "the job folder that was made behind the junction is removed again"
    finally:
        os.rmdir(got.parent)


def test_post_create_check_passes_for_a_normal_job_folder_and_for_a_file(world):
    got = resolve(world)
    job = outroot.create_job(got, "20261113-101010-aaaaaa", **FAKES)
    outroot.post_create_check(job, got, attrs_of=outroot.default_attrs_of)
    path = os.path.join(job, "f.bin")
    open(path, "wb").close()
    outroot.post_create_check(path, got)


def test_post_create_check_refuses_a_path_that_left_the_root(world, tmp_path):
    got = resolve(world)
    with pytest.raises(PathBlocked) as info:
        outroot.post_create_check(str(tmp_path / "elsewhere" / "job"), got)
    assert info.value.reason == "outside_root"


def test_the_longest_matching_root_is_reported(world):
    inner = os.path.join(world["work"], "jobs", "inner")
    assert resolve(world, os.path.join(inner, "x"), inner).root == inner
    assert resolve(world, os.path.join(world["work"], "jobs", "other")).root == world["work"]


def test_the_longest_root_wins_whatever_the_order_of_the_config(world):
    inner = os.path.join(world["work"], "jobs", "inner")
    reordered = config.Config(world["work"], [inner, world["work"]], tuple(config.DEFAULT_MEDIA_HOSTS), [])
    got = outroot.resolve_output(os.path.join(inner, "x"), reordered, world["env"], **FAKES)
    assert got.root == inner


def test_a_job_folder_that_cannot_be_removed_after_a_failed_check_still_reports_the_block(world, monkeypatch):
    got = resolve(world)

    def boom(path):
        raise OSError("in use")

    monkeypatch.setattr(os, "rmdir", boom)
    with pytest.raises(PathBlocked):
        outroot.create_job(got, "20261113-101010-bbbbbb", attrs_of=lambda p: (0x410, 0xA0000003) if p.endswith("bbbbbb") else None)
