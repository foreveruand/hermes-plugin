"""Tests for subtitle/lyric sidecar detection and the audio-extraction scan filter.

A media file that already ships a subtitle/lyric sidecar needs no transcription, so
``search_files``/``resolve_target_files``/``search_candidates`` can drop it. Detection
mirrors ``scripts/scan_missing_subs.py``.

Run directly: ``python3 tests/test_sidecar_filter.py`` (also pytest-compatible).
"""

import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import core  # noqa: E402

EXTS = [".mp4", ".mkv", ".flac"]


def _cfg(**overrides):
    cfg = {"index_extensions": list(EXTS), "scan_dirs": [], "db_path": ""}
    cfg.update(overrides)
    return cfg


def _touch(path: Path) -> Path:
    path.write_text("x")
    return path


def test_has_subtitle_sidecar_matches_variants():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cases = {
            "exact.mp4": "exact.srt",
            "lang_dot.mp4": "lang_dot.chs.ass",
            "lang_dash.mkv": "lang_dash-zh-CN.vtt",
            "audio.flac": "audio.lrc",
            "audio_dup.flac": "audio_dup.flac.lrc",
            "enc - hevc-qsv.mkv": "enc.srt",
        }
        for media, sidecar in cases.items():
            _touch(root / media)
            _touch(root / sidecar)
        for media in cases:
            assert core.has_subtitle_sidecar(root / media), media


def test_has_subtitle_sidecar_ignores_unrelated_and_missing():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _touch(root / "naked.mp4")
        _touch(root / "target.mp4")
        _touch(root / "unrelated.srt")
        assert core.has_subtitle_sidecar(root / "naked.mp4") is False
        assert core.has_subtitle_sidecar(root / "target.mp4") is False


def test_resolve_directory_skips_files_with_sidecars():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _touch(root / "with_sub.mp4")
        _touch(root / "with_sub.srt")
        _touch(root / "no_sub.flac")

        kept = [p.name for p in core.resolve_target_files(str(root), _cfg(), skip_with_subtitles=True)]
        assert kept == ["no_sub.flac"]

        # Without the flag the directory scan is unchanged.
        all_files = [p.name for p in core.resolve_target_files(str(root), _cfg())]
        assert all_files == ["no_sub.flac", "with_sub.mp4"]


def test_explicit_file_target_is_always_honored():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        media = _touch(root / "with_sub.mp4")
        _touch(root / "with_sub.srt")
        res = core.resolve_target_files(str(media), _cfg(), skip_with_subtitles=True)
        assert [p.name for p in res] == ["with_sub.mp4"]


def test_search_files_skips_with_subtitles_walk():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _touch(root / "ep01.mp4")
        _touch(root / "ep01.srt")
        _touch(root / "ep02.mp4")
        cfg = _cfg(scan_dirs=[str(root)])

        filtered = core.search_files("ep", cfg, limit=10, skip_with_subtitles=True)
        assert [Path(p).name for p in filtered] == ["ep02.mp4"]

        unfiltered = core.search_files("ep", cfg, limit=10)
        assert sorted(Path(p).name for p in unfiltered) == ["ep01.mp4", "ep02.mp4"]


def test_search_candidates_defaults_to_skipping_subtitled_files():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _touch(root / "lib01.flac")
        _touch(root / "lib01.lrc")
        _touch(root / "lib02.flac")
        cfg = _cfg(scan_dirs=[str(root)])

        cand = core.search_candidates("lib", cfg, limit=10)
        assert [p.name for p in cand] == ["lib02.flac"]


if __name__ == "__main__":
    for name in sorted(n for n in globals() if n.startswith("test_")):
        globals()[name]()
        print(f"PASS {name}")
    print("ALL TESTS PASSED")
