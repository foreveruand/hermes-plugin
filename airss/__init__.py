"""Hermes AIRSS Plugin Entrypoint."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .commands import register_all_commands
from .config import AIRSSConfig
from .database import Database
from .delivery import MessageDelivery
from .fetcher import RSSFetcher
from .filter import AIFilterService
from .scheduler import RSSScheduler
from .telegram_ui import wire_telegram_handlers
from .tools import register_all_tools

logger = logging.getLogger("airss")


def register(ctx) -> None:
    """Plugin registration hook invoked by Hermes plugin manager."""
    cfg_data = {}
    if hasattr(ctx, "get_config"):
        for section in [
            "ai_config",
            "fetch_config",
            "rsshub_config",
            "proxy_config",
            "storage_config",
            "delivery_config",
        ]:
            val = ctx.get_config(section, None)
            if val is not None:
                cfg_data[section] = val
    config = AIRSSConfig.from_dict(cfg_data)

    hermes_home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    data_dir = hermes_home / "data" / "airss"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "rss.db"

    db = Database(db_path)
    fetcher = RSSFetcher(
        proxy=config.proxy_config.get("proxy"),
        proxy_enabled=config.proxy_config.get("enable_proxy", False),
        timeout=int(config.fetch_config.get("request_timeout", 30)),
        rsshub_url=config.rsshub_config.get("rsshub_url"),
        rsshub_key=config.rsshub_config.get("rsshub_key"),
    )
    delivery = MessageDelivery(
        default_webhook=config.delivery_config.get("default_webhook", ""),
        telegram_bot_token=config.delivery_config.get("telegram_bot_token", ""),
    )
    filter_service = AIFilterService(db, config)
    scheduler = RSSScheduler(
        ctx=ctx,
        db=db,
        fetcher=fetcher,
        config=config,
        delivery=delivery,
        filter_service=filter_service,
    )

    # Register Agent tools
    register_all_tools(ctx, db, scheduler, fetcher, config)

    # Register Slash commands
    register_all_commands(ctx, db, scheduler, fetcher, config)

    # Register Telegram platform handlers
    if hasattr(ctx, "register_telegram_handler"):
        ctx.register_telegram_handler(
            lambda native, adapter: wire_telegram_handlers(
                native, adapter, db, scheduler
            )
        )
    elif hasattr(ctx, "register_platform_handler"):
        ctx.register_platform_handler(
            "telegram",
            lambda native, adapter: wire_telegram_handlers(
                native, adapter, db, scheduler
            ),
        )

    # Start background scheduler worker thread
    scheduler.start()

    if hasattr(ctx, "on_unload"):
        ctx.on_unload(lambda: scheduler.stop())

    logger.info("Hermes AIRSS plugin registered successfully")
