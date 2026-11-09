"""
Hermes IMAP IDLE Email Watcher Plugin.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
import sys

# Ensure imapclient is resolvable from any Hermes local environment or tools site-packages
hermes_base = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
for p in glob.glob(str(hermes_base / "**/site-packages"), recursive=True):
    if p not in sys.path:
        sys.path.insert(0, p)

import logging

logger = logging.getLogger("imap-watcher")

# Import the watcher defensively: it pulls in ``imapclient`` at import time. If that
# dependency is missing we still register ``/mailstatus`` so the failure is visible in
# chat instead of silently losing the command (the plugin still shows as "enabled").
_WATCHER_IMPORT_ERROR: Exception | None = None
try:
    from .watcher import get_status_report, start_watcher, stop_watcher
except Exception as _exc:  # noqa: BLE001 - surface any import/dependency failure
    _WATCHER_IMPORT_ERROR = _exc
    logger.error("imap-watcher 依赖加载失败: %s", _exc)

    def _dependency_error_text() -> str:
        return (
            "⚠️ imap-watcher 已启用但无法运行：Python 依赖缺失。\n"
            f"错误: {_WATCHER_IMPORT_ERROR}\n"
            "该插件依赖 `imapclient`。请安装后重启 Hermes：\n"
            "  • 声明式安装: `hermes pm repair`\n"
            "  • 或手动安装到 Hermes 运行时环境: "
            "`<hermes-python> -m pip install imapclient`"
        )

    def get_status_report() -> str:  # type: ignore[misc]
        return _dependency_error_text()

    def start_watcher() -> None:  # type: ignore[misc]
        logger.error("imap-watcher 未启动：%s", _WATCHER_IMPORT_ERROR)

    def stop_watcher() -> None:  # type: ignore[misc]
        pass


def _handle_mailstatus(args: str = "") -> str:
    """Handle /mailstatus slash command."""
    return get_status_report()


def register(ctx) -> None:
    """Plugin registration hook invoked by Hermes plugin manager."""
    # Register slash command (always, so a missing dependency is diagnosable from chat)
    if hasattr(ctx, "register_command"):
        ctx.register_command(
            "mailstatus",
            _handle_mailstatus,
            description="查看邮箱 IMAP 长连接监听运行状态",
        )

    if _WATCHER_IMPORT_ERROR is not None:
        logger.error(
            "imap-watcher is enabled but cannot start: missing Python dependency "
            "imapclient. Install it (hermes pm repair) and restart Hermes."
        )
        return

    # Start watcher background threads directly
    try:
        start_watcher()
    except Exception as exc:
        logger.error(f"Failed to start IMAP watcher: {exc}")

    if hasattr(ctx, "on_unload"):
        ctx.on_unload(lambda: stop_watcher())

    logger.info("Hermes IMAP Watcher plugin loaded successfully.")
