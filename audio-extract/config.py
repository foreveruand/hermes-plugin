"""Configuration management for Hermes Audio Extract & Video Clip plugin."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("audio-extract")


def resolve_hermes_profile_dir(ctx: Any = None) -> Path:
    """Resolve the Hermes profile home that owns this plugin.

    The plugin is installed at ``<profile>/plugins/<plugin>/``, so its own location is the
    most reliable signal: in a multi-profile host (gateway/dashboard) the process-wide
    ``HERMES_HOME`` may point at the root ``~/.hermes`` while this plugin belongs to a
    named profile, which would send host paths to the wrong ``data/`` tree.
    Prefers, in order: the plugin host context, the physical install location, the
    official Hermes helpers, then ``HERMES_HOME``, then ``~/.hermes``.
    """
    # 1. Plugin host context for the profile that actually loaded us.
    home_path = getattr(getattr(ctx, "_manager", None), "home_path", None)
    if home_path:
        return Path(home_path).resolve()

    # 2. Physical install location: <profile>/plugins/<plugin>/config.py -> parents[2].
    candidate = Path(__file__).resolve().parents[2]
    if candidate.parent.name == "profiles" or (candidate / "config.yaml").exists() or (candidate / "data").exists():
        return candidate

    # 3. Official Hermes helpers (unavailable outside a Hermes install).
    try:
        from hermes_constants import get_hermes_home, named_profile_home

        named = named_profile_home(Path(__file__).resolve())
        if named is not None:
            return named
        return Path(get_hermes_home()).resolve()
    except Exception:
        pass

    # 4. Environment / default fallbacks.
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]).resolve()
    return Path.home() / ".hermes"


def resolve_hermes_profile_name(profile_dir: Path) -> str:
    """Return the canonical profile id owning ``profile_dir`` (``default`` for the root)."""
    try:
        from hermes_constants import profile_name_for_home

        name = profile_name_for_home(profile_dir)
        if name:
            return name
    except Exception:
        pass
    if profile_dir.parent.name == "profiles" and not profile_dir.name.startswith("."):
        return profile_dir.name
    return "default"


HERMES_PROFILE_DIR = resolve_hermes_profile_dir()
HERMES_PROFILE_NAME = resolve_hermes_profile_name(HERMES_PROFILE_DIR)

PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = HERMES_PROFILE_DIR / "data" / "audio-extract"

DEFAULT_CONFIG: dict[str, Any] = {
    "out_dir": "/mnt/media/Temp",
    "scan_dirs": [
        "/mnt/media/MediaServer",
        "/mnt/media/record",
    ],
    "db_path": str(DATA_DIR / "file_index.db"),
    "jobs_dir": str(DATA_DIR / ".jobs"),
    "index_extensions": [
        ".mp4",
        ".mkv",
        ".mov",
        ".wmv",
        ".flv",
        ".webm",
        ".ts",
        ".flac",
    ],
    "tasks_dir": str(DATA_DIR / "tasks"),
    # Livestream recordings: /vclip publishes the clipped segment under
    # <record_dir>/upload and then removes the source plus its Emby sidecars.
    "record_dir": "/mnt/media/record",
    # Behaviour of the periodic index scan (scripts/refresh_media_index.sh):
    # newly indexed media without a subtitle/lyric sidecar is extracted for Whisper
    # once it looks fully written. See core.AUTO_EXTRACT_DEFAULTS.
    "auto_extract": {
        "enabled": True,
        "format": "mp3",
        "settle_seconds": 900,
        "sample_wait_seconds": 2,
        "batch_size": 20,
        "max_per_scan": 200,
        "delivery_target": "",
    },
}


def _load_user_config(cfg: dict[str, Any]) -> None:
    """Overlay the user's ``config.json`` onto ``cfg`` (mutated in place).

    Canonical location: ``DATA_DIR / "config.json"`` — the profile's data tree
    survives plugin reinstalls; the plugin directory does not. A legacy
    ``config.json`` in the plugin directory is still read when the data-dir
    file is absent, and is copied into the data dir so a future reinstall
    cannot take it away.
    """
    data_cfg_path = DATA_DIR / "config.json"
    legacy_cfg_path = PLUGIN_DIR / "config.json"
    source = data_cfg_path if data_cfg_path.is_file() else legacy_cfg_path
    if not source.is_file():
        return
    try:
        with open(source, "r", encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    except Exception as e:
        logger.warning("Failed to load plugin config from %s: %s", source, e)
        return
    if source == legacy_cfg_path:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            data_cfg_path.write_bytes(legacy_cfg_path.read_bytes())
            logger.info("Migrated audio-extract config to %s", data_cfg_path)
        except Exception as e:
            logger.warning("Failed to migrate plugin config to %s: %s", data_cfg_path, e)


def load_config(ctx: Any = None) -> dict[str, Any]:
    """Load configuration with cascaded fallbacks."""
    cfg = DEFAULT_CONFIG.copy()

    # 1. User config file in the profile data dir (legacy plugin-dir copy is
    #    still honoured and migrated on first load).
    _load_user_config(cfg)

    # 2. Context overrides if provided by Hermes plugin manager
    if ctx is not None and hasattr(ctx, "get_config"):
        for key in ("out_dir", "scan_dirs", "db_path", "jobs_dir", "index_extensions", "tasks_dir", "record_dir", "auto_extract"):
            val = ctx.get_config(key, None)
            if val is not None:
                cfg[key] = val

    # Ensure paths exist or are valid directories
    try:
        Path(cfg["tasks_dir"]).mkdir(parents=True, exist_ok=True)
        Path(cfg["jobs_dir"]).mkdir(parents=True, exist_ok=True)
        Path(cfg["db_path"]).parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("Failed to create directories for audio-extract: %s", e)

    return cfg
