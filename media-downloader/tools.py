"""Agent tool implementations for the media downloader."""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from .core import is_asmr_target, parse_extra_args, resolve_destination
    from .tasks import get_originating_delivery_target, submit_task
except ImportError:
    from core import is_asmr_target, parse_extra_args, resolve_destination
    from tasks import get_originating_delivery_target, submit_task

logger = logging.getLogger("media-downloader")


def register_all_tools(ctx: Any, cfg: dict[str, Any]) -> None:
    """Register LLM agent tools with Hermes PluginContext."""

    def tool_media_download(args: dict[str, Any], **kwargs: Any) -> str:
        """Submit a media download to the background worker and return its task id."""
        url = str(args.get("url", "")).strip()
        rj_code = str(args.get("rj_code", "")).strip()
        if not url and rj_code:
            url = rj_code
        if not url:
            return json.dumps({"success": False, "error": "Parameter 'url' or 'rj_code' is required."}, ensure_ascii=False)

        category = str(args.get("type", "auto")).strip() or "auto"
        if category not in ("auto", "video", "image", "asmr"):
            return json.dumps({"success": False, "error": f"Unsupported type: {category}"}, ensure_ascii=False)
        if is_asmr_target(url):
            category = "asmr"

        author = str(args.get("author", "")).strip()
        out_dir = str(args.get("out", "")).strip()

        try:
            final_dir, label = resolve_destination(url, cfg, category=category,
                                                   author=author, out_dir=out_dir)
        except Exception as e:
            return json.dumps({"success": False, "error": f"Cannot resolve destination: {e}"}, ensure_ascii=False)

        params = {
            "type": category,
            "author": author,
            "out": out_dir,
            "proxy": str(args.get("proxy", "")).strip(),
            "no_proxy": bool(args.get("no_proxy", False)),
            "force_proxy": bool(args.get("force_proxy", False)),
            "extra_args": parse_extra_args(str(args.get("extra", ""))),
            "category": label,
        }

        try:
            task = submit_task(
                task_type="download",
                target=url,
                params=params,
                delivery_target=get_originating_delivery_target(),
                cfg=cfg,
            )
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        return json.dumps(
            {
                "success": True,
                "async": True,
                "task_id": task.id,
                "url": url,
                "category": label,
                "destination": str(final_dir),
                "note": "下载在后台线程执行，完成后自动推送简报；可用 /dlstatus <任务ID> 查询。",
            },
            ensure_ascii=False,
        )

    if hasattr(ctx, "register_tool"):
        ctx.register_tool(
            name="media_download",
            toolset="media_downloader",
            schema={
                "name": "media_download",
                "description": (
                    "Download media via yt-dlp / gallery-dl, or download an ASMR.ONE work by "
                    "DLSite work number (RJ code). ASMR works are tagged with dvtag after the "
                    "audio is downloaded, then archived into the media library. Runs in a "
                    "background thread and pushes a completion briefing; returns a task id "
                    "immediately instead of blocking."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Media URL to download"},
                        "rj_code": {
                            "type": "string",
                            "description": "ASMR.ONE / DLSite work number such as RJ123456 (alternative to url)",
                        },
                        "type": {
                            "type": "string",
                            "description": "Content category: auto, video, image or asmr (default auto)",
                        },
                        "author": {"type": "string", "description": "Author subdirectory for Pixiv / X galleries"},
                        "out": {"type": "string", "description": "Optional custom archive directory (overrides auto classification)"},
                        "proxy": {"type": "string", "description": "Optional custom proxy URL"},
                        "no_proxy": {"type": "boolean", "description": "Force direct connection"},
                        "force_proxy": {"type": "boolean", "description": "Force proxy usage"},
                        "extra": {"type": "string", "description": "Extra arguments passed through to yt-dlp / gallery-dl"},
                    },
                    "required": ["url"],
                },
            },
            handler=tool_media_download,
        )
