"""Tests for the /auex interactive multi-select registry.

These exercise the Telegram-independent state helpers in ``telegram_ui`` — no
python-telegram-bot import is needed.

Run directly: ``python3 tests/test_selection_ui.py`` (also pytest-compatible).
"""

import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import telegram_ui  # noqa: E402


def test_register_and_toggle():
    token = telegram_ui.register_pending(
        "123", "telegram:123", "ep", ["/a/1.mp4", "/a/2.mp4"], "mp3", False
    )
    entry = telegram_ui.get_pending(token)
    assert entry["files"] == ["/a/1.mp4", "/a/2.mp4"]
    assert entry["selected"] == set()

    telegram_ui.toggle_pending(token, 0)
    assert telegram_ui.get_pending(token)["selected"] == {0}
    telegram_ui.toggle_pending(token, 0)
    assert telegram_ui.get_pending(token)["selected"] == set()


def test_toggle_unknown_token_returns_none():
    assert telegram_ui.toggle_pending("missing", 0) is None


def test_toggle_out_of_range_is_ignored():
    token = telegram_ui.register_pending("1", "", "k", ["/a/1.mp4"], "mp3", False)
    telegram_ui.toggle_pending(token, 5)
    assert telegram_ui.get_pending(token)["selected"] == set()


def test_pop_removes_entry():
    token = telegram_ui.register_pending("1", "", "k", ["/a/1.mp4"], "mp3", True)
    assert telegram_ui.pop_pending(token) is not None
    assert telegram_ui.get_pending(token) is None


def test_expired_entries_are_pruned():
    token = telegram_ui.register_pending("1", "", "k", ["/a/1.mp4"], "mp3", False)
    telegram_ui._PENDING[token]["created_at"] = (
        time.time() - telegram_ui._SELECTION_TTL_SECONDS - 1
    )
    assert telegram_ui.get_pending(token) is None


def test_render_text_marks_selection():
    token = telegram_ui.register_pending(
        "1", "", "k", ["/a/one.mp4", "/a/two.mp4"], "flac", False
    )
    telegram_ui.toggle_pending(token, 1)
    text = telegram_ui.render_text(telegram_ui.get_pending(token))
    assert "已选 <b>1</b> 个" in text
    assert "⬜ 1." in text and "✅ 2." in text
    assert "one.mp4" in text


def test_no_bot_until_wired():
    assert telegram_ui.has_bot() is False


if __name__ == "__main__":
    for name in sorted(n for n in globals() if n.startswith("test_")):
        globals()[name]()
        print(f"PASS {name}")
    print("ALL TESTS PASSED")
