"""`outroot.confined_work(arg, work_root, realpath=os.path.realpath)`: the folder `--work` of the smoke script must
resolve (links followed) to a place under <work_root>/jobs and must not already sit inside a `smoke-` folder. It
returns the resolved Path, or None; it creates nothing. (The smoke script used to carry this as `_confined_work`.)"""

import os
from pathlib import Path

from agent_crawl_studio import outroot


def test_a_folder_under_jobs_is_accepted_and_returned_resolved(tmp_path):
    work = tmp_path / "root" / "jobs" / "w"
    got = outroot.confined_work(str(work), str(tmp_path / "root"))
    assert got == Path(os.path.realpath(work)) and not work.exists()


def test_jobs_itself_is_accepted(tmp_path):
    assert outroot.confined_work(str(tmp_path / "root" / "jobs"), str(tmp_path / "root")) is not None


def test_outside_jobs_is_refused(tmp_path):
    root = str(tmp_path / "root")
    for place in (tmp_path / "elsewhere", tmp_path / "root", tmp_path / "root" / "jobs" / ".." / "x"):
        assert outroot.confined_work(str(place), root) is None


def test_a_smoke_folder_in_the_path_is_refused_no_nesting(tmp_path):
    root = str(tmp_path / "root")
    assert outroot.confined_work(str(tmp_path / "root" / "jobs" / "SMOKE-20261113-000000-abcdef" / "x"), root) is None


def test_a_link_that_leads_out_is_refused_by_the_resolved_path(tmp_path):
    root = str(tmp_path / "root")
    outside = tmp_path / "outside"

    def realpath(path):
        return str(outside / "x") if path.endswith("link") else os.path.realpath(path)
    assert outroot.confined_work(str(tmp_path / "root" / "jobs" / "link"), root, realpath=realpath) is None
