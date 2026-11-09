"""Tests for the record-directory vclip workflow: clips publish into upload/, and the
source recording plus Emby sidecars are removed only after a verified successful clip.

Run directly: ``python3 tests/test_record_clip.py`` (also pytest-compatible).
"""

import sys
import tempfile
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import core  # noqa: E402
import tasks  # noqa: E402


class _Result:
    def __init__(self, returncode: int):
        self.returncode = returncode
        self.stderr = "" if returncode == 0 else "boom"


def _fake_run(write_bytes: bytes, returncode: int = 0):
    captured = {}

    def run(cmd, **kwargs):
        captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(write_bytes)
        return _Result(returncode)

    return run, captured


def _make_record(root: Path) -> tuple[Path, dict]:
    record_dir = root / "record"
    record_dir.mkdir()
    cfg = {"record_dir": str(record_dir)}
    return record_dir, cfg


def test_is_record_source_excludes_upload_and_outside():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        record_dir, cfg = _make_record(root)
        upload = record_dir / "upload"
        upload.mkdir()
        (record_dir / "show.mkv").write_bytes(b"x")
        (upload / "show.mkv").write_bytes(b"x")
        (root / "elsewhere.mkv").write_bytes(b"x")

        assert core.is_record_source(record_dir / "show.mkv", cfg)
        assert not core.is_record_source(upload / "show.mkv", cfg)
        assert not core.is_record_source(root / "elsewhere.mkv", cfg)
        assert core.record_upload_path(record_dir / "show.mkv", cfg) == upload / "show.mkv"


def test_clip_stages_hidden_file_then_renames():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "show.mkv"
        src.write_bytes(b"source")
        out = root / "out" / "show.mkv"
        run, captured = _fake_run(b"clip")

        with mock.patch("core.subprocess.run", side_effect=run):
            out_file, _log = core.clip_video_file(src, "00:00:10", "00:00:20", out)

        assert Path(captured["cmd"][-1]).name == ".show.tmp.mkv"
        assert out_file == out and out.read_bytes() == b"clip"
        assert not (out.parent / ".show.tmp.mkv").exists()
        assert src.read_bytes() == b"source"


def test_clip_failure_leaves_source_and_no_output():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "show.mkv"
        src.write_bytes(b"source")
        out = root / "out" / "show.mkv"
        run, _ = _fake_run(b"partial", returncode=1)

        with mock.patch("core.subprocess.run", side_effect=run):
            try:
                core.clip_video_file(src, "00:00:10", "00:00:20", out)
            except RuntimeError:
                pass
            else:
                raise AssertionError("expected RuntimeError on ffmpeg failure")

        assert src.read_bytes() == b"source"
        assert not out.exists()
        assert not (out.parent / ".show.tmp.mkv").exists()


def test_record_task_cleans_source_after_success():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        record_dir, cfg = _make_record(root)
        stem = "[2026.05.08][Douyu][Host]show"
        src = record_dir / f"{stem}.mkv"
        src.write_bytes(b"source")
        sidecars = [
            record_dir / f"{stem}-poster.jpg",
            record_dir / f"{stem}.jpg",
            record_dir / f"{stem}.nfo",
            record_dir / f"{stem} - mobile.mp4",
            record_dir / f"{stem} - mobile (1).mp4",
            record_dir / f"{stem} - mobile-poster.jpg",
            record_dir / f"{stem} - mobile (1)-poster.jpg",
        ]
        for f in sidecars:
            f.write_bytes(b"sidecar")
        unrelated = record_dir / "other.mkv"
        unrelated.write_bytes(b"other")

        output = core.record_upload_path(src, cfg)
        task = tasks.Task(
            id="clip_1",
            type="clip",
            status="pending",
            target=src.name,
            resolved_path=str(src),
            params={
                "start": "00:00:10",
                "end": "00:00:20",
                "out": str(output),
                "cleanup_source": True,
            },
        )
        run, _ = _fake_run(b"clip")

        with mock.patch("core.subprocess.run", side_effect=run):
            tasks._run_task_body(task, cfg)

        assert output.read_bytes() == b"clip"
        assert not src.exists()
        for f in sidecars:
            assert not f.exists(), f"{f.name} should have been removed"
        assert unrelated.read_bytes() == b"other"
        assert task.output_path == str(output)
        assert len(task.params["deleted_files"]) == 1 + len(sidecars)


def test_record_task_keeps_source_when_clip_fails():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        record_dir, cfg = _make_record(root)
        stem = "show"
        src = record_dir / f"{stem}.mkv"
        src.write_bytes(b"source")
        poster = record_dir / f"{stem}-poster.jpg"
        poster.write_bytes(b"poster")
        nfo = record_dir / f"{stem}.nfo"
        nfo.write_bytes(b"nfo")

        task = tasks.Task(
            id="clip_2",
            type="clip",
            status="pending",
            target=src.name,
            resolved_path=str(src),
            params={"start": "00:00:10", "end": "00:00:20", "cleanup_source": True},
        )
        run, _ = _fake_run(b"partial", returncode=1)

        with mock.patch("core.subprocess.run", side_effect=run):
            try:
                tasks._run_task_body(task, cfg)
            except RuntimeError:
                pass
            else:
                raise AssertionError("expected RuntimeError on ffmpeg failure")

        assert src.read_bytes() == b"source"
        assert poster.read_bytes() == b"poster"
        assert nfo.read_bytes() == b"nfo"
        assert not (record_dir / "upload" / src.name).exists()


if __name__ == "__main__":
    for name in sorted(n for n in globals() if n.startswith("test_")):
        globals()[name]()
        print(f"PASS {name}")
    print("ALL TESTS PASSED")
