"""Tests for job cleanup in sync_subtitles_once: a job whose Temp audio was deleted
was cancelled, so its .jobs entry must go; an unmounted output dir is not a deletion.

Run directly: ``python3 tests/test_sync_jobs_cancel.py`` (also pytest-compatible).
"""

import json
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import core  # noqa: E402


def _job(jobs_dir: Path, out_dir: Path, stem: str, audio: bool) -> Path:
    video = out_dir.parent / f"{stem}.mkv"
    video.write_bytes(b"x")
    mp3 = out_dir / f"{stem}.mp3"
    if audio:
        mp3.write_bytes(b"audio")
    job_file = jobs_dir / f"{video.name}.json"
    job_file.write_text(
        json.dumps({"video": str(video), "mp3": str(mp3), "created_at": 0}),
        encoding="utf-8",
    )
    return job_file


def test_deleted_audio_removes_job():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        out_dir = root / "Temp"
        out_dir.mkdir()
        jobs_dir = root / ".jobs"
        jobs_dir.mkdir()
        cfg = {"jobs_dir": str(jobs_dir), "out_dir": str(out_dir)}

        gone = _job(jobs_dir, out_dir, "cancelled", audio=False)
        alive = _job(jobs_dir, out_dir, "pending", audio=True)

        assert core.sync_subtitles_once(cfg) == 0
        assert not gone.exists()
        assert alive.exists()


def test_unmounted_out_dir_keeps_jobs():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        out_dir = root / "Temp"
        out_dir.mkdir()
        jobs_dir = root / ".jobs"
        jobs_dir.mkdir()
        cfg = {"jobs_dir": str(jobs_dir), "out_dir": str(out_dir)}

        # Share unmounted: the job's mp3 path lives under a directory that is gone.
        job = _job(jobs_dir, out_dir, "pending", audio=True)
        (out_dir / "pending.mp3").unlink()
        out_dir.rmdir()

        assert core.sync_subtitles_once(cfg) == 0
        assert job.exists()


def test_job_without_mp3_is_kept():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        out_dir = root / "Temp"
        out_dir.mkdir()
        jobs_dir = root / ".jobs"
        jobs_dir.mkdir()
        cfg = {"jobs_dir": str(jobs_dir), "out_dir": str(out_dir)}

        job_file = jobs_dir / "legacy.json"
        job_file.write_text(json.dumps({"video": str(root / "legacy.mkv")}), encoding="utf-8")

        assert core.sync_subtitles_once(cfg) == 0
        assert job_file.exists()


if __name__ == "__main__":
    for name in sorted(n for n in globals() if n.startswith("test_")):
        globals()[name]()
        print(f"PASS {name}")
    print("ALL TESTS PASSED")
