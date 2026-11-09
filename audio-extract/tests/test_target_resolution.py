"""Tests for target resolution: file paths, non-recursive directory scan, keyword search.

Run directly: ``python3 tests/test_target_resolution.py`` (also pytest-compatible).
"""

import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import core  # noqa: E402


def _cfg(**overrides):
    cfg = {"index_extensions": [".mp4", ".mkv", ".flac"], "scan_dirs": [], "db_path": ""}
    cfg.update(overrides)
    return cfg


def _make_tree(root: Path) -> None:
    (root / "a.mp4").write_text("x")
    (root / "b.txt").write_text("x")
    (root / "c.flac").write_text("x")
    sub = root / "sub"
    sub.mkdir()
    (sub / "d.mkv").write_text("x")


def test_file_target_returns_itself():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_tree(root)
        res = core.resolve_target_files(str(root / "a.mp4"), _cfg())
        assert [p.name for p in res] == ["a.mp4"]


def test_directory_target_is_non_recursive():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_tree(root)
        res = core.resolve_target_files(str(root), _cfg())
        names = [p.name for p in res]
        # matching formats only, sorted, and sub/d.mkv excluded (no recursion)
        assert names == ["a.mp4", "c.flac"]


def test_keyword_fallback_uses_scan_dirs():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_tree(root)
        res = core.resolve_target_files("a.mp4", _cfg(scan_dirs=[str(root)]))
        assert len(res) == 1 and res[0].name == "a.mp4"


def test_missing_target_returns_empty():
    with tempfile.TemporaryDirectory() as td:
        assert core.resolve_target_files("definitely-not-here", _cfg()) == []


def test_search_candidates_returns_all_matches():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "ep01.mp4").write_text("x")
        (root / "ep02.mp4").write_text("x")
        (root / "notes.txt").write_text("x")
        cfg = _cfg(scan_dirs=[str(root)])
        cand = core.search_candidates("ep", cfg, limit=10)
        assert sorted(p.name for p in cand) == ["ep01.mp4", "ep02.mp4"]
        # resolve_target_files still collapses keyword search to a single best match.
        assert len(core.resolve_target_files("ep", cfg)) == 1


if __name__ == "__main__":
    for name in sorted(n for n in globals() if n.startswith("test_")):
        globals()[name]()
        print(f"PASS {name}")
    print("ALL TESTS PASSED")
