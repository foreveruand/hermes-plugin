"""Slash command handlers for Hermes Media Downloader plugin."""

from __future__ import annotations

import logging
import shlex
import time
from typing import Any

try:
    from .config import DEFAULT_CONFIG
    from .core import is_asmr_target, parse_extra_args, resolve_destination
    from .handoff import (
        ImportDestinationRequired,
        decode_manifest,
        is_handoff_code,
        join_destination,
        resolve_import_destination,
        split_handoff,
    )
    from .tasks import (
        bdpan_return_url,
        get_originating_delivery_target,
        list_recent_tasks,
        load_task,
        submit_task,
    )
except ImportError:
    from config import DEFAULT_CONFIG
    from core import is_asmr_target, parse_extra_args, resolve_destination
    from handoff import (
        ImportDestinationRequired,
        decode_manifest,
        is_handoff_code,
        join_destination,
        resolve_import_destination,
        split_handoff,
    )
    from tasks import (
        bdpan_return_url,
        get_originating_delivery_target,
        list_recent_tasks,
        load_task,
        submit_task,
    )

logger = logging.getLogger("media-downloader")

STATUS_LABELS = {
    "pending": "⏳ 队列等待中",
    "running": "⚡ 正在下载",
    "completed": "✅ 已完成",
    "failed": "❌ 下载失败",
}

USAGE = (
    "📥 **/dl 媒体下载 / 入库用法**\n\n"
    "• **下载**: `/dl <URL 或 RJ号> [选项]`\n"
    "  例: `/dl https://www.bilibili.com/video/BV1xxxxxx`\n"
    "  例: `/dl https://x.com/user/status/123 --author 画师名`\n"
    "  例: `/dl RJ123456` (ASMR.ONE，等同 `/asmr RJ123456`)\n\n"
    "• **入库**: `/dl HMD1:<入库指令> [--to 分类|路径|远端]`\n"
    "  从另一台服务器推送的加密归档自动下载、解密解压并入库。\n"
    "  入库指令已携带分类时无需 --to；可覆盖到本机其他分类或绝对目录。\n\n"
    "**可选参数:**\n"
    "• `--type <auto|video|image|asmr>`: 内容分类 (默认 auto)\n"
    "• `--author <名称>`: Pixiv / X 图集的作者子目录\n"
    "• `--out <路径>`: 指定归档目录 (覆盖自动分类，可填本地路径或 rclone 远端 `remote:path`)\n"
    "• `--to <分类|路径|远端>`: 入库目标 (预设分类、任意绝对路径，或 rclone 远端 `remote:path`)\n"
    "• `--proxy <URL>`: 自定义代理\n"
    "• `--no-proxy` / `--force-proxy`: 强制直连 / 强制代理\n"
    "• `--extra \"<参数>\"`: 透传给 yt-dlp / gallery-dl 的附加参数\n\n"
    "*任务将在后台线程执行，完成后自动推送简报，不受超时限制。*"
)

ASMR_USAGE = (
    "🎧 **/asmr ASMR.ONE 下载用法**\n\n"
    "• **用法**: `/asmr <RJ号> [选项]`\n"
    "  例: `/asmr RJ123456`\n\n"
    "**处理流程:**\n"
    "1. 从 ASMR.ONE 拉取作品元数据与音轨\n"
    "2. 下载音轨、封面与文本（默认仅无损）\n"
    "3. 调用 `dvtag` 写入标签并将 WAV 转为 FLAC\n"
    "4. VTT 字幕转 LRC，生成 `album.nfo`\n"
    "5. 整理入库到 `destinations.asmr/<社团>/<作品号>`\n\n"
    "**可选参数:**\n"
    "• `--out <路径>`: 覆盖归档目录\n"
    "• `--proxy <URL>` / `--no-proxy` / `--force-proxy`: 代理控制\n\n"
    "*任务将在后台线程执行，完成后自动推送简报。*"
)


def _submit_download(cfg: dict[str, Any], target: str, category: str, author: str, out_dir: str,
                     proxy: str, no_proxy: bool, force_proxy: bool, extra_args: list[str]) -> str:
    """Resolve the destination, enqueue the task, and render the acceptance reply."""
    try:
        final_dir, label = resolve_destination(target, cfg, category=category, author=author,
                                              out_dir=out_dir)
    except Exception as e:
        return f"❌ 无法解析归档目录: {e}"

    params = {
        "type": category,
        "author": author,
        "out": out_dir,
        "proxy": proxy,
        "no_proxy": no_proxy,
        "force_proxy": force_proxy,
        "extra_args": extra_args,
        "category": label,
    }

    task = submit_task(
        task_type="download",
        target=target,
        params=params,
        delivery_target=get_originating_delivery_target(),
        cfg=cfg,
    )

    return (
        f"⏳ **媒体下载任务已提交后台处理**\n\n"
        f"• **任务 ID**: `{task.id}`\n"
        f"• **目标**: `{target}`\n"
        f"• **归档分类**: `{label}`\n"
        f"• **归档目录**: `{final_dir}`\n\n"
        f"*下载完成后将自动推送简报。可用 `/dlstatus {task.id}` 查询进度。*"
    )


class MediaDownloaderCommands:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    def _handle_import(self, tokens: list[str]) -> str:
        """Handle a handoff code: resolve the local target and enqueue an import task."""
        code = tokens[0]
        override = ""
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("--to", "-d") and i + 1 < len(tokens):
                override = tokens[i + 1]
                i += 2
            elif tok.startswith("--to="):
                override = tok.split("=", 1)[1]
                i += 1
            else:
                return f"❌ 无法识别的参数 `{tok}`。用法: `/dl <入库指令> [--to 分类|路径|远端]`"

        try:
            manifest = decode_manifest(code)
            root, label, sub = resolve_import_destination(manifest, self.cfg, override)
        except ImportDestinationRequired as exc:
            return f"❌ {exc}"
        except Exception as exc:
            return f"❌ 入库指令无效: {exc}"

        target = join_destination(root, sub)
        task = submit_task(
            task_type="import",
            target=code,
            params={"to": override, "category": label,
                    "archive": str(manifest.get("name") or "")},
            delivery_target=get_originating_delivery_target(),
            cfg=self.cfg,
        )
        return (
            f"⏳ **媒体入库任务已提交后台处理**\n\n"
            f"• **任务 ID**: `{task.id}`\n"
            f"• **归档包**: `{manifest.get('name')}`\n"
            f"• **入库分类**: `{label}`\n"
            f"• **入库目录**: `{target}`\n\n"
            f"*将自动下载、解密解压并入库；完成后推送简报。可用 `/dlstatus {task.id}` 查询进度。*"
        )

    def handle_dl(self, raw_args: str) -> str:
        """Handle /dl slash command: submit the download to the background worker."""
        text = (raw_args or "").strip()
        if not text or text in ("-h", "--help", "help"):
            return USAGE

        # Handoff codes may arrive bare, fenced, or with the whole "/dl ..." line.
        handoff_cmd = split_handoff(text)
        if handoff_cmd:
            code, rest = handoff_cmd
            try:
                extra = shlex.split(rest) if rest else []
            except Exception:
                extra = rest.split()
            return self._handle_import([code, *extra])

        try:
            tokens = shlex.split(text)
        except Exception:
            tokens = text.split()

        # Tolerate forwarding the whole command line, then route handoff codes.
        if tokens and tokens[0].lower() in ("dl", "/dl"):
            tokens = tokens[1:]
        if tokens and is_handoff_code(tokens[0]):
            return self._handle_import(tokens)

        url = ""
        category = "auto"
        author = ""
        out_dir = ""
        proxy = ""
        no_proxy = False
        force_proxy = False
        extra_raw = ""

        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("--type", "-t") and i + 1 < len(tokens):
                category = tokens[i + 1]
                i += 2
            elif tok.startswith("--type="):
                category = tok.split("=", 1)[1]
                i += 1
            elif tok in ("--author", "-a") and i + 1 < len(tokens):
                author = tokens[i + 1]
                i += 2
            elif tok.startswith("--author="):
                author = tok.split("=", 1)[1]
                i += 1
            elif tok in ("--out", "-o") and i + 1 < len(tokens):
                out_dir = tokens[i + 1]
                i += 2
            elif tok.startswith("--out="):
                out_dir = tok.split("=", 1)[1]
                i += 1
            elif tok == "--proxy" and i + 1 < len(tokens):
                proxy = tokens[i + 1]
                i += 2
            elif tok.startswith("--proxy="):
                proxy = tok.split("=", 1)[1]
                i += 1
            elif tok == "--extra" and i + 1 < len(tokens):
                extra_raw = tokens[i + 1]
                i += 2
            elif tok.startswith("--extra="):
                extra_raw = tok.split("=", 1)[1]
                i += 1
            elif tok == "--no-proxy":
                no_proxy = True
                i += 1
            elif tok == "--force-proxy":
                force_proxy = True
                i += 1
            elif not url:
                url = tok
                i += 1
            else:
                i += 1

        if not url:
            return "❌ 请提供要下载的媒体 URL 或 RJ 号。输入 `/dl --help` 查看用法。"
        if category not in ("auto", "video", "image", "asmr"):
            return f"❌ 不支持的分类 `{category}`，可选: auto / video / image / asmr。"

        return _submit_download(self.cfg, url, category, author, out_dir, proxy,
                                no_proxy, force_proxy, parse_extra_args(extra_raw))

    def handle_asmr(self, raw_args: str) -> str:
        """Handle /asmr slash command: download an ASMR.ONE work by RJ code."""
        text = (raw_args or "").strip()
        if not text or text in ("-h", "--help", "help"):
            return ASMR_USAGE

        try:
            tokens = shlex.split(text)
        except Exception:
            tokens = text.split()

        target = ""
        out_dir = ""
        proxy = ""
        no_proxy = False
        force_proxy = False

        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("--out", "-o") and i + 1 < len(tokens):
                out_dir = tokens[i + 1]
                i += 2
            elif tok.startswith("--out="):
                out_dir = tok.split("=", 1)[1]
                i += 1
            elif tok == "--proxy" and i + 1 < len(tokens):
                proxy = tokens[i + 1]
                i += 2
            elif tok.startswith("--proxy="):
                proxy = tok.split("=", 1)[1]
                i += 1
            elif tok == "--no-proxy":
                no_proxy = True
                i += 1
            elif tok == "--force-proxy":
                force_proxy = True
                i += 1
            elif not target:
                target = tok
                i += 1
            else:
                i += 1

        if not target:
            return "❌ 请提供作品编号，例如 `/asmr RJ123456`。"
        if not is_asmr_target(target):
            return f"❌ `{target}` 不是有效的作品编号，请使用 RJ 号，例如 `/asmr RJ123456`。"

        return _submit_download(self.cfg, target.strip().upper(), "asmr", "", out_dir, proxy,
                                no_proxy, force_proxy, [])

    def handle_dlstatus(self, raw_args: str) -> str:
        """Handle /dlstatus slash command."""
        task_id = (raw_args or "").strip()
        if not task_id:
            return "❌ 用法: `/dlstatus <任务ID>`"

        tasks_dir = self.cfg.get("tasks_dir", DEFAULT_CONFIG["tasks_dir"])
        task = load_task(task_id, tasks_dir)
        if not task:
            return f"❌ 未找到任务 ID: `{task_id}`"

        st_text = STATUS_LABELS.get(task.status, task.status)
        if task.type == "import" and task.status == "running":
            st_text = "⚡ 正在下载 / 解压入库"
        created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.created_at))
        duration = f"{task.duration_seconds():.1f}s" if task.finished_at else "运行中"

        out_lines = [
            f"ℹ️ **任务详情** [`{task.id}`]\n",
            f"• **状态**: **{st_text}**",
            f"• **目标**: `{task.target}`",
            f"• **归档分类**: `{task.resolved_path or task.params.get('category', '-')}`",
            f"• **提交时间**: {created}",
            f"• **耗时**: {duration}",
        ]
        if task.status == "running" and task.progress:
            out_lines.append(f"• **传输进度**: `{task.progress}`")
        if task.output_path:
            label = "入库目录" if task.type == "import" else "归档目录"
            out_lines.append(f"• **{label}**: `{task.output_path}`")
        transfer = task.transfer or {}
        if transfer.get("engine") == "bdpan":
            saved = transfer.get("saved_path") or transfer.get("remote_path") or "-"
            out_lines.append(f"• **网盘路径**: `{saved}`")
            url = bdpan_return_url(transfer)
            if url:
                out_lines.append(f"• **查看**: [点击查看]({url})")
            if transfer.get("archive_name"):
                out_lines.append(f"• **归档文件**: `{transfer.get('archive_name')}`")
            if transfer.get("password"):
                out_lines.append(f"• **解压密码**: `{transfer.get('password')}`")
            if transfer.get("internal_structure"):
                out_lines.append(f"• **内部结构**: `{transfer.get('internal_structure')}`")
        elif transfer.get("engine") == "bdpan-import":
            out_lines.append(f"• **归档包**: `{transfer.get('archive_name', '-')}`")
            out_lines.append(
                f"• **导入**: 新增 {transfer.get('moved', 0)} / 跳过 {transfer.get('skipped', 0)}"
            )
        elif transfer.get("engine") == "rclone":
            out_lines.append(
                f"• **传输**: 成功 {transfer.get('moved', 0)} / 失败 {transfer.get('failed', 0)}"
            )
        if transfer.get("manifest"):
            out_lines.append(f"• **入库指令**:\n```\n/dl {transfer['manifest']}\n```")
        if task.files:
            out_lines.append(f"• **文件数**: {len(task.files)}")
        if task.error:
            out_lines.append(f"• **错误**:\n> {task.error}")

        return "\n".join(out_lines)

    def handle_dltasks(self, raw_args: str) -> str:
        """Handle /dltasks slash command."""
        tasks_dir = self.cfg.get("tasks_dir", DEFAULT_CONFIG["tasks_dir"])
        tasks = list_recent_tasks(tasks_dir, limit=10)
        if not tasks:
            return "📋 暂无历史下载任务记录。"

        lines = [f"📋 **最近下载任务 (共 {len(tasks)} 条):**\n"]
        for t in tasks:
            st = STATUS_LABELS.get(t.status, t.status)
            created = time.strftime("%H:%M:%S", time.localtime(t.created_at))
            lines.append(
                f"• `{t.id}` [{st}]\n"
                f"  目标: `{t.target[:60]}` | 文件: {len(t.files)} | 提交: {created}"
            )
        return "\n".join(lines)
