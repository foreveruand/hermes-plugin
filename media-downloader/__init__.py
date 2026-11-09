"""Hermes Media Downloader Plugin.

Unified yt-dlp / gallery-dl downloader for videos, audio and images across Bilibili,
YouTube, X/Twitter and Pixiv, plus an ASMR.ONE pipeline for DLSite work numbers that
tags downloads with dvtag before archiving. Runs downloads as background tasks and
delivers completion briefings out of band.
"""

from __future__ import annotations

import logging
from typing import Any

try:
    from .commands import MediaDownloaderCommands
    from .config import load_config
    from .tasks import start_worker, stop_worker
    from .tools import register_all_tools
except ImportError:
    from commands import MediaDownloaderCommands
    from config import load_config
    from tasks import start_worker, stop_worker
    from tools import register_all_tools

logger = logging.getLogger("media-downloader")


def register(ctx: Any) -> None:
    """Plugin registration hook invoked by Hermes plugin manager."""
    cfg = load_config(ctx)

    # 1. Register LLM Agent tools
    register_all_tools(ctx, cfg)

    # 2. Register Slash Commands
    commands = MediaDownloaderCommands(cfg)
    if hasattr(ctx, "register_command"):
        ctx.register_command(
            name="dl",
            handler=commands.handle_dl,
            description="下载媒体到归档目录 (后台异步任务，完成后推送简报)",
            args_hint="<URL> [--type auto|video|image|asmr] [--author 名称]",
        )
        ctx.register_command(
            name="asmr",
            handler=commands.handle_asmr,
            description="按 RJ 号下载 ASMR.ONE 作品 (dvtag 处理后入库，后台异步任务)",
            args_hint="<RJ号> [--out 路径] [--proxy URL]",
        )
        ctx.register_command(
            name="dlstatus",
            handler=commands.handle_dlstatus,
            description="查看指定下载任务的状态与归档结果",
            args_hint="<任务ID>",
        )
        ctx.register_command(
            name="dltasks",
            handler=commands.handle_dltasks,
            description="查看最近的媒体下载任务",
            args_hint="",
        )
        logger.info("Registered media-downloader slash commands: /dl, /asmr, /dlstatus, /dltasks")

    # 3. Start background worker thread
    try:
        start_worker(cfg)
    except Exception as exc:
        logger.error("Failed to start media-downloader background worker: %s", exc)

    # 4. Lifecycle teardown
    if hasattr(ctx, "on_unload"):
        ctx.on_unload(stop_worker)

    logger.info("Hermes Media Downloader plugin loaded successfully.")
