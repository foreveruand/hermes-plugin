"""Agent tool implementations for audio extraction and video clipping."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

try:
    from .core import clip_video_file, extract_audio_file, parse_interval, resolve_target_file, resolve_target_files, search_files, sync_subtitles_once
except ImportError:
    from core import clip_video_file, extract_audio_file, parse_interval, resolve_target_file, resolve_target_files, search_files, sync_subtitles_once

logger = logging.getLogger("audio-extract")


def register_all_tools(ctx: Any, cfg: dict[str, Any]) -> None:
    """Register LLM agent tools with Hermes PluginContext."""

    def tool_video_clip(args: dict[str, Any], **kwargs: Any) -> str:
        """Fast clip video segment."""
        target = args.get("target", "").strip()
        start = args.get("start", "").strip()
        end = args.get("end", "").strip()
        interval = args.get("interval", "").strip()
        out = args.get("out", "").strip()
        reencode = bool(args.get("reencode", False))

        if not target:
            return json.dumps({"success": False, "error": "Parameter 'target' is required."})

        if interval:
            try:
                start, end = parse_interval(interval)
            except Exception as e:
                return json.dumps({"success": False, "error": f"Invalid interval: {e}"})

        if not start or not end:
            return json.dumps({"success": False, "error": "Both start and end time (or interval) are required."})

        resolved = resolve_target_file(target, cfg)
        if not resolved:
            return json.dumps({"success": False, "error": f"Target media file not found: {target}"})

        out_path = Path(out) if out else None
        try:
            out_file, log = clip_video_file(
                input_file=resolved,
                start_time=start,
                end_time=end,
                output_path=out_path,
                reencode=reencode,
            )
            return json.dumps(
                {
                    "success": True,
                    "output_file": str(out_file),
                    "input_file": str(resolved),
                    "start": start,
                    "end": end,
                    "reencode": reencode,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    def tool_audio_extract(args: dict[str, Any], **kwargs: Any) -> str:
        """Extract audio for Whisper transcription."""
        target = args.get("target", "").strip()
        fmt = args.get("format", "mp3").strip()
        out_dir = args.get("out_dir", "").strip()
        no_job = bool(args.get("no_job", False))

        if not target:
            return json.dumps({"success": False, "error": "Parameter 'target' is required."})

        resolved_list = resolve_target_files(target, cfg, skip_with_subtitles=True)
        if not resolved_list:
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Target not found or has no files needing transcription "
                        f"(matching files already have subtitles/lyrics): {target}"
                    ),
                }
            )

        target_out_dir = Path(out_dir) if out_dir else Path(cfg["out_dir"])
        jobs_dir = None if no_job else Path(cfg["jobs_dir"])

        outputs: list[str] = []
        failures: list[dict[str, str]] = []
        for in_path in resolved_list:
            try:
                out_file, log = extract_audio_file(
                    input_file=in_path,
                    out_dir=target_out_dir,
                    jobs_dir=jobs_dir,
                    format=fmt,
                )
                outputs.append(str(out_file))
            except Exception as e:
                failures.append({"input": str(in_path), "error": str(e)})

        if failures and not outputs:
            return json.dumps({"success": False, "error": failures[0]["error"]}, ensure_ascii=False)

        return json.dumps(
            {
                "success": True,
                "output_files": outputs,
                "input_files": [str(p) for p in resolved_list],
                "count": len(outputs),
                "failed": failures,
                "format": fmt,
                "job_registered": not no_job,
            },
            ensure_ascii=False,
        )

    def tool_audio_search(args: dict[str, Any], **kwargs: Any) -> str:
        """Search media files in library."""
        keyword = args.get("keyword", "").strip()
        limit = int(args.get("limit", 15))

        if not keyword:
            return json.dumps({"success": False, "error": "Parameter 'keyword' is required."})

        matches = search_files(keyword, cfg, limit=limit, skip_with_subtitles=True)
        return json.dumps(
            {
                "success": True,
                "keyword": keyword,
                "matches": matches,
                "count": len(matches),
            },
            ensure_ascii=False,
        )

    def tool_audio_sync_subtitles(args: dict[str, Any], **kwargs: Any) -> str:
        """Scan and synchronize subtitles back to media sources."""
        try:
            cnt = sync_subtitles_once(cfg)
            return json.dumps(
                {
                    "success": True,
                    "synced_count": cnt,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    # Register each tool
    if hasattr(ctx, "register_tool"):
        ctx.register_tool(
            name="video_clip",
            toolset="audio_extract",
            schema={
                "name": "video_clip",
                "description": "Fast lossless video clipping via FFmpeg stream copy (or optional re-encode).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Filename, keyword, or path to video file"},
                        "start": {"type": "string", "description": "Start timestamp (HH:MM:SS or MM:SS)"},
                        "end": {"type": "string", "description": "End timestamp (HH:MM:SS or MM:SS)"},
                        "interval": {"type": "string", "description": "Optional compact interval like 00:01:00-00:02:00 or 10500-11000"},
                        "reencode": {"type": "boolean", "description": "Whether to re-encode (default false, stream copy)"},
                        "out": {"type": "string", "description": "Optional custom output destination path"},
                    },
                    "required": ["target"],
                },
            },
            handler=tool_video_clip,
        )

        ctx.register_tool(
            name="audio_extract",
            toolset="audio_extract",
            schema={
                "name": "audio_extract",
                "description": "Extract 16kHz mono audio from media file for Whisper speech-to-text transcription. Files that already have a matching subtitle/lyric sidecar (srt/ass/vtt/lrc) are skipped.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Filename, keyword, file path, or directory path (a directory extracts its immediate matching media files, without recursing into subdirectories)"},
                        "format": {"type": "string", "description": "Audio format (default mp3)"},
                        "out_dir": {"type": "string", "description": "Optional output directory"},
                        "no_job": {"type": "boolean", "description": "Skip registering in .jobs for subtitle sync"},
                    },
                    "required": ["target"],
                },
            },
            handler=tool_audio_extract,
        )

        ctx.register_tool(
            name="audio_search",
            toolset="audio_extract",
            schema={
                "name": "audio_search",
                "description": "Search indexed media files across configured media library directories. Files that already have a subtitle/lyric sidecar are excluded.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "Search keyword or filename substring"},
                        "limit": {"type": "integer", "description": "Maximum number of results to return (default 15)"},
                    },
                    "required": ["keyword"],
                },
            },
            handler=tool_audio_search,
        )

        ctx.register_tool(
            name="audio_sync_subtitles",
            toolset="audio_extract",
            schema={
                "name": "audio_sync_subtitles",
                "description": "Scan temporary directory for generated subtitles and synchronize them back to media files.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            handler=tool_audio_sync_subtitles,
        )
