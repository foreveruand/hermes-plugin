"""qBittorrent management plugin for Hermes.

Runs in-process, so it uses the Hermes dependency environment (``httpx`` is
declared in ``plugin.yaml`` and locked by PM). No host-specific paths: data
lands under ``$HERMES_HOME`` and config comes from the plugin's own ``.env``.
"""

from __future__ import annotations

import logging
from typing import Any

from .commands import register_all_commands

logger = logging.getLogger("qbittorrent-manager")


def register(ctx: Any) -> None:
    """Plugin registration hook invoked by the Hermes plugin manager."""
    register_all_commands(ctx)
    logger.info("qbittorrent-manager plugin registered")
