"""Configuration loader for VibeGuard plugin."""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("vibeguard")

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "placeholder_prefix": "__VG_",
    # Empty means <profile>/data/vibeguard/mapping.json
    "mapping": "",
    "patterns": {
        "keywords": [],
        "regex": [
            {"pattern": r"sk-[A-Za-z0-9]{48}", "category": "CREDENTIAL"},
            {"pattern": r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}", "category": "CREDENTIAL"},
            {"pattern": r"AKIA[0-9A-Z]{16}", "category": "CREDENTIAL"},
        ],
        "builtin": ["china_id", "china_phone", "email"],
        "exclude": ["example.com", "localhost", "127.0.0.1", "0.0.0.0"],
    },
    "topology": {"enabled": True, "glob": [], "archive": ""},
}


def hermes_home() -> Path:
    """Hermes home, with a safe fallback for tests and standalone CLI use."""
    env = os.getenv("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def profile_root() -> Path | None:
    """The profile this copy of the plugin belongs to: <profile>/plugins/vibeguard/..."""
    root = Path(__file__).resolve().parents[2]
    if (root / "distribution.yaml").is_file() or (root / "vibeguard.json").is_file():
        return root
    return None


def get_config_path() -> Path:
    env_path = os.getenv("HERMES_VIBEGUARD_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    root = profile_root()
    if root is not None:
        return root / "vibeguard.json"
    return hermes_home() / "vibeguard.json"


def load_config() -> Dict[str, Any]:
    cfg_path = get_config_path()
    config = copy.deepcopy(DEFAULT_CONFIG)
    if cfg_path.is_file():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("Failed to load VibeGuard config from %s: %s", cfg_path, e)
            data = {}
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, dict) and isinstance(config.get(key), dict):
                    config[key].update(value)
                else:
                    config[key] = value
    else:
        logger.info("VibeGuard config not found at %s; using defaults", cfg_path)
    config["base_dir"] = str(cfg_path.parent)
    return config


def mapping_path(config: Dict[str, Any]) -> Path:
    raw = str(config.get("mapping") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else Path(config["base_dir"]) / path
    return Path(config["base_dir"]) / "data" / "vibeguard" / "mapping.json"
