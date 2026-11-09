"""Configuration loader for Hermes airss plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class AIRSSConfig:
    ai_config: Dict[str, Any] = field(
        default_factory=lambda: {
            "ai_provider": "",
            "ai_summary_timezone": "Asia/Shanghai",
            "ai_digest_max_articles": 50,
            "ai_digest_max_input_tokens": 131072,
            "ai_digest_max_output_tokens": 8192,
            "ai_digest_min_chars": 150,
            "ai_digest_title_max_len": 120,
            "ai_digest_content_max_len": 2048,
            "ai_digest_recent_days": 0,
            "ai_fallback_message": "",
            "ai_digest_image_enabled": True,
            "ai_digest_image_width": 900,
            "ai_digest_image_scale": 2,
            "ai_filter_provider": "",
            "ai_filter_model_type": "generative",
            "ai_filter_decision_url": "https://openrouter.ai/api/alpha/decisions",
            "ai_filter_decision_key": "",
            "ai_filter_decision_model": "typesafe/jev-1.13",
            "ai_filter_decision_threshold": 0.5,
            "ai_filter_recent_minutes": 30,
        }
    )
    fetch_config: Dict[str, Any] = field(
        default_factory=lambda: {
            "default_interval": 5,
            "max_error_count": 100,
            "max_concurrent_fetches": 5,
            "request_timeout": 30,
            "max_image_number": 0,
            "enable_spoiler": True,
            "backlog_interval": 5,
        }
    )
    rsshub_config: Dict[str, Any] = field(
        default_factory=lambda: {
            "rsshub_url": "",
            "rsshub_key": "",
        }
    )
    proxy_config: Dict[str, Any] = field(
        default_factory=lambda: {
            "proxy": "",
            "enable_proxy": False,
        }
    )
    storage_config: Dict[str, Any] = field(
        default_factory=lambda: {
            "article_retention_days": 30,
        }
    )
    delivery_config: Dict[str, Any] = field(
        default_factory=lambda: {
            "default_webhook": "",
            "telegram_bot_token": "",
        }
    )

    @classmethod
    def from_dict(cls, data: Dict[str, Any] | None) -> "AIRSSConfig":
        if not data:
            return cls()
        cfg = cls()
        for key in [
            "ai_config",
            "fetch_config",
            "rsshub_config",
            "proxy_config",
            "storage_config",
            "delivery_config",
        ]:
            if key in data and isinstance(data[key], dict):
                getattr(cfg, key).update(data[key])
        return cfg
