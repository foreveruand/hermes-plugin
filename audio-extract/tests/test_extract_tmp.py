"""Tests for .tmp staging in audio extraction: ffmpeg writes to a ".tmp" path and the
final file only appears after a successful atomic rename.

Run directly: ``python3 tests/test_extract_tmp.py`` (also pytest-compatible).
"""

import sys
import tempfile
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import core  # noqa: E402


class _Result:
    def __init__(self, returncode: int):
        self.returncode = returncode
        self.stderr = "" if returncode == 0 else "boom"


def _fake_run(write_bytes: bytes, returncode: int = 0):
    """Return a subprocess.run stand-in that records argv and materializes the output."""

    def run(cmd, **kwargs):
        captured["cmd"] = cmd
        out = Path(cmd[-1])
        out.write_bytes(write_bytes)
        return _Result(returncode)

    captured = {}
    return run, captured


def test_extract_stages_tmp_then_renames():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "video.mkv"
        src.write_bytes(b"x")
        out_dir = root / "out"
        run, captured = _fake_run(b"audio")

        with mock.patch("core.subprocess.run", side_effect=run):
            out_file, _log = core.extract_audio_file(src, out_dir, None, format="mp3")

        assert Path(captured["cmd"][-1]).name == "video.mp3.tmp"
        assert "-f" in captured["cmd"] and "mp3" in captured["cmd"]
        assert out_file == out_dir / "video.mp3"
        assert out_file.read_bytes() == b"audio"
        assert not (out_dir / "video.mp3.tmp").exists()


def test_extract_failure_leaves_no_files():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "video.mkv"
        src.write_bytes(b"x")
        out_dir = root / "out"
        run, _ = _fake_run(b"partial", returncode=1)

        with mock.patch("core.subprocess.run", side_effect=run):
            try:
                core.extract_audio_file(src, out_dir, None)
            except RuntimeError:
                pass
            else:
                raise AssertionError("expected RuntimeError on ffmpeg failure")

        assert not (out_dir / "video.mp3").exists()
        assert not (out_dir / "video.mp3.tmp").exists()


if __name__ == "__main__":
    for name in sorted(n for n in globals() if n.startswith("test_")):
        globals()[name]()
        print(f"PASS {name}")
    print("ALL TESTS PASSED")
