"""Tests for the periodic scan's automatic extraction.

The scan may only hand a file to ffmpeg once it is fully written: a file that is
still being downloaded or moved into the library must be deferred, never extracted.
Files that already ship a subtitle/lyric sidecar need no transcription at all.

Run directly: ``python3 tests/test_auto_extract_scan.py`` (also pytest-compatible).
"""

import os
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import core  # noqa: E402
import tasks  # noqa: E402

EXTS = [".mp4", ".mkv", ".flac"]


def _touch(path: Path, age_seconds: float = 3600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    if age_seconds:
        ts = time.time() - age_seconds
        os.utime(path, (ts, ts))
    return path


def _cfg(td: str, **overrides):
    cfg = {
        "scan_dirs": [],
        "index_extensions": list(EXTS),
        "db_path": str(Path(td) / "index.db"),
        "out_dir": str(Path(td) / "out"),
        "auto_extract": {"settle_seconds": 900, "sample_wait_seconds": 0},
    }
    cfg.update(overrides)
    return cfg


def _init_index(cfg) -> None:
    core.refresh_index(cfg, incremental=True)


def test_refresh_index_reports_only_new_files():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "lib"
        root.mkdir()
        a = _touch(root / "a.mkv")
        b = _touch(root / "b.mkv")
        cfg = _cfg(td, scan_dirs=[str(root)])

        first = core.refresh_index(cfg, track_new=True)
        assert sorted(Path(p).name for p in first["new_files"]) == ["a.mkv", "b.mkv"]

        second = core.refresh_index(cfg, track_new=True)
        assert second["new_files"] == []

        c = _touch(root / "c.mkv")
        third = core.refresh_index(cfg, track_new=True)
        assert [Path(p).name for p in third["new_files"]] == [c.name]

        # Untracked callers keep the small return shape.
        assert "new_files" not in core.refresh_index(cfg)


def test_plan_skips_files_with_sidecars_and_existing_output():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "lib"
        root.mkdir()
        with_sub = _touch(root / "with_sub.mkv")
        _touch(root / "with_sub.srt")
        naked = _touch(root / "naked.mkv")
        done = _touch(root / "done.mkv")
        out = Path(td) / "out"
        out.mkdir()
        (out / "done.mp3").write_text("audio")

        cfg = _cfg(td, scan_dirs=[str(root)])
        _init_index(cfg)

        plan = core.plan_auto_extraction(cfg, [str(with_sub), str(naked), str(done)])
        assert plan["batches"] == [[naked]]
        assert plan["counts"]["has_subtitle"] == 1
        assert plan["counts"]["output_exists"] == 1
        assert plan["counts"]["queued"] == 1


def test_plan_defers_fresh_file_then_queues_it_on_next_scan():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "lib"
        root.mkdir()
        fresh = _touch(root / "fresh.mkv", age_seconds=0)
        cfg = _cfg(td, scan_dirs=[str(root)])
        _init_index(cfg)

        first = core.plan_auto_extraction(cfg, [str(fresh)])
        assert first["batches"] == []
        assert first["counts"]["deferred"] == 1

        # Next scan: the file is no longer "new", but the deferred row makes it
        # eligible again once it has settled (mtime now older than the settle window).
        ts = time.time() - 3600
        os.utime(fresh, (ts, ts))
        second = core.plan_auto_extraction(cfg, [])
        assert second["batches"] == [[fresh]]


def test_plan_defers_download_in_progress_markers():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "lib"
        root.mkdir()
        candidates = [
            _touch(root / "downloading.mkv.part"),
            _touch(root / "qb.mkv.!qB"),
            _touch(root / "syno.mkv.downloading"),
            _touch(root / ".incomplete" / "nested.mkv"),
        ]
        # A side-car control file beside an otherwise complete-looking payload.
        control = _touch(root / "control.mkv")
        _touch(root / "control.mkv.aria2")
        candidates.append(control)

        cfg = _cfg(td, scan_dirs=[str(root)])
        _init_index(cfg)

        for p in candidates:
            assert core.is_write_in_progress(p), p.name

        plan = core.plan_auto_extraction(cfg, [str(p) for p in candidates])
        assert plan["batches"] == []
        assert plan["counts"]["queued"] == 0


def test_plan_caps_and_batches_ready_files():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "lib"
        root.mkdir()
        files = [_touch(root / f"ep{i:02d}.mkv") for i in range(5)]
        cfg = _cfg(
            td,
            scan_dirs=[str(root)],
            auto_extract={"settle_seconds": 900, "sample_wait_seconds": 0,
                          "batch_size": 2, "max_per_scan": 4},
        )
        _init_index(cfg)

        plan = core.plan_auto_extraction(cfg, [str(p) for p in files])
        assert [len(b) for b in plan["batches"]] == [2, 2]
        assert plan["counts"]["queued"] == 4
        assert plan["counts"]["deferred"] == 1


def test_plan_removes_state_for_deleted_files():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "lib"
        root.mkdir()
        gone = _touch(root / "gone.mkv", age_seconds=0)
        cfg = _cfg(td, scan_dirs=[str(root)])
        _init_index(cfg)
        core.plan_auto_extraction(cfg, [str(gone)])
        gone.unlink()

        plan = core.plan_auto_extraction(cfg, [])
        assert plan["counts"]["missing"] == 1
        assert plan["batches"] == []


def test_plan_skips_excluded_paths():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "lib"
        record = root / "record"
        rec = _touch(record / "stream.mkv")
        keep = _touch(root / "asmr" / "track.mkv")
        cfg = _cfg(
            td,
            scan_dirs=[str(root)],
            auto_extract={"settle_seconds": 900, "sample_wait_seconds": 0,
                          "exclude_paths": [str(record)]},
        )
        _init_index(cfg)

        plan = core.plan_auto_extraction(cfg, [str(rec), str(keep)])
        assert plan["batches"] == [[keep]]
        assert plan["counts"]["excluded"] == 1
        # Excluded files are never re-queued by later scans.
        assert core.plan_auto_extraction(cfg, [])["batches"] == []


def test_run_task_now_executes_inline_without_a_worker():
    with tempfile.TemporaryDirectory() as td:
        cfg = {
            "tasks_dir": str(Path(td) / "tasks"),
            "out_dir": str(Path(td) / "out"),
            "jobs_dir": str(Path(td) / "jobs"),
        }
        src = _touch(Path(td) / "ep01.mkv")
        calls = []

        def fake_extract(input_file, out_dir, jobs_dir, format):  # noqa: A002
            out = Path(out_dir) / f"{Path(input_file).stem}.{format}"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("audio")
            calls.append(Path(input_file))
            return out, "ok"

        original = tasks.extract_audio_file
        tasks.extract_audio_file = fake_extract
        try:
            task = tasks.submit_task(
                task_type="extract",
                target=src.name,
                resolved_path=str(src),
                params={"format": "mp3", "inputs": [str(src)]},
                cfg=cfg,
                enqueue=False,
            )
            result = tasks.run_task_now(task, cfg)
        finally:
            tasks.extract_audio_file = original

        assert result.status == "completed"
        assert calls == [src]
        assert tasks.load_task(task.id, cfg["tasks_dir"]).status == "completed"
        assert tasks._TASK_QUEUE.empty()


if __name__ == "__main__":
    for name in sorted(n for n in globals() if n.startswith("test_")):
        globals()[name]()
        print(f"PASS {name}")
    print("ALL TESTS PASSED")
