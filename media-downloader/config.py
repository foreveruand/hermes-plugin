"""Configuration management for Hermes Media Downloader plugin.

Defaults are intentionally portable: every path is derived from the environment
(``HOME``, ``XDG_CONFIG_HOME``, ``TMPDIR``, ``HERMES_MEDIA_ROOT``) or from the plugin's
own install location under the owning profile, so the plugin loads cleanly on any host
and in any profile. Host-specific locations (NFS archive roots,
fast scratch mounts, browser profiles) belong in ``config.json``, which is gitignored
and never shipped with the plugin.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("media-downloader")


def resolve_hermes_profile_dir(ctx: Any = None) -> Path:
    """Resolve the Hermes profile home that owns this plugin.

    The plugin is installed at ``<profile>/plugins/<plugin>/``, so its own location is the
    most reliable signal: in a multi-profile host (gateway/dashboard) the process-wide
    ``HERMES_HOME`` may point at the root ``~/.hermes`` while this plugin belongs to a
    named profile, which would send host config lookups to the wrong ``data/`` tree.
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

DATA_DIR = HERMES_PROFILE_DIR / "data" / "media-downloader"

# Plugin state lives under the Hermes home data area so `hermes backup` captures it
# (the backup zips the whole Hermes home; anything under ~/.config would be missed).
XDG_CONFIG_ROOT = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
CONFIG_DIR = Path(os.environ.get("HERMES_MEDIA_CONFIG_DIR", DATA_DIR)).expanduser()

# Archive root for downloaded media; override per host via HERMES_MEDIA_ROOT or config.json.
MEDIA_ROOT = Path(os.environ.get("HERMES_MEDIA_ROOT", Path.home() / "Media")).expanduser()

# Browser profile used for cookie extraction (empty string disables browser cookies).
CHROMIUM_PROFILE = Path(
    os.environ.get("HERMES_CHROMIUM_PROFILE", XDG_CONFIG_ROOT / "google-chrome-for-testing" / "Profile1")
).expanduser()

DEFAULT_CONFIG: dict[str, Any] = {
    "config_dir": str(CONFIG_DIR),
    "cookies_dir": str(CONFIG_DIR / "cookies"),
    "yt_archive_file": str(CONFIG_DIR / "archive.txt"),
    "gdl_archive_file": str(CONFIG_DIR / "archive-gallery.txt"),
    "gdl_config_file": str(CONFIG_DIR / "gallery-dl.conf"),
    "chromium_profile_path": str(CHROMIUM_PROFILE),
    "chromium_cookie_browser": f"chromium:{CHROMIUM_PROFILE}",
    "staging_base": str(Path(tempfile.gettempdir()) / "hermes-media-downloads"),
    "staging_fallback": str(HERMES_PROFILE_DIR / "cache" / "scratch" / "downloads"),
    "default_proxy": os.environ.get("HERMES_MEDIA_PROXY", ""),
    "domestic_domains": [
        "bilibili.com",
        "b23.tv",
        "douyin.com",
        "weibo.com",
        "weibo.cn",
        "kuaishou.com",
        "ixigua.com",
        "youku.com",
        "qq.com",
    ],
    "destinations": {
        "asmr": str(MEDIA_ROOT / "ASMR"),
        "asmr_r": str(MEDIA_ROOT / "ASMR_R"),
        "bilibili": str(MEDIA_ROOT / "bilibili"),
        "pixiv": str(MEDIA_ROOT / "pixiv"),
        "extract": str(MEDIA_ROOT / "extract"),
    },
    # ASMR.ONE download pipeline (RJ codes). Keeps the plugin's shared proxy, staging
    # and destination configuration so ASMR works are archived like any other media.
    "asmr": {
        "api_base": os.environ.get("HERMES_ASMR_API_BASE", "https://api.asmr-200.com"),
        "hq_only": True,
        "file_types": ["audio", "image", "text"],
        "blacklist": [
            "无SE",
            "SE無",
            "无音效",
            "効果音無し",
            "なし",
            "音效cut",
            "体験版",
        ],
        "max_concurrent_downloads": 3,
        "request_timeout_seconds": 30,
        "download_attempts": 3,
        "dvtag_timeout_seconds": 3600,
    },
    "bin_fallback_paths": [str(Path.home() / ".local" / "bin"), "/usr/local/bin"],
    # Upload backend selected after a download completes: "local" (move into a local
    # directory), "rclone" (transfer a remote spec), or "bdpan" (pack into an encrypted
    # 7z and upload to Baidu Netdisk). The three modes are mutually exclusive.
    "upload_mode": "local",
    # Remote transfer used when upload_mode == "rclone" (destinations hold remote specs).
    "rclone": {
        "bin": "rclone",
        "mode": "move",
        "exclude": ["*.part", "*.ytdl"],
        "flags": [],
        "stats_interval": "2s",
        "timeout_seconds": 7200,
    },
    # Baidu Netdisk transfer used when upload_mode == "bdpan". Uploads are scoped under
    # /apps/bdpan/; remote_dir is relative to that root (empty = application root).
    # Archives are split into fixed-size volumes (7z -v) and uploaded one volume at a
    # time so a broken transfer retries a single part instead of the whole package.
    # The outer filename is deliberately neutral ({prefix}_{YYYYMMDD}_{token}.7z):
    # titles and circle names stay inside the encrypted payload, out of netdisk
    # keyword review. Set obfuscate_filename to false to restore the readable slug.
    "bdpan": {
        "bin": "bdpan",
        "remote_dir": "",
        "seven_zip_bin": "7z",
        "password_length": 20,
        "archive_suffix_len": 6,
        "archive_name_prefix": "pkg",
        "obfuscate_filename": True,
        "volume_size": "2g",
        "upload_retries": 3,
        "compress_level": 9,
        "seven_zip_flags": [],
        "agentname": "",
        "timeout_seconds": 7200,
    },
    "tasks_dir": str(DATA_DIR / "tasks"),
}


def _merge_section(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge overrides into a config section, replacing it when the value is not a mapping."""
    merged = base.copy()
    if not isinstance(overrides, dict):
        return merged
    for key, val in overrides.items():
        current = merged.get(key)
        if isinstance(val, dict) and isinstance(current, dict):
            merged[key] = {**current, **val}
        else:
            merged[key] = val
    return merged


def load_config(ctx: Any = None) -> dict[str, Any]:
    """Load configuration with cascaded fallbacks and section-wise merging."""
    profile_dir = resolve_hermes_profile_dir(ctx)
    config_dir = Path(
        os.environ.get("HERMES_MEDIA_CONFIG_DIR", profile_dir / "data" / "media-downloader")
    ).expanduser()

    cfg = DEFAULT_CONFIG.copy()
    cfg["config_dir"] = str(config_dir)
    cfg["tasks_dir"] = str(config_dir / "tasks")

    # 1. Host config file under the owning profile's data area (gitignored, covered by backup).
    #    Merge section-wise: a user file that overrides one key of a nested section (e.g. a
    #    single destination) must not drop the other defaults.
    host_cfg_path = config_dir / "config.json"
    if host_cfg_path.is_file():
        try:
            with open(host_cfg_path, "r", encoding="utf-8-sig") as f:
                overrides = json.load(f)
            if isinstance(overrides, dict):
                cfg = _merge_section(cfg, overrides)
        except Exception as e:
            logger.warning("Failed to load plugin config from %s: %s", host_cfg_path, e)

    # 2. Context overrides provided by Hermes plugin manager
    if ctx is not None and hasattr(ctx, "get_config"):
        for key in DEFAULT_CONFIG:
            val = ctx.get_config(key, None)
            if val is not None:
                cfg[key] = _merge_section(cfg.get(key, {}), val) \
                    if isinstance(cfg.get(key), dict) else val

    # 3. Derive cookie browser spec from the configured profile path
    profile_path = str(cfg.get("chromium_profile_path", "")).strip()
    cfg["chromium_cookie_browser"] = f"chromium:{profile_path}" if profile_path else ""

    # 4. Ensure working directories exist
    try:
        Path(cfg["tasks_dir"]).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("Failed to create tasks directory for media-downloader: %s", e)

    return cfg
