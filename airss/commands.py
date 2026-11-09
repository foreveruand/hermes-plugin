"""Slash commands for Hermes AIRSS plugin."""

from __future__ import annotations

import logging
from typing import Any, List

from .config import AIRSSConfig
from .database import Database
from .fetcher import RSSFetcher
from .models import RSSGroup, RSSSubscription, Subscriber
from .scheduler import RSSScheduler

logger = logging.getLogger("airss")


class RSSCommandHandlers:
    """Handlers for in-session slash commands."""

    def __init__(
        self,
        db: Database,
        scheduler: RSSScheduler,
        fetcher: RSSFetcher,
        config: AIRSSConfig,
    ):
        self.db = db
        self.scheduler = scheduler
        self.fetcher = fetcher
        self.config = config

    async def cmd_rss_list(self, raw_args: str) -> str:
        """List all subscriptions."""
        subs = await self.db.get_all_subscriptions()
        if not subs:
            return "📌 当前没有配置任何 RSS 订阅。可通过 `/rssadd <url> [名称]` 添加。"

        lines = [f"📋 **RSS 订阅列表 (共 {len(subs)} 个)**\n"]
        for s in subs:
            status = "🔴 已暂停" if s.stop else "🟢 运行中"
            err_str = f" [错误次数: {s.error_count}]" if s.error_count > 0 else ""
            lines.append(
                f"• `[ID {s.id}]` **{s.name}** ({status}{err_str})\n"
                f"  地址: {s.url}\n"
                f"  抓取间隔: {s.interval} 分钟 | 所属分组: {s.source_group_id}\n"
            )
        return "\n".join(lines)

    async def cmd_rss_add(self, raw_args: str) -> str:
        """Add a new RSS subscription: /rssadd <url> [name]."""
        parts = raw_args.strip().split(maxsplit=1)
        if not parts:
            return "❌ 用法: `/rssadd <url> [名称]`\n例如: `/rssadd https://example.com/feed.xml 科技早报`"

        url = parts[0]
        name = parts[1] if len(parts) > 1 else ""

        existing = await self.db.get_subscription_by_url(url)
        if existing:
            return f"⚠️ 该订阅源已存在：`[ID {existing.id}]` {existing.name}"

        # Probe feed for title if name is omitted
        if not name:
            res = await self.fetcher.fetch_feed(url)
            name = res.feed_title or "未命名订阅"

        sub = RSSSubscription(
            name=name,
            url=url,
            interval=int(self.config.fetch_config.get("default_interval", 5)),
            source_group_id=1,
        )
        new_id = await self.db.add_subscription(sub)
        return f"✅ 成功添加订阅 `[ID {new_id}]` **{name}**\n地址: {url}"

    async def cmd_rss_del(self, raw_args: str) -> str:
        """Delete a subscription: /rssdel <id>."""
        arg = raw_args.strip()
        if not arg or not arg.isdigit():
            return "❌ 用法: `/rssdel <ID>`\n例如: `/rssdel 1`"

        sub_id = int(arg)
        sub = await self.db.get_subscription(sub_id)
        if not sub:
            return f"⚠️ 未找到 ID 为 {sub_id} 的订阅"

        await self.db.delete_subscription(sub_id)
        return f"🗑️ 已成功删除订阅 `[ID {sub_id}]` **{sub.name}**"

    async def cmd_rss_update(self, raw_args: str) -> str:
        """Trigger immediate fetch: /rssupdate [id]."""
        arg = raw_args.strip()
        if arg:
            if not arg.isdigit():
                return "❌ 用法: `/rssupdate [ID]`"
            sub_id = int(arg)
            sub = await self.db.get_subscription(sub_id)
            if not sub:
                return f"⚠️ 未找到 ID 为 {sub_id} 的订阅"
            new_count = await self.scheduler.fetch_subscription(sub)
            return f"🔄 抓取完成：`[ID {sub_id}]` **{sub.name}**，发现 {new_count} 篇新文章。"

        subs = await self.db.get_all_subscriptions()
        total_new = 0
        for s in subs:
            if not s.stop:
                count = await self.scheduler.fetch_subscription(s)
                total_new += count
        return f"🔄 全部活跃订阅源抓取完成，共发现 {total_new} 篇新文章。"

    async def cmd_rss_group(self, raw_args: str) -> str:
        """Manage groups: /rssgroup [list|add|del] [args]."""
        parts = raw_args.strip().split(maxsplit=2)
        action = parts[0].lower() if parts else "list"

        if action == "list":
            groups = await self.db.get_all_groups()
            lines = [f"📁 **RSS 分组列表 (共 {len(groups)} 个)**\n"]
            for g in groups:
                sched = ", ".join(g.schedules) if g.schedules else "无推送时间"
                prompt_info = " [自定义Prompt]" if g.persona_prompt else ""
                lines.append(f"• `[ID {g.id}]` **{g.name}** ({sched}){prompt_info}")
            return "\n".join(lines)

        if action == "add":
            if len(parts) < 2:
                return "❌ 用法: `/rssgroup add <分组名称> [推送时间HH:MM]`"
            name = parts[1]
            schedules = [parts[2]] if len(parts) > 2 else []
            new_group = RSSGroup(name=name, schedules=schedules)
            new_id = await self.db.add_group(new_group)
            sched_str = f"（推送时间: {schedules[0]}）" if schedules else ""
            return f"✅ 已添加分组 `[ID {new_id}]` **{name}** {sched_str}"

        if action == "del":
            if len(parts) < 2 or not parts[1].isdigit():
                return "❌ 用法: `/rssgroup del <分组ID>`"
            gid = int(parts[1])
            if gid == 1:
                return "❌ 不能删除默认分组 (ID 1)"
            await self.db.delete_group(gid)
            return f"🗑️ 已成功删除分组 `[ID {gid}]`"

        return "❌ 支持的子命令: `/rssgroup list`, `/rssgroup add <名称> [时间]`, `/rssgroup del <ID>`"

    async def cmd_rss_sub(self, raw_args: str) -> str:
        """Add subscriber: /rsssub <subscription_id> <umo>."""
        parts = raw_args.strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit():
            return "❌ 用法: `/rsssub <订阅ID> <接收端UMO或Webhook>`\n例如: `/rsssub 1 telegram:12345678`"

        sub_id = int(parts[0])
        umo = parts[1]

        sub = await self.db.get_subscription(sub_id)
        if not sub:
            return f"⚠️ 未找到 ID 为 {sub_id} 的订阅"

        existing = await self.db.get_subscriber(sub_id, umo)
        if existing:
            return f"⚠️ 接收端 `{umo}` 已经订阅了 `[ID {sub_id}]` **{sub.name}**"

        subscriber = Subscriber(subscription_id=sub_id, umo=umo)
        await self.db.add_subscriber(subscriber)
        return f"✅ 成功将 `{umo}` 添加为 `[ID {sub_id}]` **{sub.name}** 的订阅接收端"


def register_all_commands(
    ctx: Any,
    db: Database,
    scheduler: RSSScheduler,
    fetcher: RSSFetcher,
    config: AIRSSConfig,
) -> None:
    """Register slash commands into Hermes Agent."""
    handlers = RSSCommandHandlers(db, scheduler, fetcher, config)

    commands = [
        ("rsslist", handlers.cmd_rss_list, "列出所有 RSS 订阅源", ""),
        ("rssadd", handlers.cmd_rss_add, "添加新的 RSS 订阅", "<url> [名称]"),
        ("rssdel", handlers.cmd_rss_del, "删除指定的 RSS 订阅", "<id>"),
        ("rssupdate", handlers.cmd_rss_update, "立即更新抓取订阅源", "[id]"),
        ("rssgroup", handlers.cmd_rss_group, "管理 RSS 分组与摘要计划", "[list|add|del]"),
        ("rsssub", handlers.cmd_rss_sub, "绑定订阅接收端", "<订阅ID> <umo>"),
    ]

    for name, handler, desc, hint in commands:
        if hasattr(ctx, "register_command"):
            ctx.register_command(
                name=name,
                handler=handler,
                description=desc,
                args_hint=hint,
            )
            logger.info(f"Registered slash command /{name}")
