"""Agent tools for Hermes AIRSS plugin."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from .config import AIRSSConfig
from .database import Database
from .fetcher import RSSFetcher
from .models import GLOBAL_CONFIGURABLE_FIELDS, RSSGroup, RSSSubscription, Subscriber
from .scheduler import RSSScheduler

logger = logging.getLogger("airss")


def _schema(name: str, description: str, properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        params["required"] = required
    return {
        "name": name,
        "description": description,
        "parameters": params,
    }


# ==================== Tool Schemas ====================

RSS_SUBSCRIPTION_SCHEMA = _schema(
    "rss_subscription",
    "Manage RSS subscriptions in Hermes AIRSS: add, list, get, update, delete, or set_config.",
    {
        "action": {
            "type": "string",
            "enum": ["add", "list", "get", "update", "delete", "set_config"],
            "description": "Action to perform on subscriptions.",
        },
        "subscription_id": {
            "type": "integer",
            "description": "ID of the subscription (required for get, update, delete, set_config).",
        },
        "url": {"type": "string", "description": "RSS feed URL (required for add)."},
        "name": {"type": "string", "description": "Display name of the subscription."},
        "interval": {"type": "integer", "description": "Fetch interval in minutes (default 5)."},
        "source_group_id": {"type": "integer", "description": "Group ID for this subscription (default 1)."},
        "key": {
            "type": "string",
            "description": f"Field name to update for set_config: {', '.join(GLOBAL_CONFIGURABLE_FIELDS)}",
        },
        "value": {
            "type": "string",
            "description": "New value for set_config (booleans/integers will be parsed automatically).",
        },
    },
    required=["action"],
)

RSS_GROUP_SCHEMA = _schema(
    "rss_group",
    "Manage RSS groups, schedules, and persona prompts for AI digests.",
    {
        "action": {
            "type": "string",
            "enum": ["add", "list", "get", "update", "delete"],
            "description": "Action to perform on groups.",
        },
        "group_id": {"type": "integer", "description": "ID of the group (required for get, update, delete)."},
        "name": {"type": "string", "description": "Group name."},
        "schedules": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Push schedules (e.g. ['09:00', '18:00'] or 5-part cron expressions).",
        },
        "persona_prompt": {
            "type": "string",
            "description": "Custom system prompt for AI digests generated for this group.",
        },
    },
    required=["action"],
)

RSS_FETCH_SCHEMA = _schema(
    "rss_fetch",
    "Trigger immediate fetching of RSS subscriptions or generate a signed RSSHub URL.",
    {
        "subscription_id": {
            "type": "integer",
            "description": "Optional subscription ID to fetch. If omitted, all active subscriptions are checked.",
        },
        "rsshub_path": {
            "type": "string",
            "description": "Optional RSSHub route path (e.g. '/bilibili/user/video/12345') to build signed URL.",
        },
    },
)

RSS_ARTICLES_SCHEMA = _schema(
    "rss_articles",
    "Query recent or unsent articles from the AIRSS database.",
    {
        "subscription_id": {
            "type": "integer",
            "description": "Optional subscription ID filter.",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of articles to return (default 20).",
        },
        "unsent_only": {
            "type": "boolean",
            "description": "If true, only returns articles not yet marked sent.",
        },
    },
)

RSS_DIGEST_SCHEMA = _schema(
    "rss_digest",
    "Generate or send an AI summary digest for a specific RSS group.",
    {
        "group_id": {
            "type": "integer",
            "description": "ID of the group (default 1).",
        },
        "send": {
            "type": "boolean",
            "description": "If true, distributes the digest to all subscribers of this group. Default false (preview only).",
        },
    },
)

RSS_SUBSCRIBER_SCHEMA = _schema(
    "rss_subscriber",
    "Manage subscribers and personal delivery configurations for RSS feeds.",
    {
        "action": {
            "type": "string",
            "enum": ["add", "list", "get", "update", "delete"],
            "description": "Action to perform on subscribers.",
        },
        "subscription_id": {
            "type": "integer",
            "description": "ID of the subscription.",
        },
        "umo": {
            "type": "string",
            "description": "Subscriber destination UMO (e.g. 'telegram:12345678' or webhook URL).",
        },
        "config": {
            "type": "object",
            "description": "Personal config options: only_title, only_pic, enable_spoiler, stop, black_keyword, white_keyword, ai_filter_enabled.",
        },
    },
    required=["action", "subscription_id"],
)


# ==================== Tool Handlers ====================

class AirssToolHandlers:
    """Tool handlers bound to database, scheduler, fetcher and config."""

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

    async def handle_rss_subscription(self, args: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
        kwargs = {**(args or {}), **kwargs}
        action = kwargs.get("action")
        sub_id = kwargs.get("subscription_id")

        if action == "list":
            subs = await self.db.get_all_subscriptions()
            return {
                "success": True,
                "count": len(subs),
                "subscriptions": [
                    {
                        "id": s.id,
                        "name": s.name,
                        "url": s.url,
                        "interval": s.interval,
                        "group_id": s.source_group_id,
                        "stop": s.stop,
                        "error_count": s.error_count,
                        "last_fetch": s.last_fetch.isoformat() if s.last_fetch else None,
                    }
                    for s in subs
                ],
            }

        if action == "get":
            if not sub_id:
                return {"success": False, "error": "subscription_id is required for get"}
            sub = await self.db.get_subscription(sub_id)
            if not sub:
                return {"success": False, "error": f"Subscription {sub_id} not found"}
            return {
                "success": True,
                "subscription": {
                    "id": sub.id,
                    "name": sub.name,
                    "url": sub.url,
                    "interval": sub.interval,
                    "group_id": sub.source_group_id,
                    "stop": sub.stop,
                    "black_keyword": sub.black_keyword,
                    "ai_summary_enabled": sub.ai_summary_enabled,
                    "enable_proxy": sub.enable_proxy,
                },
            }

        if action == "add":
            url = kwargs.get("url")
            if not url:
                return {"success": False, "error": "url is required for add"}
            existing = await self.db.get_subscription_by_url(url)
            if existing:
                return {"success": False, "error": f"Subscription with URL already exists (id={existing.id})"}

            name = kwargs.get("name") or "New Subscription"
            interval = kwargs.get("interval", 5)
            group_id = kwargs.get("source_group_id", 1)

            sub = RSSSubscription(
                name=name,
                url=url,
                interval=interval,
                source_group_id=group_id,
            )
            new_id = await self.db.add_subscription(sub)
            return {"success": True, "subscription_id": new_id, "name": name, "url": url}

        if action == "delete":
            if not sub_id:
                return {"success": False, "error": "subscription_id is required for delete"}
            await self.db.delete_subscription(sub_id)
            return {"success": True, "message": f"Subscription {sub_id} deleted"}

        if action == "set_config":
            if not sub_id:
                return {"success": False, "error": "subscription_id is required for set_config"}
            key = kwargs.get("key")
            value = kwargs.get("value")
            if not key:
                return {"success": False, "error": "key is required for set_config"}

            # Cast value appropriately
            if key in ["stop", "ai_summary_enabled", "enable_proxy"]:
                val_bool = str(value).lower() in ("true", "1", "yes", "on")
                await self.db.update_subscription_global_config(sub_id, key, val_bool)
            elif key in ["interval", "source_group_id", "max_image_number"]:
                val_int = int(value) if value is not None else 0
                await self.db.update_subscription_global_config(sub_id, key, val_int)
            else:
                await self.db.update_subscription_global_config(sub_id, key, str(value))

            return {"success": True, "message": f"Updated subscription {sub_id} {key}={value}"}

        return {"success": False, "error": f"Unknown action {action}"}

    async def handle_rss_group(self, args: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
        kwargs = {**(args or {}), **kwargs}
        action = kwargs.get("action")
        group_id = kwargs.get("group_id")

        if action == "list":
            groups = await self.db.get_all_groups()
            return {
                "success": True,
                "groups": [
                    {
                        "id": g.id,
                        "name": g.name,
                        "schedules": g.schedules,
                        "persona_prompt": g.persona_prompt,
                    }
                    for g in groups
                ],
            }

        if action == "add":
            name = kwargs.get("name") or "New Group"
            schedules = kwargs.get("schedules") or []
            persona_prompt = kwargs.get("persona_prompt")
            new_group = RSSGroup(name=name, schedules=schedules, persona_prompt=persona_prompt)
            new_id = await self.db.add_group(new_group)
            return {"success": True, "group_id": new_id, "name": name}

        if action == "get":
            if not group_id:
                return {"success": False, "error": "group_id is required"}
            group = await self.db.get_group(group_id)
            if not group:
                return {"success": False, "error": f"Group {group_id} not found"}
            return {
                "success": True,
                "group": {
                    "id": group.id,
                    "name": group.name,
                    "schedules": group.schedules,
                    "persona_prompt": group.persona_prompt,
                },
            }

        if action == "update":
            if not group_id:
                return {"success": False, "error": "group_id is required"}
            group = await self.db.get_group(group_id)
            if not group:
                return {"success": False, "error": f"Group {group_id} not found"}
            if "name" in kwargs:
                group.name = kwargs["name"]
            if "schedules" in kwargs:
                group.schedules = kwargs["schedules"]
            if "persona_prompt" in kwargs:
                group.persona_prompt = kwargs["persona_prompt"]
            await self.db.update_group(group)
            return {"success": True, "message": f"Group {group_id} updated"}

        if action == "delete":
            if not group_id:
                return {"success": False, "error": "group_id is required"}
            if group_id == 1:
                return {"success": False, "error": "Cannot delete default group (id=1)"}
            await self.db.delete_group(group_id)
            return {"success": True, "message": f"Group {group_id} deleted"}

        return {"success": False, "error": f"Unknown action {action}"}

    async def handle_rss_fetch(self, args: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
        kwargs = {**(args or {}), **kwargs}
        rsshub_path = kwargs.get("rsshub_path")
        if rsshub_path:
            signed_url = self.fetcher.build_rsshub_url(rsshub_path)
            return {"success": True, "rsshub_url": signed_url}

        sub_id = kwargs.get("subscription_id")
        if sub_id:
            sub = await self.db.get_subscription(sub_id)
            if not sub:
                return {"success": False, "error": f"Subscription {sub_id} not found"}
            new_count = await self.scheduler.fetch_subscription(sub)
            return {
                "success": True,
                "subscription_id": sub_id,
                "name": sub.name,
                "new_articles_count": new_count,
            }

        # Fetch all subscriptions
        subs = await self.db.get_all_subscriptions()
        total_new = 0
        details = []
        for s in subs:
            if s.stop:
                continue
            count = await self.scheduler.fetch_subscription(s)
            total_new += count
            details.append({"id": s.id, "name": s.name, "new_articles": count})

        return {
            "success": True,
            "total_new_articles": total_new,
            "subscriptions": details,
        }

    async def handle_rss_articles(self, args: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
        kwargs = {**(args or {}), **kwargs}
        sub_id = kwargs.get("subscription_id")
        limit = kwargs.get("limit", 20)
        unsent_only = kwargs.get("unsent_only", False)

        if unsent_only and sub_id:
            articles = await self.db.get_unsent_articles(sub_id)
        else:
            articles = await self.db.get_recent_articles(subscription_id=sub_id, limit=limit)

        return {
            "success": True,
            "count": len(articles),
            "articles": [
                {
                    "id": a.id,
                    "subscription_id": a.subscription_id,
                    "title": a.title,
                    "link": a.link,
                    "author": a.author,
                    "published_at": a.published_at.isoformat() if a.published_at else None,
                    "fetched_at": a.fetched_at.isoformat() if a.fetched_at else None,
                    "is_sent": a.is_sent,
                }
                for a in articles
            ],
        }

    async def handle_rss_digest(self, args: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
        kwargs = {**(args or {}), **kwargs}
        group_id = kwargs.get("group_id", 1)
        send = kwargs.get("send", False)

        group = await self.db.get_group(group_id)
        if not group:
            return {"success": False, "error": f"Group {group_id} not found"}

        if send:
            recipient_count = await self.scheduler.run_group_digest(group_id)
            return {
                "success": True,
                "message": f"Digest generated and sent to {recipient_count} recipients for group '{group.name}'",
                "recipients": recipient_count,
            }

        # Preview mode
        subs = await self.db.get_subscriptions_by_group(group_id)
        all_articles = []
        for s in subs:
            if s.id:
                arts = await self.db.get_recent_articles(subscription_id=s.id, limit=10)
                all_articles.extend(arts)

        if not all_articles:
            return {"success": True, "digest": "该分组下暂无可用于生成摘要的文章。"}

        digest_text, count = await self.scheduler.digest_service.generate_digest(
            all_articles, group_id=group_id
        )
        return {
            "success": True,
            "group_name": group.name,
            "article_count": count,
            "digest": digest_text,
        }

    async def handle_rss_subscriber(self, args: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
        kwargs = {**(args or {}), **kwargs}
        action = kwargs.get("action")
        sub_id = kwargs.get("subscription_id")
        umo = kwargs.get("umo")

        if not sub_id:
            return {"success": False, "error": "subscription_id is required"}

        if action == "list":
            subscribers = await self.db.get_subscribers(sub_id)
            return {
                "success": True,
                "subscribers": [
                    {
                        "id": s.id,
                        "subscription_id": s.subscription_id,
                        "umo": s.umo,
                        "personal_config": s.personal_config,
                    }
                    for s in subscribers
                ],
            }

        if not umo:
            return {"success": False, "error": "umo is required for add/get/update/delete"}

        if action == "add":
            existing = await self.db.get_subscriber(sub_id, umo)
            if existing:
                return {"success": False, "error": f"Subscriber {umo} already subscribed to {sub_id}"}
            cfg = kwargs.get("config") or {}
            new_sub = Subscriber(subscription_id=sub_id, umo=umo, personal_config=cfg)
            subscriber_id = await self.db.add_subscriber(new_sub)
            return {"success": True, "subscriber_id": subscriber_id, "umo": umo}

        if action == "get":
            s = await self.db.get_subscriber(sub_id, umo)
            if not s:
                return {"success": False, "error": f"Subscriber {umo} not found"}
            return {
                "success": True,
                "subscriber": {
                    "id": s.id,
                    "subscription_id": s.subscription_id,
                    "umo": s.umo,
                    "personal_config": s.personal_config,
                },
            }

        if action == "update":
            s = await self.db.get_subscriber(sub_id, umo)
            if not s:
                return {"success": False, "error": f"Subscriber {umo} not found"}
            new_cfg = kwargs.get("config") or {}
            s.personal_config.update(new_cfg)
            await self.db.update_subscriber(s)
            return {"success": True, "message": f"Updated subscriber {umo}"}

        if action == "delete":
            await self.db.delete_subscriber(sub_id, umo)
            return {"success": True, "message": f"Deleted subscriber {umo}"}

        return {"success": False, "error": f"Unknown action {action}"}


def register_all_tools(
    ctx: Any,
    db: Database,
    scheduler: RSSScheduler,
    fetcher: RSSFetcher,
    config: AIRSSConfig,
) -> None:
    """Register all 6 tools into Hermes Agent."""
    handlers = AirssToolHandlers(db, scheduler, fetcher, config)

    tools = [
        ("rss_subscription", RSS_SUBSCRIPTION_SCHEMA, handlers.handle_rss_subscription, "📡"),
        ("rss_group", RSS_GROUP_SCHEMA, handlers.handle_rss_group, "📁"),
        ("rss_fetch", RSS_FETCH_SCHEMA, handlers.handle_rss_fetch, "🔄"),
        ("rss_articles", RSS_ARTICLES_SCHEMA, handlers.handle_rss_articles, "📰"),
        ("rss_digest", RSS_DIGEST_SCHEMA, handlers.handle_rss_digest, "📑"),
        ("rss_subscriber", RSS_SUBSCRIBER_SCHEMA, handlers.handle_rss_subscriber, "👤"),
    ]

    def _wrap(fn):
        async def _wrapped(*args, **kwargs):
            res = await fn(*args, **kwargs)
            return json.dumps(res, ensure_ascii=False) if isinstance(res, (dict, list)) else str(res)
        return _wrapped

    for name, schema, handler, emoji in tools:
        if hasattr(ctx, "register_tool"):
            ctx.register_tool(
                name=name,
                toolset="airss",
                schema=schema,
                handler=_wrap(handler),
                is_async=True,
                emoji=emoji,
                description=schema["description"],
            )
            logger.info(f"Registered tool {name} into Hermes toolset 'airss'")
