"""Regression tests for profile-scoped config resolution and briefing delivery.

Run directly: ``python3 tests/test_config_resolution.py`` (also pytest-compatible).
"""

import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = PLUGIN_ROOT.parent.parent  # <profile>/plugins/<plugin> -> <profile>
EXPECTED_NAME = PROFILE_DIR.name if PROFILE_DIR.parent.name == "profiles" else "default"

sys.path.insert(0, str(PLUGIN_ROOT))

import config  # noqa: E402
import tasks  # noqa: E402


def test_profile_resolution_ignores_root_hermes_home():
    """A host pointed at the root home must not steal this plugin's owning profile."""
    saved = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(Path.home() / ".hermes")
    try:
        assert config.resolve_hermes_profile_dir().resolve() == PROFILE_DIR.resolve()
        assert config.resolve_hermes_profile_name(PROFILE_DIR) == EXPECTED_NAME
    finally:
        if saved is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = saved


def test_load_config_paths_stay_in_profile():
    """Host paths must resolve under the owning profile, not the shared root home."""
    cfg = config.load_config()
    for key in ("db_path", "tasks_dir", "jobs_dir"):
        assert str(PROFILE_DIR) in cfg[key], f"{key}={cfg[key]} not under {PROFILE_DIR}"


def test_send_command_is_portable_and_profile_scoped():
    """The profile must be explicit and the non-portable flag must be gone."""
    cmd = tasks._build_send_command(EXPECTED_NAME, "telegram:1:2", "hi")
    assert cmd == ["hermes", "--profile", EXPECTED_NAME, "send", "--to", "telegram:1:2", "hi"]
    assert "--no-preview" not in cmd
    assert tasks._resolve_send_profile() == EXPECTED_NAME


if __name__ == "__main__":
    for name in sorted(n for n in globals() if n.startswith("test_")):
        globals()[name]()
        print(f"PASS {name}")
    print("ALL TESTS PASSED")
