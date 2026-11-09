"""Hermes Audio Extract & Video Clip Plugin.

Lossless video clipping, Whisper audio extraction, and subtitle synchronization
with robust background task execution and out-of-band delivery notifications.
"""

from __future__ import annotations

import logging
from typing import Any

try:
    from .commands import AudioExtractCommands
    from .config import load_config
    from .tasks import start_worker, stop_worker
    from .telegram_ui import wire_telegram_handlers
    from .tools import register_all_tools
except ImportError:
    from commands import AudioExtractCommands
    from config import load_config
    from tasks import start_worker, stop_worker
    from telegram_ui import wire_telegram_handlers
    from tools import register_all_tools

logger = logging.getLogger("audio-extract")


def register(ctx: Any) -> None:
    """Plugin registration hook invoked by Hermes plugin manager."""
    cfg = load_config(ctx)

    # 1. Register LLM Agent tools
    register_all_tools(ctx, cfg)

    # 2. Register Slash Commands
    commands = AudioExtractCommands(cfg)
    if hasattr(ctx, "register_command"):
        ctx.register_command(
            name="vclip",
            handler=commands.handle_vclip,
            description="视频片段剪辑 (无损流复制/重编码，后台异步长任务)",
            args_hint="<目标> <开始> <结束>",
        )
        ctx.register_command(
            name="auex",
            handler=commands.handle_auex,
            description="提取 16kHz 单声道音频用于 Whisper 转写",
            args_hint="<关键词|文件|目录>",
        )
        ctx.register_command(
            name="subsync",
            handler=commands.handle_subsync,
            description="扫描并同步字幕回原媒体文件目录",
            args_hint="[once]",
        )
        ctx.register_command(
            name="audiosearch",
            handler=commands.handle_audiosearch,
            description="搜索媒体库文件",
            args_hint="<关键词>",
        )
        ctx.register_command(
            name="audiojobs",
            handler=commands.handle_audiojobs,
            description="查看待处理音频转写任务",
            args_hint="",
        )
        ctx.register_command(
            name="audiotasks",
            handler=commands.handle_audiotasks,
            description="查看最近后台剪辑与提取任务",
            args_hint="",
        )
        ctx.register_command(
            name="vclipstatus",
            handler=commands.handle_vclipstatus,
            description="查看指定任务详细状态与输出",
            args_hint="<任务ID>",
        )
        logger.info("Registered audio-extract slash commands: /vclip, /auex, /subsync, /audiosearch, /audiojobs, /audiotasks, /vclipstatus")

    # 3. Register Telegram inline-keyboard UI for /auex multi-select
    if hasattr(ctx, "register_telegram_handler"):
        ctx.register_telegram_handler(
            lambda native, adapter: wire_telegram_handlers(native, adapter, cfg)
        )
    elif hasattr(ctx, "register_platform_handler"):
        ctx.register_platform_handler(
            "telegram",
            lambda native, adapter: wire_telegram_handlers(native, adapter, cfg),
        )

    # 4. Start background worker thread
    try:
        start_worker(cfg)
    except Exception as exc:
        logger.error("Failed to start audio-extract background worker: %s", exc)

    # 5. Lifecycle teardown
    if hasattr(ctx, "on_unload"):
        ctx.on_unload(stop_worker)

    logger.info("Hermes Audio Extract plugin loaded successfully.")
