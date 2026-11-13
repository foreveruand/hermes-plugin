from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
# The plugin is self-contained; targets.json ships under references/.
BUNDLED_TARGETS = PLUGIN / "references" / "targets.json"
# Profile root: explicit override, else infer from the installed plugin's
# location (<profile>/plugins/<plugin>/tests -> parents[1]).  No host-specific
# profile name is baked into the plugin repo.
PROFILE = Path(os.environ.get("NIKKE_PROFILE_DIR") or PLUGIN.parents[1])

sys.path.insert(0, str(PLUGIN / "dashboard"))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    progress = PROFILE / "data" / "nikke" / "player_progress.json"
    if not progress.is_file():
        pytest.skip("nikke-tracker progress data is required; set NIKKE_PROFILE_DIR")
    shutil.copy(progress, data / "player_progress.json")
    targets = tmp_path / "targets.json"
    shutil.copy(BUNDLED_TARGETS, targets)
    monkeypatch.setenv("NIKKE_DATA_DIR", str(data))
    monkeypatch.setenv("NIKKE_VAULT_PATH", str(vault))
    monkeypatch.setenv("NIKKE_TARGETS_PATH", str(targets))
    monkeypatch.setenv("NIKKE_PROFILE_DIR", str(PROFILE))
    # Each test gets a fresh targets path; make sure the module cache never
    # leaks a doc from a previous test's temporary directory.
    import nikke_core as core

    core.modules().db.invalidate_targets_cache()
    return {"data": data, "vault": vault, "targets": targets, "plugin": PLUGIN}
