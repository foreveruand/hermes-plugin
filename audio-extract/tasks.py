"""Background task queue, execution, persistence, and dedicated out-of-band delivery notifications."""

from __future__ import annotations

import html
import json
import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    from .config import DEFAULT_CONFIG, HERMES_PROFILE_NAME
    from .core import cleanup_record_source, clip_video_file, extract_audio_file, refresh_index
except ImportError:
    from config import DEFAULT_CONFIG, HERMES_PROFILE_NAME
    from core import cleanup_record_source, clip_video_file, extract_audio_file, refresh_index

logger = logging.getLogger("audio-extract")

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
    type: str  # clip, extract, refresh-index
    status: str  # pending, running, completed, failed
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    target: str = ""
    resolved_path: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    output_path: Optional[str] = None
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
        task_file = p / f"{task.id}.json"
        with open(task_file, "w", encoding="utf-8") as f:
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
    files = sorted(p.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[:limit]:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                d = json.load(fp)
            d.pop("duration", None)
            tasks.append(Task(**d))
        except Exception:
            continue
    return tasks


def queue_task_notification(task: Task) -> None:
    """Format and enqueue completion/failure notification into the notification queue."""
    if not task.delivery_target:
        return

    esc_id = html.escape(task.id)
    target_name = Path(task.resolved_path).name if task.resolved_path else task.target
    esc_target = html.escape(target_name)
    duration_str = f"{task.duration_seconds():.1f}s"

    if task.type == "clip":
        if task.status == "completed":
            esc_out = html.escape(str(task.output_path or ""))
            p = task.params
            interval_info = f"{p.get('start', '')} - {p.get('end', '')}"
            deleted_count = len(p.get("deleted_files") or [])
            cleanup_line = (
                f"\n• <b>已清理</b>: <code>{deleted_count} 个源文件/关联文件</code>"
                if deleted_count
                else ""
            )
            msg = (
                f"🎬 <b>视频剪辑完成</b>\n\n"
                f"• <b>任务 ID</b>: <code>{esc_id}</code>\n"
                f"• <b>文件</b>: <code>{esc_target}</code>\n"
                f"• <b>片段</b>: <code>{html.escape(interval_info)}</code>\n"
                f"• <b>耗时</b>: <code>{duration_str}</code>{cleanup_line}\n"
                f"• <b>输出路径</b>:\n"
                f"<code>{esc_out}</code>"
            )
        else:
            err_msg = task.error or "未知错误"
            msg = (
                f"❌ <b>视频剪辑失败</b>\n\n"
                f"• <b>任务 ID</b>: <code>{esc_id}</code>\n"
                f"• <b>文件</b>: <code>{esc_target}</code>\n"
                f"• <b>耗时</b>: <code>{duration_str}</code>\n"
                f"• <b>错误信息</b>:\n"
                f"<blockquote expandable>{html.escape(err_msg)}</blockquote>"
            )
    elif task.type == "extract":
        p = task.params
        if task.status == "completed":
            esc_out = html.escape(str(task.output_path or ""))
            fmt = html.escape(p.get("format", "mp3"))
            if p.get("batch"):
                ok = p.get("success_count", 0)
                fail = p.get("fail_count", 0)
                fail_line = ""
                if fail:
                    reasons = "; ".join(p.get("failures", [])[:2])
                    fail_line = f"\n• <b>失败</b>: <code>{fail} 个</code> · <code>{html.escape(reasons)}</code>"
                msg = (
                    f"🎵 <b>音频提取完成</b> ({ok}/{ok + fail})\n\n"
                    f"• <b>任务 ID</b>: <code>{esc_id}</code>\n"
                    f"• <b>来源</b>: <code>{esc_target}</code>\n"
                    f"• <b>格式</b>: <code>{fmt}</code>\n"
                    f"• <b>耗时</b>: <code>{duration_str}</code>{fail_line}\n"
                    f"• <b>输出目录</b>:\n"
                    f"<code>{esc_out}</code>"
                )
            else:
                msg = (
                    f"🎵 <b>音频提取完成</b>\n\n"
                    f"• <b>任务 ID</b>: <code>{esc_id}</code>\n"
                    f"• <b>原文件</b>: <code>{esc_target}</code>\n"
                    f"• <b>格式</b>: <code>{fmt}</code>\n"
                    f"• <b>耗时</b>: <code>{duration_str}</code>\n"
                    f"• <b>输出路径</b>:\n"
                    f"<code>{esc_out}</code>"
                )
        else:
            err_msg = task.error or "未知错误"
            msg = (
                f"❌ <b>音频提取失败</b>\n\n"
                f"• <b>任务 ID</b>: <code>{esc_id}</code>\n"
                f"• <b>原文件</b>: <code>{esc_target}</code>\n"
                f"• <b>错误信息</b>:\n"
                f"<blockquote expandable>{html.escape(err_msg)}</blockquote>"
            )
    else:
        status_text = "完成" if task.status == "completed" else "失败"
        msg = f"⚙️ 任务 <code>{esc_id}</code> ({task.type}) 处理{status_text} (耗时: {duration_str})。"

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
        try:
            _dispatch_notification(target, text)
        finally:
            _NOTIFICATION_QUEUE.task_done()


def _dispatch_notification(target: str, text: str) -> None:
    """Deliver one notification, retrying a few times. Blocking."""
    cmd = _build_send_command(_resolve_send_profile(), target, text)
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
            return
        if res.returncode == 0:
            logger.info("hermes send delivered successfully to %s", target)
            return
        logger.warning("hermes send failed (%d) on attempt %d/%d: %s",
                       res.returncode, attempt, _SEND_ATTEMPTS, res.stderr)


def _run_task_body(task: Task, cfg: dict[str, Any]) -> None:
    """Execute a task in place. Raises on failure."""
    if task.type == "clip":
        p = task.params
        in_path = Path(task.resolved_path)
        out_path = Path(p["out"]) if p.get("out") else None
        out_file, log = clip_video_file(
            input_file=in_path,
            start_time=p["start"],
            end_time=p["end"],
            output_path=out_path,
            reencode=bool(p.get("reencode", False)),
        )
        task.output_path = str(out_file)
        task.log = log

        # Record-dir clips publish into upload/, so the source recording and its Emby
        # sidecars are removed afterwards. clip_video_file only returns after the
        # staging file was renamed, but verify the output again before deleting: the
        # source is unrecoverable once removed.
        if p.get("cleanup_source"):
            if not out_file.is_file() or out_file.stat().st_size == 0:
                raise RuntimeError(f"Clipped output missing or empty, keeping source: {out_file}")
            deleted = cleanup_record_source(in_path, cfg, keep=out_file)
            p["deleted_files"] = [str(x) for x in deleted]
            if deleted:
                task.log = log + "\nDeleted:\n" + "\n".join(str(x) for x in deleted)

    elif task.type == "extract":
        p = task.params
        inputs = [Path(x) for x in p.get("inputs", [])] or [Path(task.resolved_path)]
        out_dir = Path(p["out_dir"]) if p.get("out_dir") else Path(cfg["out_dir"])
        jobs_dir = None if p.get("no_job") else Path(cfg["jobs_dir"])

        outputs: list[str] = []
        failures: list[str] = []
        for in_path in inputs:
            logger.info("Extracting %s", in_path)
            try:
                out_file, log = extract_audio_file(
                    input_file=in_path,
                    out_dir=out_dir,
                    jobs_dir=jobs_dir,
                    format=p.get("format", "mp3"),
                )
                outputs.append(str(out_file))
            except Exception as e:
                logger.error("Extraction failed for %s: %s", in_path, e)
                failures.append(f"{in_path.name}: {e}")

        if failures and not outputs:
            raise RuntimeError("; ".join(failures))

        p["success_count"] = len(outputs)
        p["fail_count"] = len(failures)
        p["failures"] = failures
        task.output_path = outputs[0] if len(outputs) == 1 else str(out_dir)
        task.log = "\n".join(outputs + [f"FAILED {f}" for f in failures])

    elif task.type == "refresh-index":
        res = refresh_index(cfg, incremental=not bool(task.params.get("full", False)))
        task.log = f"Index updated: {res}"

    else:
        raise ValueError(f"Unknown task type: {task.type}")


def execute_task(task: Task, cfg: dict[str, Any], tasks_dir: str | Path) -> Task:
    """Run ``task`` synchronously, persisting status before and after."""
    tasks_dir = Path(tasks_dir)
    task.status = "running"
    task.started_at = time.time()
    save_task(task, tasks_dir)

    try:
        _run_task_body(task, cfg)
        task.status = "completed"
    except Exception as e:
        logger.error("Task %s failed: %s", task.id, e, exc_info=True)
        task.status = "failed"
        task.error = str(e)
        task.log += f"\nError: {e}"
    finally:
        task.finished_at = time.time()
        save_task(task, tasks_dir)
    return task


def run_task_now(task: Task, cfg: dict[str, Any]) -> Task:
    """Run a task in the calling process and deliver its notification inline.

    Used by the scan cron, whose process owns the whole extraction: the agent
    process lifetime is too short to host a background worker.
    """
    execute_task(task, cfg, cfg.get("tasks_dir", DEFAULT_CONFIG["tasks_dir"]))

    if task.delivery_target:
        try:
            queue_task_notification(task)
            while True:
                try:
                    target, text = _NOTIFICATION_QUEUE.get_nowait()
                except queue.Empty:
                    break
                try:
                    _dispatch_notification(target, text)
                finally:
                    _NOTIFICATION_QUEUE.task_done()
        except Exception as exc:
            logger.error("Failed to deliver notification for task %s: %s", task.id, exc)
    return task


def _worker_loop(generation: int, cfg: dict[str, Any]) -> None:
    """Dedicated background queue worker for interactive tasks."""
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

        execute_task(task, cfg, tasks_dir)
        _TASK_QUEUE.task_done()

        try:
            queue_task_notification(task)
        except Exception as e:
            logger.error("Failed to queue notification for task %s: %s", task.id, e)


def submit_task(
    task_type: str,
    target: str,
    resolved_path: str,
    params: dict[str, Any],
    delivery_target: Optional[str] = None,
    cfg: Optional[dict[str, Any]] = None,
    enqueue: bool = True,
) -> Task:
    """Create and persist a task; enqueue it for the background worker by default.

    ``enqueue=False`` only records the task so the caller can run it inline
    (the scan cron does this).
    """
    actual_cfg = cfg or _GLOBAL_CFG
    tasks_dir = Path(actual_cfg.get("tasks_dir", DEFAULT_CONFIG["tasks_dir"]))
    task_id = f"{task_type}_{int(time.time())}_{os.urandom(3).hex()}"

    task = Task(
        id=task_id,
        type=task_type,
        status="pending",
        target=target,
        resolved_path=resolved_path,
        params=params,
        delivery_target=delivery_target,
    )

    save_task(task, tasks_dir)
    if enqueue:
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

        t = threading.Thread(target=_worker_loop, args=(gen, _GLOBAL_CFG), daemon=True, name="audio-extract-worker")
        t.start()
        _ACTIVE_WORKER = t

        t_notif = threading.Thread(target=_notification_worker_loop, args=(gen,), daemon=True, name="audio-extract-notifier")
        t_notif.start()
        _NOTIFICATION_WORKER = t_notif

        logger.info("Audio extract background workers started (generation %d)", gen)


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

        logger.info("Audio extract background workers stopped")
