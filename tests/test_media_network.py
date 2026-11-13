"""SPR-04 TK13 (SPEC FR29.4, FR29.1; pays RQ-020): the acceptance video through `run`, against the real Internet.
Marked `network`: NOT_RUN without `--run-network`. Same way of running as `tests/test_mvp_network.py` (the Python of
the venv `core`, which has yt-dlp; ffmpeg and node must be on the machine, else the item is `DEPENDENCY_MISSING`).

    pytest tests/test_media_network.py -m network --run-network -q
"""

import json

import pytest

from test_mvp_network import check_artifact_hash, core_python, fixture_url, run_request

pytestmark = pytest.mark.network


def test_the_acceptance_video_succeeds_as_a_video_file_made_by_yt_dlp(tmp_path):
    python = core_python()
    url = fixture_url("request-files.json", 2, "the acceptance video")
    request = tmp_path / "request-video.json"
    request.write_text(json.dumps({"schema_version": 1, "mode": "media", "urls": [{"url": url}]}), encoding="utf-8")
    work = tmp_path / "work"
    code, result = run_request(python, work, request)
    item = result["items"][0]
    assert (code, result["status"], item["status"]) == (0, "succeeded", "succeeded"), item.get("error")
    artifact = item["artifacts"][0]
    assert artifact["mime"].startswith("video/") and artifact["engine"] == "yt-dlp"
    assert artifact["bytes"] > 0
    check_artifact_hash(work, result, artifact)
