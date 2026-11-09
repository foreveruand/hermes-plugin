"""Telegram inline-button UI for interactive multi-select audio extraction (/auex).

When a keyword search matches several files the ``/auex`` command can no longer
guess a single target. It registers a *pending selection* here and sends an
inline keyboard; the user toggles files and taps "开始提取" to submit.

Selection state lives in-process (the default ``in_process`` plugin isolation),
which is where both the command handler and the callback handler run.
"""

from __future__ import annotations

import html
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

try:
    from .tasks import submit_task
except ImportError:
    from tasks import submit_task

logger = logging.getLogger("audio-extract")

CALLBACK_PREFIX = "auex:"
_SELECTION_TTL_SECONDS = 1800
_MAX_LABEL_LEN = 32

# token -> selection entry; both the command handler and the callback handler run
# on the same event loop, but the lock keeps the registry safe regardless.
_PENDING: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
_BOT: Any = None
_CFG: dict[str, Any] = {}


def has_bot() -> bool:
    """True once a Telegram bot handle has been captured (i.e. the UI is usable)."""
    return _BOT is not None


def _prune_locked(now: float) -> None:
    expired = [t for t, e in _PENDING.items() if now - e["created_at"] > _SELECTION_TTL_SECONDS]
    for t in expired:
        _PENDING.pop(t, None)


def register_pending(
    chat_id: str,
    delivery_target: str,
    keyword: str,
    candidates: list[Any],
    fmt: str,
    no_job: bool,
) -> str:
    """Store a fresh candidate list and return its short selection token."""
    token = uuid.uuid4().hex[:8]
    now = time.time()
    with _LOCK:
        _prune_locked(now)
        _PENDING[token] = {
            "chat_id": str(chat_id),
            "delivery_target": delivery_target,
            "keyword": keyword,
            "files": [str(p) for p in candidates],
            "selected": set(),
            "fmt": fmt,
            "no_job": no_job,
            "created_at": now,
        }
    return token


def get_pending(token: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        _prune_locked(time.time())
        return _PENDING.get(token)


def toggle_pending(token: str, idx: int) -> Optional[dict[str, Any]]:
    """Flip one candidate's selection; returns the entry (or None when expired)."""
    with _LOCK:
        _prune_locked(time.time())
        entry = _PENDING.get(token)
        if entry is None:
            return None
        if 0 <= idx < len(entry["files"]):
            if idx in entry["selected"]:
                entry["selected"].discard(idx)
            else:
                entry["selected"].add(idx)
        return entry


def pop_pending(token: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        return _PENDING.pop(token, None)


def _short_name(name: str) -> str:
    return name if len(name) <= _MAX_LABEL_LEN else name[: _MAX_LABEL_LEN - 1] + "…"


def render_text(entry: dict[str, Any]) -> str:
    """Render the selection prompt as Telegram-allowed HTML."""
    total = len(entry["files"])
    selected = len(entry["selected"])
    lines = [
        f"🎵 <b>找到 {total} 个匹配文件</b>",
        f"请选择要提取音频的文件（可多选，已选 <b>{selected}</b> 个）：",
        "",
    ]
    for i, f in enumerate(entry["files"]):
        mark = "✅" if i in entry["selected"] else "⬜"
        lines.append(f"{mark} {i + 1}. <code>{html.escape(Path(f).name)}</code>")
    lines.append("")
    lines.append(
        f"格式: <code>{html.escape(entry['fmt'])} (16kHz 单声道)</code> · "
        f"注册任务: <code>{'否' if entry['no_job'] else '是 (.jobs)'}</code>"
    )
    return "\n".join(lines)


def build_keyboard(token: str, entry: dict[str, Any]) -> Any:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    for i, f in enumerate(entry["files"]):
        mark = "✅" if i in entry["selected"] else "⬜"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark} {i + 1}. {_short_name(Path(f).name)}",
                    callback_data=f"{CALLBACK_PREFIX}t:{token}:{i}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("🚀 开始提取", callback_data=f"{CALLBACK_PREFIX}c:{token}"),
            InlineKeyboardButton("✖ 取消", callback_data=f"{CALLBACK_PREFIX}x:{token}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def send_selection_prompt(chat_id: str, token: str, entry: dict[str, Any]) -> bool:
    """Send the keyboard via the captured bot; False when no bot is available."""
    if _BOT is None:
        return False
    try:
        await _BOT.send_message(
            chat_id=chat_id,
            text=render_text(entry),
            parse_mode="HTML",
            reply_markup=build_keyboard(token, entry),
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:
        logger.error("Failed to send /auex selection prompt: %s", exc)
        return False


def _format_submission(task: Any, entry: dict[str, Any], count: int) -> str:
    return (
        f"⏳ <b>音频提取任务已提交</b>\n\n"
        f"• 任务 ID: <code>{html.escape(task.id)}</code>\n"
        f"• 文件数: <code>{count}</code>\n"
        f"• 格式: <code>{html.escape(entry['fmt'])} (16kHz 单声道)</code>\n\n"
        f"<i>完成后将自动发送通知，可用 /vclipstatus 查询状态。</i>"
    )


def wire_telegram_handlers(native_app: Any, adapter: Any, cfg: dict[str, Any]) -> None:
    """Capture the bot handle and wire the ``auex:`` callback handler."""
    global _BOT, _CFG

    if not native_app or not hasattr(native_app, "add_handler"):
        return

    try:
        from telegram import ParseMode
        from telegram.ext import CallbackQueryHandler
    except ImportError:
        logger.debug("python-telegram-bot not installed; skipping /auex selection UI")
        return

    _BOT = getattr(native_app, "bot", None)
    _CFG = cfg

    async def _callback_handler(update: Any, context: Any) -> None:
        query = update.callback_query
        if not query or not query.data or not query.data.startswith(CALLBACK_PREFIX):
            return

        parts = query.data[len(CALLBACK_PREFIX):].split(":")
        action = parts[0] if parts else ""
        token = parts[1] if len(parts) > 1 else ""

        if action == "t" and len(parts) == 3 and parts[2].isdigit():
            entry = get_pending(token)
            if entry is None:
                await query.answer("⚠️ 该选择已过期，请重新发送 /auex")
                return
            if not _same_chat(query, entry):
                await query.answer("⚠️ 无权操作")
                return
            entry = toggle_pending(token, int(parts[2]))
            await query.answer()
            try:
                await query.edit_message_text(
                    render_text(entry),
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_keyboard(token, entry),
                )
            except Exception as exc:
                logger.debug("edit selection message failed: %s", exc)
            return

        if action == "c":
            entry = get_pending(token)
            if entry is None:
                await query.answer("⚠️ 该选择已过期或已处理")
                return
            if not _same_chat(query, entry):
                await query.answer("⚠️ 无权操作")
                return
            selected = sorted(entry["selected"])
            if not selected:
                await query.answer("请先选择至少一个文件", show_alert=True)
                return
            pop_pending(token)
            await query.answer("已提交")
            selected_files = [entry["files"][i] for i in selected]
            try:
                task = submit_task(
                    task_type="extract",
                    target=entry["keyword"],
                    resolved_path=entry["keyword"],
                    params={
                        "format": entry["fmt"],
                        "no_job": entry["no_job"],
                        "batch": True,
                        "inputs": selected_files,
                    },
                    delivery_target=entry["delivery_target"] or None,
                    cfg=_CFG,
                )
                await query.edit_message_text(
                    _format_submission(task, entry, len(selected_files)),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as exc:
                logger.error("Failed to submit /auex selection task: %s", exc)
                await query.edit_message_text(
                    f"❌ 提交失败: {html.escape(str(exc))}", parse_mode=ParseMode.HTML
                )
            return

        if action == "x":
            entry = get_pending(token)
            if entry is not None and not _same_chat(query, entry):
                await query.answer("⚠️ 无权操作")
                return
            pop_pending(token)
            await query.answer("已取消")
            try:
                await query.edit_message_text("✖ 已取消本次音频提取选择。")
            except Exception as exc:
                logger.debug("edit cancel message failed: %s", exc)
            return

    native_app.add_handler(CallbackQueryHandler(_callback_handler, pattern=r"^auex:"))
    logger.info("Wired Telegram CallbackQueryHandler for /auex selection UI")


def _same_chat(query: Any, entry: dict[str, Any]) -> bool:
    """Guard against a button tap from a different chat than the requester."""
    message = getattr(query, "message", None)
    chat = getattr(message, "chat_id", None)
    return chat is not None and str(chat) == entry["chat_id"]
