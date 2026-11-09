"""Tests for profile-scoped config/cache locations.

The plugin's ``.env`` and search cache live in the owning profile's
``data/qbittorrent-manager/`` tree, not in the plugin directory (which a
reinstall can replace) and not in the root ``~/.hermes`` tree when the
plugin belongs to a named profile.

Run directly: ``python3 tests/test_config_paths.py`` (also pytest-compatible).
"""

import os
import sys
import tempfile
import types
from pathlib import Path

# client.py imports httpx at module level; the path logic does not need it.
sys.modules.setdefault("httpx", types.ModuleType("httpx"))

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import client  # noqa: E402


def _fake_install(tmp: str) -> Path:
    """A plugin dir laid out as <profile>/plugins/qbittorrent-manager."""
    plugin_dir = Path(tmp) / "profiles" / "p1" / "plugins" / "qbittorrent-manager"
    plugin_dir.mkdir(parents=True)
    return plugin_dir


def test_data_dir_resolves_to_owning_profile():
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = _fake_install(tmp)
        saved = client.PLUGIN_DIR
        client.PLUGIN_DIR = plugin_dir
        try:
            profile = Path(tmp) / "profiles" / "p1"
            assert client.profile_home() == profile
            assert client.data_dir() == profile / "data" / "qbittorrent-manager"
            assert client.cache_file() == profile / "data" / "qbittorrent-manager" / "search_cache.json"
        finally:
            client.PLUGIN_DIR = saved


def test_legacy_env_is_migrated_and_loaded():
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = _fake_install(tmp)
        (plugin_dir / ".env").write_text(
            "QB_URL=http://qb.example:9000\nQB_RULE=TestRule\n", encoding="utf-8")
        saved = client.PLUGIN_DIR
        client.PLUGIN_DIR = plugin_dir
        saved_env = {k: os.environ.get(k) for k in ("QB_URL", "QB_RULE")}
        os.environ.pop("QB_URL", None)
        os.environ.pop("QB_RULE", None)
        try:
            client._load_plugin_env()
            assert os.environ.get("QB_URL") == "http://qb.example:9000"
            canonical = Path(tmp) / "profiles" / "p1" / "data" / "qbittorrent-manager" / ".env"
            assert canonical.is_file(), "legacy .env was not migrated into the data dir"
        finally:
            client.PLUGIN_DIR = saved
            for key, val in saved_env.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val


if __name__ == "__main__":
    for name in sorted(n for n in globals() if n.startswith("test_")):
        globals()[name]()
        print(f"PASS {name}")
    print("ALL TESTS PASSED")
