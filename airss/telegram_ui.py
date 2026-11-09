"""Telegram inline button UI handlers for AIRSS."""

from __future__ import annotations

import html
import logging
from typing import Any

from .database import Database
from .models import CONFIG_NUMBER_MAP
from .scheduler import RSSScheduler

logger = logging.getLogger("airss")


def _esc(value: Any) -> str:
    """Escape untrusted text for Telegram's HTML parse mode."""
    return html.escape(str(value), quote=False)


def _sub_detail_text(sub: Any) -> str:
    """Render the subscription detail card as Telegram-allowed HTML."""
    status_emoji = "🟢 正常抓取" if not sub.stop else "🔴 已暂停"
    return (
        f"📡 <b>{_esc(sub.name)}</b>\n\n"
        f"• 状态: {status_emoji}\n"
        f"• 地址: {_esc(sub.url)}\n"
        f"• 抓取周期: {_esc(sub.interval)} 分钟\n"
        f"• 错误计数: {_esc(sub.error_count)}\n"
    )


def wire_telegram_handlers(
    native_app: Any,
    adapter: Any,
    db: Database,
    scheduler: RSSScheduler,
) -> None:
    """Wire PTB callback query handler for airss inline keyboards."""
    if not native_app or not hasattr(native_app, "add_handler"):
        return

    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
        from telegram.ext import CallbackQueryHandler, ContextTypes

        async def _callback_handler(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> None:
            query = update.callback_query
            if not query or not query.data or not query.data.startswith("airss:"):
                return

            await query.answer()
            data = query.data.split(":")
            action = data[1] if len(data) > 1 else ""

            if action == "list":
                subs = await db.get_all_subscriptions()
                buttons = [
                    [
                        InlineKeyboardButton(
                            f"{'🟢' if not s.stop else '🔴'} {s.name}",
                            callback_data=f"airss:sub:{s.id}",
                        )
                    ]
                    for s in subs
                ]
                if not buttons:
                    await query.edit_message_text("📋 暂无订阅源，使用 `/rssadd <url>` 添加。")
                    return
                reply_markup = InlineKeyboardMarkup(buttons)
                await query.edit_message_text(
                    "<b>📋 RSS 订阅列表</b>\n点击查看与配置具体订阅：",
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )

            elif action == "sub":
                sub_id = int(data[2])
                sub = await db.get_subscription(sub_id)
                if not sub:
                    await query.edit_message_text("⚠️ 订阅不存在")
                    return

                text = _sub_detail_text(sub)
                toggle_btn_text = "▶️ 恢复抓取" if sub.stop else "⏸️ 暂停抓取"
                buttons = [
                    [
                        InlineKeyboardButton(
                            toggle_btn_text,
                            callback_data=f"airss:toggle:{sub.id}:stop",
                        ),
                        InlineKeyboardButton(
                            "🔄 立即抓取", callback_data=f"airss:fetch:{sub.id}"
                        ),
                    ],
                    [InlineKeyboardButton("🔙 返回列表", callback_data="airss:list")],
                ]
                await query.edit_message_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(buttons),
                )

            elif action == "toggle":
                sub_id = int(data[2])
                field = data[3]
                sub = await db.get_subscription(sub_id)
                if sub and field == "stop":
                    sub.stop = not sub.stop
                    await db.update_subscription(sub)
                    text = _sub_detail_text(sub)
                    toggle_btn_text = "▶️ 恢复抓取" if sub.stop else "⏸️ 暂停抓取"
                    buttons = [
                        [
                            InlineKeyboardButton(
                                toggle_btn_text,
                                callback_data=f"airss:toggle:{sub.id}:stop",
                            ),
                            InlineKeyboardButton(
                                "🔄 立即抓取", callback_data=f"airss:fetch:{sub.id}"
                            ),
                        ],
                        [InlineKeyboardButton("🔙 返回列表", callback_data="airss:list")],
                    ]
                    await query.edit_message_text(
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(buttons),
                    )

            elif action == "fetch":
                sub_id = int(data[2])
                sub = await db.get_subscription(sub_id)
                if sub:
                    new_count = await scheduler.fetch_subscription(sub)
                    await query.edit_message_text(
                        f"✅ 已触发抓取 <code>ID {_esc(sub.id)}</code> "
                        f"<b>{_esc(sub.name)}</b>，发现 {_esc(new_count)} 篇新文章。",
                        parse_mode=ParseMode.HTML,
                    )

        native_app.add_handler(
            CallbackQueryHandler(_callback_handler, pattern=r"^airss:")
        )
        logger.info("Wired Telegram CallbackQueryHandler for airss UI")

    except ImportError:
        logger.debug("python-telegram-bot not installed or imported, skipping telegram UI wiring")
    except Exception as exc:
        logger.warning("Failed to wire telegram UI: %s", exc)
