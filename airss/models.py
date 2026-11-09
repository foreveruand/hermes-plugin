"""Data models and config mapping for airss plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# Adapter types
TELEGRAM_ADAPTER = "telegram"
WECOM_ADAPTER = "wecom"
WEBHOOK_ADAPTER = "webhook"

# Configurable fields for subscription global config
GLOBAL_CONFIGURABLE_FIELDS = [
    "name",
    "url",
    "black_keyword",
    "cookies",
    "content_to_remove",
    "max_image_number",
    "interval",
    "ai_summary_enabled",
    "enable_proxy",
    "stop",
    "source_group_id",
]


@dataclass
class RSSSubscription:
    """RSS subscription model."""

    id: Optional[int] = None
    name: str = ""
    url: str = ""
    interval: int = 5  # fetch interval in minutes
    source_group_id: int = 1  # group ID for digest scheduling

    # Global config fields
    cookies: Optional[str] = None
    black_keyword: Optional[str] = None
    content_to_remove: Optional[str] = None  # regex pattern to remove from content
    max_image_number: int = 0  # 0 = unlimited
    ai_summary_enabled: bool = False  # True = aggregate in digest, False = send individually
    enable_proxy: bool = False
    stop: bool = False

    # Internal tracking
    error_count: int = 0
    last_fetch: Optional[datetime] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None


@dataclass
class RSSArticle:
    """RSS article model."""

    id: Optional[int] = None
    subscription_id: int = 0
    title: str = ""
    content: str = ""
    link: str = ""
    guid: str = ""
    author: str = ""
    published_at: Optional[datetime] = None
    fetched_at: datetime = field(default_factory=datetime.now)
    is_sent: bool = False
    image_urls: List[str] = field(default_factory=list)
    ai_filter_result: Optional[bool] = None
    ai_filter_checked_at: Optional[datetime] = None


@dataclass
class RSSGroup:
    """RSS group model for organizing subscriptions and digest schedules."""

    id: Optional[int] = None
    name: str = ""
    schedules: List[str] = field(default_factory=list)  # HH:MM or 5-field cron
    persona_id: Optional[str] = None
    persona_prompt: Optional[str] = None


@dataclass
class Subscriber:
    """Subscriber model linking a user/session/channel to a subscription."""

    id: Optional[int] = None
    subscription_id: int = 0
    umo: str = ""  # "platform:chat_id", "session_id", or webhook URL
    personal_config: Dict[str, Any] = field(default_factory=dict)

    def is_webhook(self) -> bool:
        """Check if this subscriber uses webhook (URL instead of umo)."""
        return self.umo.startswith("http://") or self.umo.startswith("https://")

    def get_adapter(self) -> str:
        """Get adapter type from umo."""
        if self.is_webhook():
            return WEBHOOK_ADAPTER
        parts = self.umo.split(":", 1)
        if len(parts) > 1:
            return parts[0]
        return TELEGRAM_ADAPTER

    def get_webhook_url(self) -> Optional[str]:
        """Get webhook URL if this is a webhook subscriber."""
        if self.is_webhook():
            return self.umo
        return None


# Personalization config keys (per-subscriber)
PERSONAL_CONFIG_KEYS = {
    "only_title": False,  # Only send article title
    "only_pic": False,  # Only send images
    "only_has_pic": False,  # Only send if article has image
    "enable_spoiler": True,  # Spoiler images (default True)
    "stop": False,  # Pause subscription for this subscriber
    "black_keyword": "",  # Keyword filter
    "white_keyword": "",  # Keyword allowlist filter
    "ai_filter_enabled": False,  # AI topic duplicate filtering
}

# Config number to name mapping for interactive selection
CONFIG_NUMBER_MAP = {
    "①": "only_title",
    "②": "only_pic",
    "③": "only_has_pic",
    "④": "enable_spoiler",
    "⑤": "stop",
    "⑥": "black_keyword",
    "⑦": "interval",
    "⑧": "max_image_number",
    "⑨": "ai_summary_enabled",
    "⑩": "enable_proxy",
    "⑪": "source_group_id",
    "⑫": "black_keyword",
    "⑬": "white_keyword",
    "⑭": "ai_filter_enabled",
}

# Config name to number mapping
CONFIG_NAME_MAP = {
    "only_title": "①",
    "only_pic": "②",
    "only_has_pic": "③",
    "enable_spoiler": "④",
    "stop": "⑤",
    "black_keyword": "⑥",
    "white_keyword": "⑬",
    "ai_filter_enabled": "⑭",
    "interval": "⑦",
    "max_image_number": "⑧",
    "ai_summary_enabled": "⑨",
    "enable_proxy": "⑩",
    "source_group_id": "⑪",
}

CONFIG_DESCRIPTIONS = {
    "only_title": "① 仅发送标题 - 当前: {value}",
    "only_pic": "② 仅发送图片 - 当前: {value}",
    "only_has_pic": "③ 仅发送有图片的文章 - 当前: {value}",
    "enable_spoiler": "④ 图片剧透标签 - 当前: {value}",
    "stop": "⑤ 暂停订阅 - 当前: {value}",
    "interval": "⑦ 抓取间隔（分钟）- 当前: {value}",
    "max_image_number": "⑧ 最大图片数 - 当前: {value}",
    "ai_summary_enabled": "⑨ AI摘要开关 - 当前: {value}",
    "enable_proxy": "⑩ 使用代理 - 当前: {value}",
    "source_group_id": "⑪ 所属分组ID - 当前: {value}",
    "black_keyword_personal": "⑥ 关键词黑名单 - 当前: {value}",
    "black_keyword_global": "⑫ 关键词黑名单（全局）- 当前: {value}",
    "white_keyword": "⑬ 关键词白名单 - 当前: {value}",
    "ai_filter_enabled": "⑭ AI筛选过滤 - 当前: {value}",
}

PERSONAL_CONFIG_NUMBERS = ["①", "②", "③", "④", "⑤", "⑥", "⑬", "⑭"]
GLOBAL_CONFIG_NUMBERS = ["⑦", "⑧", "⑨", "⑩", "⑪", "⑫"]


def get_config_number(name: str) -> Optional[str]:
    return CONFIG_NAME_MAP.get(name)


def get_config_name(number: str) -> Optional[str]:
    return CONFIG_NUMBER_MAP.get(number)


def get_effective_bool(
    subscriber: Subscriber, key: str, subscription: RSSSubscription
) -> bool:
    config = subscriber.personal_config or {}
    if key in config:
        return bool(config[key])
    return bool(getattr(subscription, key, False))


def get_effective_text(
    subscriber: Subscriber, key: str, subscription: RSSSubscription
) -> str:
    config = subscriber.personal_config or {}
    if key in config:
        return str(config[key])
    return str(getattr(subscription, key, ""))
