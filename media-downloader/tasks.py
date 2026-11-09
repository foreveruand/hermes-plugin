"""Background task queue, execution, persistence, and out-of-band completion briefings."""

from __future__ import annotations

import html
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    from .config import DEFAULT_CONFIG, HERMES_PROFILE_NAME
    from .core import format_archived_paths, is_asmr_target, run_download_target
    from .handoff import run_import_target
except ImportError:
    from config import DEFAULT_CONFIG, HERMES_PROFILE_NAME
    from core import format_archived_paths, is_asmr_target, run_download_target
    from handoff import run_import_target

logger = logging.getLogger("media-downloader")

# `hermes send` boots the full CLI on every call (~15-40s here, longer under load), so a
# tight timeout silently kills a slow-but-successful delivery and loses the briefing.
_SEND_TIMEOUT_SECONDS = max(1, int(os.environ.get("HERMES_NOTIFY_SEND_TIMEOUT", "180")))
_SEND_ATTEMPTS = max(1, int(os.environ.get("HERMES_NOTIFY_SEND_ATTEMPTS", "2")))


def _resolve_send_profile() -> str:
    """Profile whose credentials deliver the briefing.

    The gateway does not export ``HERMES_PROFILE``, and ``HERMES_HOME`` may name the root
    rather than the owning profile, so fall back to the profile resolved from this plugin's
    install location.
    """
    return os.environ.get("HERMES_PROFILE") or HERMES_PROFILE_NAME or "default"


def _build_send_command(profile: str, target: str, text: str) -> list[str]:
    """Build the ``hermes send`` argv for a completion briefing."""
    return ["hermes", "--profile", profile, "send", "--to", target, text]

_TASK_QUEUE: queue.Queue[Task] = queue.Queue()
_NOTIFICATION_QUEUE: queue.Queue[tuple[str, str]] = queue.Queue()

_ACTIVE_WORKER: Optional[threading.Thread] = None
_NOTIFICATION_WORKER: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_WORKER_GENERATION = 0
_LIFECYCLE_LOCK = threading.Lock()
_GLOBAL_CFG: dict[str, Any] = {}


@dataclass
class Task:
    id: str
    type: str  # download, import
    status: str  # pending, running, completed, failed
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    target: str = ""
    resolved_path: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    output_path: Optional[str] = None
    files: list[str] = field(default_factory=list)
    transfer: dict[str, Any] = field(default_factory=dict)
    progress: str = ""
    delivery_target: Optional[str] = None
    error: Optional[str] = None
    log: str = ""

    def duration_seconds(self) -> float:
        if self.started_at and self.finished_at:
            return max(0.0, self.finished_at - self.started_at)
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["duration"] = self.duration_seconds()
        return d


def get_originating_delivery_target() -> str:
    """Extract originating delivery target from session context variables."""
    platform = ""
    chat_id = ""
    thread_id = ""

    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "")
    except Exception:
        pass

    if not platform:
        platform = os.environ.get("HERMES_SESSION_PLATFORM", "") or os.environ.get("HERMES_PLATFORM", "telegram")
    if not chat_id:
        chat_id = os.environ.get("HERMES_SESSION_CHAT_ID", "")
    if not thread_id:
        thread_id = os.environ.get("HERMES_SESSION_THREAD_ID", "")

    platform = platform.strip().lower() or "telegram"
    chat_id = chat_id.strip()
    thread_id = thread_id.strip()

    if chat_id:
        if thread_id and thread_id != "0":
            return f"{platform}:{chat_id}:{thread_id}"
        return f"{platform}:{chat_id}"

    return ""


def save_task(task: Task, tasks_dir: str | Path) -> None:
    """Save task metadata to JSON file."""
    try:
        p = Path(tasks_dir)
        p.mkdir(parents=True, exist_ok=True)
        with open(p / f"{task.id}.json", "w", encoding="utf-8") as f:
            json.dump(task.to_dict(), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Failed to save task %s: %s", task.id, e)


def load_task(task_id: str, tasks_dir: str | Path) -> Optional[Task]:
    """Load task from JSON file."""
    try:
        p = Path(tasks_dir) / f"{task_id}.json"
        if not p.is_file():
            return None
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        d.pop("duration", None)
        return Task(**d)
    except Exception as e:
        logger.error("Failed to load task %s: %s", task_id, e)
        return None


def list_recent_tasks(tasks_dir: str | Path, limit: int = 10) -> list[Task]:
    """List recent tasks ordered newest first."""
    p = Path(tasks_dir)
    if not p.exists():
        return []
    tasks: list[Task] = []
    for f in sorted(p.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                d = json.load(fp)
            d.pop("duration", None)
            tasks.append(Task(**d))
        except Exception:
            continue
    return tasks


def bdpan_return_url(transfer: dict[str, Any]) -> str:
    """Resolve the bdpan view link from a transfer summary (return_url, else markdown)."""
    url = str(transfer.get("return_url") or "").strip()
    if url:
        return url
    markdown = str(transfer.get("return_markdown") or "")
    m = re.search(r"\]\((https?://[^)\s]+)\)", markdown)
    return m.group(1) if m else ""


def _format_size(size: Any) -> str:
    """Render a byte count as a compact human-readable size (e.g. ``4.2 GB``)."""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return ""


def _format_bdpan_completion(transfer: dict[str, Any]) -> str:
    """Render the bdpan-specific completion details (HTML)."""
    saved = html.escape(str(transfer.get("saved_path") or transfer.get("remote_path") or ""))
    name = html.escape(str(transfer.get("archive_name") or ""))
    password = html.escape(str(transfer.get("password") or ""))
    structure = html.escape(str(transfer.get("internal_structure") or ""))
    volumes = [str(v) for v in (transfer.get("volumes") or []) if str(v).strip()]
    lines = [f"• <b>网盘路径</b>: <code>{saved}</code>"]
    url = bdpan_return_url(transfer)
    if url:
        lines.append(f'<a href="{html.escape(url, quote=True)}">点击查看</a>')
    if name:
        lines.append(f"• <b>归档文件</b>: <code>{name}</code>")
    if len(volumes) > 1:
        lines.append(f"• <b>分卷数量</b>: {len(volumes)} 卷")
        size = _format_size(transfer.get("archive_size"))
        if size:
            lines.append(f"• <b>归档大小</b>: {size}")
    lines.append(f"- <b>解压密码</b>: <code>{password}</code>")
    lines.append("- <b>加密特性</b>: 采用 7z AES-256 并开启了文件名/头部加密（-mhe=on），在网盘端无法被审查感知。")
    if structure:
        lines.append(f"- <b>内部结构</b>: 包内已构建好完整的目录规范：<code>{structure}</code>，便于远端进行入库处理")
    manifest = str(transfer.get("manifest") or "").strip()
    if manifest:
        lines.append("• <b>一键入库</b>: 在目标服务器发送")
        lines.append(f"<pre>/dl {html.escape(manifest)}</pre>")
    return "\n".join(lines) + "\n"


def _format_import_notification(task: Task) -> str:
    """Render the completion/failure briefing for a handoff import task."""
    esc_id = html.escape(task.id)
    duration_str = f"{task.duration_seconds():.1f}s"
    transfer = task.transfer or {}

    if task.status != "completed":
        err_msg = task.error or "未知错误"
        return (
            f"❌ <b>媒体入库失败</b>\n\n"
            f"• <b>任务 ID</b>: <code>{esc_id}</code>\n"
            f"• <b>归档包</b>: <code>{html.escape(str(transfer.get('archive_name') or task.params.get('archive') or ''))}</code>\n"
            f"• <b>耗时</b>: <code>{duration_str}</code>\n"
            f"• <b>错误信息</b>:\n"
            f"<blockquote expandable>{html.escape(err_msg)}</blockquote>"
        )

    category = html.escape(str(task.resolved_path or task.params.get("category") or ""))
    target = html.escape(str(task.output_path or ""))
    archive = html.escape(str(transfer.get("archive_name") or task.params.get("archive") or ""))
    return (
        f"📦 <b>媒体入库完成</b>\n\n"
        f"• <b>任务 ID</b>: <code>{esc_id}</code>\n"
        f"• <b>归档包</b>: <code>{archive}</code>\n"
        f"• <b>入库分类</b>: <code>{category}</code>\n"
        f"• <b>入库目录</b>: <code>{target}</code>\n"
        f"• <b>文件</b>: 新增 <code>{transfer.get('moved', 0)}</code> / 跳过 <code>{transfer.get('skipped', 0)}</code>\n"
        f"• <b>耗时</b>: <code>{duration_str}</code>"
    )


def queue_task_notification(task: Task) -> None:
    """Format and enqueue the completion/failure briefing into the notification queue."""
    if not task.delivery_target:
        return

    if task.type == "import":
        _NOTIFICATION_QUEUE.put((task.delivery_target, _format_import_notification(task)))
        logger.info("Queued import notification for task %s to %s", task.id, task.delivery_target)
        return

    esc_id = html.escape(task.id)
    esc_url = html.escape(task.target)
    duration_str = f"{task.duration_seconds():.1f}s"
    p = task.params

    if task.status == "completed":
        category = html.escape(str(task.resolved_path or p.get("category", "")))
        transfer = task.transfer or {}
        msg = (
            f"📥 <b>媒体下载完成</b>\n\n"
            f"• <b>任务 ID</b>: <code>{esc_id}</code>\n"
            f"• <b>源链接</b>: {esc_url}\n"
            f"• <b>归档分类</b>: <code>{category}</code>\n"
        )
        if transfer.get("engine") == "bdpan":
            msg += f"• <b>传输</b>: 成功 <code>{transfer.get('moved', 0)}</code>\n"
        elif transfer.get("engine") == "rclone":
            msg += (f"• <b>传输</b>: 成功 <code>{transfer.get('moved', 0)}</code>"
                    f" / 失败 <code>{transfer.get('failed', 0)}</code>\n")
        else:
            msg += f"• <b>文件数</b>: <code>{len(task.files)}</code>\n"
        msg += f"• <b>耗时</b>: <code>{duration_str}</code>\n"
        if transfer.get("engine") == "bdpan":
            msg += _format_bdpan_completion(transfer)
        elif task.files:
            if is_asmr_target(task.target) and task.output_path:
                msg += f"• <b>存盘路径</b>:\n```\n{html.escape(str(task.output_path))}\n```"
            else:
                msg += f"• <b>存盘路径</b>:\n{format_archived_paths(task.files)}"
        else:
            msg += "• <b>说明</b>: 下载流程结束，无新增文件（可能已被归档记录跳过）"
    else:
        err_msg = task.error or "未知错误"
        msg = (
            f"❌ <b>媒体下载失败</b>\n\n"
            f"• <b>任务 ID</b>: <code>{esc_id}</code>\n"
            f"• <b>源链接</b>: {esc_url}\n"
            f"• <b>耗时</b>: <code>{duration_str}</code>\n"
            f"• <b>错误信息</b>:\n"
            f"<blockquote expandable>{html.escape(err_msg)}</blockquote>"
        )

    _NOTIFICATION_QUEUE.put((task.delivery_target, msg))
    logger.info("Queued notification for task %s to %s", task.id, task.delivery_target)


def _notification_worker_loop(generation: int) -> None:
    """Dedicated single-consumer thread for serialized out-of-band delivery."""
    logger.info("Notification worker started (generation %d)", generation)
    while not _STOP_EVENT.is_set():
        with _LIFECYCLE_LOCK:
            if _WORKER_GENERATION != generation:
                return

        try:
            item = _NOTIFICATION_QUEUE.get(timeout=1.0)
        except queue.Empty:
            continue

        target, text = item
        cmd = _build_send_command(_resolve_send_profile(), target, text)

        try:
            for attempt in range(1, _SEND_ATTEMPTS + 1):
                logger.info("Dispatching hermes send to %s (attempt %d/%d)", target, attempt, _SEND_ATTEMPTS)
                try:
                    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, timeout=_SEND_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    logger.warning("hermes send to %s timed out after %ds (attempt %d/%d)",
                                   target, _SEND_TIMEOUT_SECONDS, attempt, _SEND_ATTEMPTS)
                    continue
                except Exception as exc:
                    logger.error("Error executing hermes send: %s", exc)
                    break
                if res.returncode == 0:
                    logger.info("hermes send delivered successfully to %s", target)
                    break
                logger.warning("hermes send failed (%d) on attempt %d/%d: %s",
                               res.returncode, attempt, _SEND_ATTEMPTS, res.stderr)
        finally:
            _NOTIFICATION_QUEUE.task_done()


def _worker_loop(generation: int, cfg: dict[str, Any]) -> None:
    """Dedicated background queue worker processing download tasks."""
    logger.info("Task worker started (generation %d)", generation)
    tasks_dir = cfg.get("tasks_dir", DEFAULT_CONFIG["tasks_dir"])

    while not _STOP_EVENT.is_set():
        with _LIFECYCLE_LOCK:
            if _WORKER_GENERATION != generation:
                logger.info("Task worker superseded (gen %d != %d), exiting", generation, _WORKER_GENERATION)
                return

        try:
            task: Task = _TASK_QUEUE.get(timeout=1.0)
        except queue.Empty:
            continue

        with _LIFECYCLE_LOCK:
            if _WORKER_GENERATION != generation:
                _TASK_QUEUE.task_done()
                return

        task.status = "running"
        task.started_at = time.time()
        save_task(task, tasks_dir)

        last_progress_save = 0.0

        def _on_progress(text: str) -> None:
            """Persist the latest one-line transfer progress (throttled) for /dlstatus."""
            nonlocal last_progress_save
            task.progress = text
            now = time.time()
            if now - last_progress_save >= 2.0:
                last_progress_save = now
                save_task(task, tasks_dir)

        try:
            p = task.params
            if task.type == "import":
                result = run_import_target(
                    code=task.target,
                    cfg=cfg,
                    category=p.get("to", ""),
                    progress_cb=_on_progress,
                )
            else:
                try:
                    from . import core as _core_mod
                    import importlib
                    importlib.reload(_core_mod)
                    _run_dl = _core_mod.run_download_target
                except Exception:
                    _run_dl = run_download_target

                result = _run_dl(
                    target=task.target,
                    cfg=cfg,
                    category=p.get("type", "auto"),
                    author=p.get("author", ""),
                    out_dir=p.get("out", ""),
                    proxy=p.get("proxy", ""),
                    force_proxy=bool(p.get("force_proxy", False)),
                    no_proxy=bool(p.get("no_proxy", False)),
                    extra_args=p.get("extra_args") or [],
                    progress_cb=_on_progress,
                )
            task.files = result["files"]
            task.resolved_path = result["category"]
            task.output_path = result["destination"]
            task.transfer = result.get("transfer") or {}
            task.progress = ""
            task.log = result["log"]
            if not result["files"] and not result["success"]:
                raise RuntimeError("yt-dlp / gallery-dl 均未成功下载该链接")
            task.status = "completed"
        except Exception as e:
            logger.error("Task %s failed: %s", task.id, e, exc_info=True)
            task.status = "failed"
            task.error = str(e)
            task.log += f"\nError: {e}"
        finally:
            task.finished_at = time.time()
            save_task(task, tasks_dir)
            _TASK_QUEUE.task_done()

            try:
                queue_task_notification(task)
            except Exception as e:
                logger.error("Failed to queue notification for task %s: %s", task.id, e)


def submit_task(
    task_type: str,
    target: str,
    params: dict[str, Any],
    delivery_target: Optional[str] = None,
    cfg: Optional[dict[str, Any]] = None,
) -> Task:
    """Create, persist, and enqueue a new task."""
    actual_cfg = cfg or _GLOBAL_CFG
    tasks_dir = actual_cfg.get("tasks_dir", DEFAULT_CONFIG["tasks_dir"])
    task_id = f"{task_type}_{int(time.time())}_{os.urandom(3).hex()}"

    task = Task(
        id=task_id,
        type=task_type,
        status="pending",
        target=target,
        params=params,
        delivery_target=delivery_target,
    )
    save_task(task, tasks_dir)
    _TASK_QUEUE.put(task)
    return task


def start_worker(cfg: dict[str, Any]) -> None:
    """Start background queue worker and notification worker with generation token."""
    global _ACTIVE_WORKER, _NOTIFICATION_WORKER, _WORKER_GENERATION, _GLOBAL_CFG
    _GLOBAL_CFG = cfg.copy()

    stop_worker()

    with _LIFECYCLE_LOCK:
        _STOP_EVENT.clear()
        _WORKER_GENERATION += 1
        gen = _WORKER_GENERATION

        t = threading.Thread(target=_worker_loop, args=(gen, _GLOBAL_CFG), daemon=True,
                             name="media-downloader-worker")
        t.start()
        _ACTIVE_WORKER = t

        t_notif = threading.Thread(target=_notification_worker_loop, args=(gen,), daemon=True,
                                   name="media-downloader-notifier")
        t_notif.start()
        _NOTIFICATION_WORKER = t_notif

        logger.info("Media downloader background workers started (generation %d)", gen)


def stop_worker() -> None:
    """Stop background queue worker and notification worker."""
    global _ACTIVE_WORKER, _NOTIFICATION_WORKER
    _STOP_EVENT.set()
    with _LIFECYCLE_LOCK:
        if _ACTIVE_WORKER and _ACTIVE_WORKER.is_alive():
            _ACTIVE_WORKER.join(timeout=2.0)
        _ACTIVE_WORKER = None

        if _NOTIFICATION_WORKER and _NOTIFICATION_WORKER.is_alive():
            _NOTIFICATION_WORKER.join(timeout=2.0)
        _NOTIFICATION_WORKER = None

        logger.info("Media downloader background workers stopped")
