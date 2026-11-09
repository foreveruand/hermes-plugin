"""Slash command handlers for Hermes Audio Extract & Video Clip plugin."""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from pathlib import Path
from typing import Any

try:
    from .config import DEFAULT_CONFIG
    from .core import is_record_source, list_active_jobs, parse_interval, record_upload_path, resolve_target_file, resolve_target_files, search_candidates, search_files, sync_subtitles_once
    from .tasks import get_originating_delivery_target, list_recent_tasks, load_task, submit_task
    from .telegram_ui import get_pending, has_bot, register_pending, send_selection_prompt
except ImportError:
    from config import DEFAULT_CONFIG
    from core import is_record_source, list_active_jobs, parse_interval, record_upload_path, resolve_target_file, resolve_target_files, search_candidates, search_files, sync_subtitles_once
    from tasks import get_originating_delivery_target, list_recent_tasks, load_task, submit_task
    from telegram_ui import get_pending, has_bot, register_pending, send_selection_prompt

logger = logging.getLogger("audio-extract")

# Cap on how many keyword hits we offer for interactive selection.
_MAX_CANDIDATES = 10


def _session_chat_id() -> str:
    """Chat id of the invoking session, for sending the selection keyboard."""
    try:
        from gateway.session_context import get_session_env

        return (get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
    except Exception:
        return ""


class AudioExtractCommands:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    def handle_vclip(self, raw_args: str) -> str:
        """Handle /vclip slash command."""
        text = raw_args.strip()
        if not text or text in ("-h", "--help", "help"):
            return (
                "🎬 **/vclip 视频剪辑用法**\n\n"
                "• **标准用法**: `/vclip <关键词或路径> <开始时间> <结束时间>`\n"
                "  例: `/vclip MIKR-116 00:05:00 00:10:00`\n"
                "• **区间简写**: `/vclip <关键词> --interval <起始-终止>`\n"
                "  例: `/vclip MIKR-116 --interval 10500-11000`\n"
                "  或: `/vclip MIKR-116 10500-11000`\n\n"
                "**可选参数:**\n"
                "• `--reencode`: 重新编码 (x264/aac, 兼容性好但较慢)\n"
                "• `--out <路径>`: 指定输出文件路径\n"
                "• `--sync`: 同步等待完成并返回结果\n\n"
                "**录播目录** (`record_dir`):\n"
                "• 剪辑产物直接放入 `<录播目录>/upload/`，沿用原文件名\n"
                "• 剪辑成功后删除源录播文件及其 nfo / poster / mobile 关联文件\n\n"
                "*提示: 剪辑任务将在后台运行，完成后自动发送通知，不再受 30 秒超时限制。*"
            )

        try:
            tokens = shlex.split(text)
        except Exception:
            tokens = text.split()

        target = ""
        start_time = ""
        end_time = ""
        interval = ""
        out_path = ""
        reencode = False
        sync_mode = False

        pos_args = []
        i = 0
        while i < len(tokens):
            arg = tokens[i]
            if arg == "--reencode":
                reencode = True
                i += 1
            elif arg == "--sync":
                sync_mode = True
                i += 1
            elif arg in ("--out", "-o") and i + 1 < len(tokens):
                out_path = tokens[i + 1]
                i += 2
            elif arg in ("--interval", "-i") and i + 1 < len(tokens):
                interval = tokens[i + 1]
                i += 2
            elif arg.startswith("--interval="):
                interval = arg.split("=", 1)[1]
                i += 1
            elif arg.startswith("--out="):
                out_path = arg.split("=", 1)[1]
                i += 1
            else:
                pos_args.append(arg)
                i += 1

        if not pos_args:
            return "❌ 请提供目标文件名或搜索关键词。输入 `/vclip --help` 查看用法。"

        target = pos_args[0]

        if interval:
            try:
                start_time, end_time = parse_interval(interval)
            except ValueError as ve:
                return f"❌ 时间区间格式错误: {ve}"
        elif len(pos_args) == 2 and "-" in pos_args[1]:
            try:
                start_time, end_time = parse_interval(pos_args[1])
            except ValueError as ve:
                return f"❌ 时间区间格式错误: {ve}"
        elif len(pos_args) >= 3:
            start_time = pos_args[1]
            end_time = pos_args[2]

        if not start_time or not end_time:
            return (
                "❌ 缺少开始或结束时间。\n"
                "格式应为: `/vclip <目标> <开始时间> <结束时间>` "
                "或 `/vclip <目标> 00:01:00-00:02:30`"
            )

        # Resolve target
        resolved = resolve_target_file(target, self.cfg)
        if not resolved:
            return (
                f"❌ 未找到匹配的媒体文件: `{target}`\n"
                "可先使用 `/audiosearch <关键词>` 确认库中是否存在该文件。"
            )

        # Livestream recordings get the move_clip.sh workflow: the clipped segment is
        # published from <record_dir>/upload and the source plus its Emby sidecars are
        # removed once the clip has been written. An explicit --out overrides the
        # destination but not the cleanup.
        record_source = is_record_source(resolved, self.cfg)
        if record_source and not out_path:
            out_path = str(record_upload_path(resolved, self.cfg))

        delivery_target = get_originating_delivery_target()
        params = {
            "start": start_time,
            "end": end_time,
            "interval": interval,
            "out": out_path,
            "reencode": reencode,
            "cleanup_source": record_source,
        }

        task = submit_task(
            task_type="clip",
            target=target,
            resolved_path=str(resolved),
            params=params,
            delivery_target=delivery_target,
            cfg=self.cfg,
        )

        mode_str = "重新编码 (x264/aac)" if reencode else "极速无损 (stream copy)"

        if sync_mode:
            # Poll task for completion (up to 120s)
            tasks_dir = self.cfg.get("tasks_dir", DEFAULT_CONFIG["tasks_dir"])
            start_wait = time.time()
            while time.time() - start_wait < 120:
                t = load_task(task.id, tasks_dir)
                if t and t.status in ("completed", "failed"):
                    if t.status == "completed":
                        deleted = len(t.params.get("deleted_files") or [])
                        cleanup_line = f"• **已清理**: `{deleted}` 个源文件/关联文件\n" if deleted else ""
                        return (
                            f"✅ **视频剪辑完成**\n\n"
                            f"• **任务 ID**: `{task.id}`\n"
                            f"• **文件**: `{resolved.name}`\n"
                            f"• **片段**: `{start_time} - {end_time}`\n"
                            f"• **耗时**: `{t.duration_seconds():.1f}s`\n"
                            f"{cleanup_line}"
                            f"• **输出路径**:\n`{t.output_path}`"
                        )
                    else:
                        return f"❌ **视频剪辑失败**: {t.error or '未知错误'}"
                time.sleep(0.5)

        record_line = (
            "• **录播处理**: 输出到 `upload/`，完成后删除源文件及关联 nfo/poster\n"
            if record_source
            else ""
        )

        return (
            f"⏳ **视频剪辑任务已提交后台处理**\n\n"
            f"• **任务 ID**: `{task.id}`\n"
            f"• **源文件**: `{resolved.name}`\n"
            f"• **时间片段**: `{start_time}` - `{end_time}`\n"
            f"• **处理模式**: `{mode_str}`\n"
            f"{record_line}\n"
            f"*提示: 剪辑不受 30 秒超时限制，完成后将自动发送通知。可用 `/vclipstatus {task.id}` 查询状态。*"
        )

    async def handle_auex(self, raw_args: str) -> str | None:
        """Handle /auex slash command for Whisper audio extraction."""
        text = raw_args.strip()
        if not text or text in ("-h", "--help", "help"):
            return (
                "🎵 **/auex 音频提取用法**\n\n"
                "• **用法**: `/auex <关键词|文件|目录> [--format mp3] [--no-job]`\n"
                "  例: `/auex MIKR-116`\n"
                "  例: `/auex /mnt/media/MediaServer/某目录`\n"
                "• **选项**:\n"
                "  `--format <格式>`: 音频格式 (默认: mp3)\n"
                "  `--no-job`: 不注册待转写任务 (.jobs)\n\n"
                "*提取出 16kHz 单声道音频 (Whisper 优化)，保存在配置的临时目录中。*\n"
                "*传入目录时仅处理该目录下符合格式的媒体文件，不递归子目录。*\n"
                "*扫描时会自动跳过已有字幕/歌词 (srt/ass/vtt/lrc 等外挂文件) 的文件。*\n"
                "*关键词命中多个文件时，将列出候选供你勾选（可多选）。*"
            )

        tokens = text.split()
        target = tokens[0]
        fmt = "mp3"
        no_job = False

        if "--format" in tokens:
            idx = tokens.index("--format")
            if idx + 1 < len(tokens):
                fmt = tokens[idx + 1]
        if "--no-job" in tokens:
            no_job = True

        target_path = Path(target).expanduser()

        # Explicit file or directory keeps the existing behaviour: a file is a single
        # target, a directory is an intentional batch (nothing ambiguous to resolve).
        # Scanning skips files that already have a subtitle/lyric sidecar, so only
        # files still needing transcription remain. An explicit file path is honored.
        if target_path.is_file() or target_path.is_dir():
            resolved_list = await asyncio.to_thread(
                resolve_target_files, target, self.cfg, True
            )
            if not resolved_list:
                if target_path.is_dir():
                    return (
                        f"❌ 目录内没有可提取的媒体文件"
                        f"（匹配文件均已存在字幕/歌词，已全部跳过）: `{target}`"
                    )
                return f"❌ 未找到匹配的媒体文件: `{target}`"
            return self._submit_extract(target, resolved_list, fmt, no_job, target_path.is_dir())

        # Keyword search: surface every hit so the user chooses which files to process.
        candidates = await asyncio.to_thread(search_candidates, target, self.cfg, _MAX_CANDIDATES)
        if not candidates:
            return f"❌ 未找到匹配的媒体文件: `{target}`"

        if len(candidates) > 1:
            chat_id = _session_chat_id()
            if has_bot() and chat_id:
                token = register_pending(
                    chat_id, get_originating_delivery_target(), target, candidates, fmt, no_job
                )
                entry = get_pending(token)
                if entry is not None and await send_selection_prompt(chat_id, token, entry):
                    # Keyboard delivered; the callback handler submits the chosen files.
                    return None
            return self._format_candidate_list(target, candidates)

        return self._submit_extract(target, candidates, fmt, no_job, False)

    def _submit_extract(
        self, target: str, resolved_list: list[Path], fmt: str, no_job: bool, is_directory: bool
    ) -> str:
        """Submit an extract task for the resolved files and return a confirmation message."""
        delivery_target = get_originating_delivery_target()
        params = {"format": fmt, "no_job": no_job}
        if is_directory:
            params["batch"] = True
            params["inputs"] = [str(p) for p in resolved_list]
            resolved_path = str(Path(target).expanduser().resolve())
        else:
            resolved_path = str(resolved_list[0])

        task = submit_task(
            task_type="extract",
            target=target,
            resolved_path=resolved_path,
            params=params,
            delivery_target=delivery_target,
            cfg=self.cfg,
        )

        if is_directory:
            return (
                f"⏳ **音频提取任务已提交后台处理**\n\n"
                f"• **任务 ID**: `{task.id}`\n"
                f"• **来源目录**: `{Path(target).expanduser().name}` ({len(resolved_list)} 个文件)\n"
                f"• **目标格式**: `{fmt} (16kHz 单声道)`\n"
                f"• **注册任务**: `{'否' if no_job else '是 (.jobs)'}`\n\n"
                f"*仅处理该目录下的媒体文件，不含子目录。完成后将自动发送通知。*"
            )

        return (
            f"⏳ **音频提取任务已提交后台处理**\n\n"
            f"• **任务 ID**: `{task.id}`\n"
            f"• **源文件**: `{resolved_list[0].name}`\n"
            f"• **目标格式**: `{fmt} (16kHz 单声道)`\n"
            f"• **注册任务**: `{'否' if no_job else '是 (.jobs)'}`\n\n"
            f"*处理完成后将自动发送通知。可通过 `/vclipstatus {task.id}` 查询状态。*"
        )

    @staticmethod
    def _format_candidate_list(target: str, candidates: list[Path]) -> str:
        """Fallback (no Telegram bot): list the keyword matches so the user can re-target."""
        lines = [f"🎵 关键词 `{target}` 命中 {len(candidates)} 个文件，请指定要处理的目标：\n"]
        for i, p in enumerate(candidates, 1):
            lines.append(f"**{i}.** `{p.name}`\n   路径: `{p}`")
        lines.append("\n💡 可改用更精确的关键词，或直接传入完整文件路径 / 目录。")
        return "\n".join(lines)

    def handle_subsync(self, raw_args: str) -> str:
        """Handle /subsync slash command."""
        cnt = sync_subtitles_once(self.cfg)
        if cnt > 0:
            return f"🔄 **字幕同步完成**: 成功处理并归档了 {cnt} 个字幕任务。"
        return "🔄 **字幕同步扫描完成**: 暂无待同步的字幕文件。"

    def handle_audiosearch(self, raw_args: str) -> str:
        """Handle /audiosearch slash command."""
        keyword = raw_args.strip()
        if not keyword:
            return "❌ 用法: `/audiosearch <关键词>`"

        matches = search_files(keyword, self.cfg, limit=10)
        if not matches:
            return f"🔍 未找到包含关键词 `{keyword}` 的媒体文件。"

        lines = [f"🔍 **找到 {len(matches)} 个匹配文件:**\n"]
        for i, m in enumerate(matches, 1):
            p = Path(m)
            lines.append(f"**{i}.** `{p.name}`\n   路径: `{m}`")
        return "\n".join(lines)

    def handle_audiojobs(self, raw_args: str) -> str:
        """Handle /audiojobs slash command."""
        jobs = list_active_jobs(self.cfg)
        if not jobs:
            return "📋 当前没有待处理的音频转写任务。"

        lines = [f"📋 **当前活跃转写任务 (共 {len(jobs)} 个):**\n"]
        for j in jobs[:10]:
            name = j.get("job_name", "未知")
            v = Path(j.get("video", "")).name
            created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(j.get("created_at", 0)))
            lines.append(f"• **{name}**\n  视频: `{v}`\n  时间: `{created}`")
        return "\n".join(lines)

    def handle_audiotasks(self, raw_args: str) -> str:
        """Handle /audiotasks slash command."""
        tasks_dir = self.cfg.get("tasks_dir", DEFAULT_CONFIG["tasks_dir"])
        tasks = list_recent_tasks(tasks_dir, limit=10)
        if not tasks:
            return "📋 暂无历史后台任务记录。"

        lines = [f"📋 **最近后台任务 (共 {len(tasks)} 条):**\n"]
        status_map = {
            "pending": "⏳ 等待中",
            "running": "⚡ 运行中",
            "completed": "✅ 已完成",
            "failed": "❌ 失败",
        }
        for t in tasks:
            st = status_map.get(t.status, t.status)
            created = time.strftime("%H:%M:%S", time.localtime(t.created_at))
            target_name = Path(t.resolved_path).name if t.resolved_path else t.target
            lines.append(
                f"• `{t.id}` [{st}]\n"
                f"  类型: {t.type} | 目标: `{target_name}` | 提交: {created}"
            )
        return "\n".join(lines)

    def handle_vclipstatus(self, raw_args: str) -> str:
        """Handle /vclipstatus slash command."""
        task_id = raw_args.strip()
        if not task_id:
            return "❌ 用法: `/vclipstatus <任务ID>`"

        tasks_dir = self.cfg.get("tasks_dir", DEFAULT_CONFIG["tasks_dir"])
        task = load_task(task_id, tasks_dir)
        if not task:
            return f"❌ 未找到任务 ID: `{task_id}`"

        status_map = {
            "pending": "⏳ 队列等待中",
            "running": "⚡ 正在处理",
            "completed": "✅ 已完成",
            "failed": "❌ 处理失败",
        }
        st_text = status_map.get(task.status, task.status)
        created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.created_at))
        duration = f"{task.duration_seconds():.1f}s" if task.finished_at else "运行中"

        out_lines = [
            f"ℹ️ **任务详情** [`{task.id}`]\n",
            f"• **状态**: **{st_text}**",
            f"• **类型**: {task.type}",
            f"• **目标**: `{task.target}`",
            f"• **源路径**: `{task.resolved_path}`",
            f"• **提交时间**: {created}",
            f"• **耗时**: {duration}",
        ]
        if task.output_path:
            out_lines.append(f"• **输出**: `{task.output_path}`")
        if task.error:
            out_lines.append(f"• **错误**:\n> {task.error}")

        return "\n".join(out_lines)
