"""Hermes AIRSS Plugin Entrypoint."""

from __future__ import annotations

import asyncio
import json
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


def _profile_dir() -> Path:
    """当前插件所属 profile：<profile>/plugins/airss/__init__.py。

    gateway 多 profile 进程里的 HERMES_HOME 可能指向 Hermes 根目录，
    所以优先按安装位置推断，失败再回退环境变量与 ~/.hermes。
    """
    plugin_dir = Path(__file__).absolute().parent
    if plugin_dir.parent.name == "plugins":
        return plugin_dir.parent.parent
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home)
    return Path.home() / ".hermes"


def _is_gateway_process(profile_dir: Path) -> bool:
    """Return True when this process is the profile gateway or its plugin host child.

    面板/CLI 也会加载插件，但它们只应读写 RSS 数据和配置；抓取与定时总结
    必须由 gateway 这一条常驻业务进程执行。gateway.lock 记录当前 profile
    的 gateway pid，插件宿主子进程的父进程也是该 pid。
    """
    try:
        record = json.loads((profile_dir / "gateway.lock").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    pid = record.get("pid")
    if record.get("kind") != "hermes-gateway" or not isinstance(pid, int) or pid <= 0:
        return False
    return pid == os.getpid() or pid == os.getppid()


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

    data_dir = _profile_dir() / "data" / "airss"
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

    # 只有 gateway（或其插件宿主子进程）运行抓取/定时总结；面板和一次性 CLI
    # 进程只提供工具与 dashboard 路由。
    if _is_gateway_process(_profile_dir()):
        scheduler.start()
        if hasattr(ctx, "on_unload"):
            ctx.on_unload(lambda: scheduler.stop())
    else:
        logger.info(
            "AIRSS scheduler not started in pid=%d: only the profile gateway runs fetches and digests",
            os.getpid(),
        )

    logger.info(
        "Hermes AIRSS plugin registered successfully (pid=%d, profile=%s)",
        os.getpid(),
        _profile_dir(),
    )
