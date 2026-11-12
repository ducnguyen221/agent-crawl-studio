"""Where files may be written (SPEC FR4.1-FR4.5, FR3.7, CQ2/ADR-004).

Order of the checks: names and shape of `output_dir` -> hard floor (never allowed, even inside a configured
root) -> allow-list (`output_roots`) -> cloud trees (Cloud Files sync root first, attributes second) ->
reparse points from the root down -> free space. Nothing is created by `resolve_output`.

Every system call is a parameter with a real default (ctypes only inside the default functions), so the
branches run on any machine with injected fakes. Reparse points are read with `os.lstat`, never `os.stat`
(stat follows a junction and hides it)."""

import ctypes
import os
import re
import shutil
from ctypes import wintypes
from dataclasses import dataclass, field

from . import config as config_mod
from . import winpath
from .winpath import PathBlocked

GiB = 2 ** 30
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_OFFLINE = 0x1000
CLOUD_ATTRIBUTES = FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_OFFLINE
CLOUD_TAG_FAMILY, CLOUD_TAG_MASK = 0x9000001A, 0xFFFF0FFF
DRIVE_REMOTE = 4

OPCOS_PROTECTED = ("plugins", "runtime", "system", "memory", "brain")
HOME_PROTECTED = (".secret", ".claude", ".codex", ".gemini", ".ssh")
STARTUP_USER = ("Microsoft", "Windows", "Start Menu", "Programs", "Startup")


class WriteBlocked(Exception):
    """Not enough free space (OUTPUT_WRITE_FAILED)."""

    code = "OUTPUT_WRITE_FAILED"

    def __init__(self, missing_gib):
        super().__init__(missing_gib)
        self.missing_gib = missing_gib


@dataclass(frozen=True)
class Root:
    root: str  # the configured root the destination sits under
    parent: str  # the folder that will hold the job folder
    warnings: list = field(default_factory=list)


# --- real system calls (the only place ctypes is used) ------------------------------------------------------------
def default_drive_type_of(path):
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    return int(kernel.GetDriveTypeW(path))


def default_sync_root_of(path):
    """True when `path` is (or is inside) a Cloud Files sync root: CfGetSyncRootInfoByPath succeeds (0x0);
    0x80070186 means it is not one."""
    cloud = ctypes.WinDLL("cldapi.dll")
    cloud.CfGetSyncRootInfoByPath.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    cloud.CfGetSyncRootInfoByPath.restype = ctypes.c_long
    buffer = ctypes.create_string_buffer(512)
    returned = wintypes.DWORD(0)
    return (cloud.CfGetSyncRootInfoByPath(path, 0, buffer, 512, ctypes.byref(returned)) & 0xFFFFFFFF) == 0


def default_attrs_of(path):
    """(file attributes, reparse tag) of the entry itself (lstat), or None when it does not exist."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info.st_file_attributes, getattr(info, "st_reparse_tag", 0)


def _nearest_dir(path):
    probe = os.path.normpath(path)
    while not os.path.isdir(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    return probe


def default_free_bytes_of(path):
    return shutil.disk_usage(_nearest_dir(path)).free


# --- helpers ----------------------------------------------------------------------------------------------------------
def _key(path):
    return os.path.normcase(os.path.normpath(path))


def _under(path, base):
    key, root = _key(path), _key(base).rstrip("\\")
    return key == root or key.startswith(root + "\\")


def _ancestors(path):
    found = []
    current = os.path.normpath(path)
    while True:
        found.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            return found
        current = parent


def _forms(path, realpath):
    """The lexical path and where it really points."""
    forms = [os.path.normpath(path)]
    if forms[0].startswith("\\\\"):  # UNC: never ask the network where it points
        return forms
    real = os.path.normpath(realpath(path))
    if _key(real) != _key(forms[0]):
        forms.append(real)
    return forms


def _forbidden(path, env, drive_type_of):
    """The hard floor: UNC, mapped network drive, any `.git`, Startup folders, OpcOS protected folders,
    secret and agent folders of the home, and a PATH entry itself (equality only)."""
    path = os.path.normpath(path)
    if path.startswith("\\\\"):
        return True
    drive = os.path.splitdrive(path)[0]
    if drive and drive_type_of(drive + "\\") == DRIVE_REMOTE:
        return True
    if any(part.lower() == ".git" for part in path.split("\\")):
        return True
    appdata, program_data = env.get("APPDATA"), env.get("ProgramData")
    if appdata and _under(path, os.path.join(appdata, *STARTUP_USER)):
        return True
    if program_data and _under(path, os.path.join(program_data, *STARTUP_USER)):
        return True
    opcos = config_mod.opcos_root(env)
    if opcos and any(_under(path, os.path.join(opcos, name)) for name in OPCOS_PROTECTED):
        return True
    home = env.get("USERPROFILE")
    if home and any(_under(path, os.path.join(home, name)) for name in HOME_PROTECTED):
        return True
    for entry in (env.get("PATH") or "").split(os.pathsep):
        if entry and _key(entry) == _key(path):
            return True
    return False


def usable_roots(config, env, *, drive_type_of=default_drive_type_of, realpath=os.path.realpath):
    """(roots that pass the floor, warnings). A root that breaks the floor is dropped with `root_ignored`."""
    kept, dropped = [], False
    for root in config.output_roots:
        if any(_forbidden(form, env, drive_type_of) for form in _forms(root, realpath)):
            dropped = True
        else:
            kept.append(root)
    return kept, (["root_ignored"] if dropped else [])


def _shape(output_dir):
    """Reject the dangerous FORMS of output_dir; True when it is absolute."""
    flat = output_dir.replace("/", "\\")
    if flat.startswith("\\\\"):
        raise PathBlocked("prefix")
    if re.match(r"^[A-Za-z]:(?!\\)", flat):
        raise PathBlocked("drive_relative")
    if flat.startswith("\\"):
        raise PathBlocked("rooted")
    if ".." in flat.split("\\"):
        raise PathBlocked("dotdot")
    return bool(re.match(r"^[A-Za-z]:\\", flat))


def _cloud(path, attrs_of, sync_root_of):
    if sync_root_of(path):
        raise PathBlocked("cloud")
    found = attrs_of(path)
    if found:
        attrs, tag = found
        if attrs & CLOUD_ATTRIBUTES or (tag & CLOUD_TAG_MASK) == CLOUD_TAG_FAMILY:
            raise PathBlocked("cloud")


def _walk_down(root, path):
    """Every path from `root` down to `path` (both included); `path` must be under `root`."""
    parts = os.path.relpath(path, root).split("\\") if _key(path) != _key(root) else []
    current = os.path.normpath(root)
    steps = [current]
    for part in parts:
        current = os.path.join(current, part)
        steps.append(current)
    return steps


def _no_reparse(steps, attrs_of):
    for step in steps:
        found = attrs_of(step)
        if found and found[0] & FILE_ATTRIBUTE_REPARSE_POINT:
            raise PathBlocked("reparse")


def resolve_output(output_dir, config, env, *, attrs_of=default_attrs_of, drive_type_of=default_drive_type_of,
                   sync_root_of=default_sync_root_of, free_bytes_of=default_free_bytes_of, realpath=os.path.realpath,
                   required_free=0):
    """Decide where the job folder may go. Raises PathBlocked (OUTPUT_PATH_BLOCKED) or WriteBlocked
    (OUTPUT_WRITE_FAILED). Creates nothing."""
    roots, warnings = usable_roots(config, env, drive_type_of=drive_type_of, realpath=realpath)
    warnings = list(config.warnings) + warnings
    jobs = os.path.join(config.work_root, "jobs")
    if output_dir is None:
        lexical, user_parts = os.path.normpath(jobs), []
    elif _shape(output_dir):
        lexical = os.path.normpath(output_dir)
        user_parts = []
    else:
        for part in output_dir.replace("/", "\\").split("\\"):
            winpath.check_component(part)
        lexical = os.path.normpath(os.path.join(jobs, output_dir))
        user_parts = output_dir.replace("/", "\\").split("\\")
    if output_dir is not None and not user_parts:
        under = [r for r in roots if _under(lexical, r)]
        if under:
            base = max(under, key=len)
            user_parts = [p for p in os.path.relpath(lexical, base).split("\\") if p not in ("", ".")]
    winpath.check_path(lexical, user_parts=user_parts)
    for part in user_parts:
        winpath.check_component(part)

    forms = _forms(lexical, realpath)
    if any(_forbidden(form, env, drive_type_of) for form in forms):
        raise PathBlocked("forbidden_place")
    chosen = None
    for root in roots:
        real_root = os.path.normpath(realpath(root))
        if _under(lexical, root) and all(_under(form, real_root) or _under(form, root) for form in forms):
            if chosen is None or len(root) > len(chosen):
                chosen = root
    if chosen is None:
        raise PathBlocked("not_in_allow_list")

    seen = set()
    for form in forms:
        for ancestor in _ancestors(form):
            if _key(ancestor) not in seen:
                seen.add(_key(ancestor))
                _cloud(ancestor, attrs_of, sync_root_of)
    _no_reparse(_walk_down(chosen, lexical), attrs_of)

    free = free_bytes_of(_nearest_dir(lexical))
    if free < required_free:
        raise WriteBlocked(-(-(required_free - free) // GiB))
    return Root(chosen, lexical, warnings)


def post_create_check(path, root, *, attrs_of=default_attrs_of, realpath=os.path.realpath):
    """After a folder or file was created: it still sits under the root (really, not only by name) and no
    component from the root down is a junction or symlink."""
    lexical = os.path.normpath(path)
    if not _under(lexical, root.root) or not _under(realpath(lexical), realpath(root.root)):
        raise PathBlocked("outside_root")
    _no_reparse(_walk_down(root.root, lexical), attrs_of)


def create_job(root, job_id, *, attrs_of=default_attrs_of, **_unused):
    """Create `<parent>/<job_id>` (it must not exist) and re-check it. A job folder that fails the re-check
    is removed again."""
    winpath.check_component(job_id)
    os.makedirs(root.parent, exist_ok=True)
    job = os.path.join(root.parent, job_id)
    os.mkdir(job)
    try:
        post_create_check(job, root, attrs_of=attrs_of)
    except PathBlocked:
        try:
            os.rmdir(job)
        except OSError:
            pass
        raise
    return job
