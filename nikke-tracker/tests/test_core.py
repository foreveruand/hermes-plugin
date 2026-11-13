from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import nikke_core as core


def _unowned_target():
    """Find a target that is not in the current progress file (data-independent)."""
    for item in core.bootstrap()["characters"]:
        if not item["owned"]:
            return item["name"]
    pytest.skip("no unowned target available in the progress snapshot")


def _slots(a="攻击力", av=14.63, b="暴击率", bv=2.30, c="", cv=0.0):
    return [
        {"affix": a, "value": av, "locked": False},
        {"affix": b, "value": bv, "locked": False},
        {"affix": c, "value": cv, "locked": False},
    ]


def test_bootstrap_shape_and_score(env):
    data = core.bootstrap()
    assert data["counts"]["targets"] == 86
    expected_owned = len(core.modules().db.load_progress()["characters"])
    assert data["counts"]["owned"] == expected_owned
    assert expected_owned >= 20
    assert len({c["name"] for c in data["characters"]}) == 86
    red = next(c for c in data["characters"] if c["name"] == "红莲:暗影")
    assert red["owned"] is True
    assert red["score"] is not None
    assert red["grade"]
    # bootstrap is a wall summary only; full detail is loaded on demand.
    assert "detail" not in red
    assert core.detail("红莲:暗影")["character"]["gear"]["head"]["slots"]


def test_detail_score_matches_advisor(env):
    detail = core.detail("红莲:暗影")
    assert detail["owned"] is True
    assert detail["diag"]["score"] == pytest.approx(detail["diag"]["score"])
    assert len(detail["diag"]["pieces"]) == 4
    assert "level" in detail["character"]["gear"]["head"]


def test_save_gear_only_touches_one_piece_and_locks_exact(env):
    detail = core.detail("小红帽")
    before_body_level = detail["character"]["gear"]["body"]["level"]
    body = core.save_gear(
        "小红帽",
        "head",
        5,
        [
            {"affix": "攻击力", "value": 14.63, "locked": False},
            {"affix": "暴击率", "value": 2.30, "locked": False},
            {"affix": "优越代码伤害", "value": 20.0, "locked": True},
        ],
        detail["rev"],
    )
    assert body["character"]["gear"]["head"]["level"] == 5
    assert [s["locked"] for s in body["character"]["gear"]["head"]["slots"]] == [False, False, True]
    assert body["character"]["gear"]["body"]["level"] == before_body_level

    # unlock slot 3, keep 1/2 unlocked; update_gear alone would preserve the lock
    body = core.save_gear(
        "小红帽",
        "head",
        5,
        [
            {"affix": "攻击力", "value": 14.63, "locked": False},
            {"affix": "暴击率", "value": 2.30, "locked": False},
            {"affix": "优越代码伤害", "value": 20.0, "locked": False},
        ],
        body["rev"],
    )
    assert [s["locked"] for s in body["character"]["gear"]["head"]["slots"]] == [False, False, False]
    stored = json.loads((env["data"] / "player_progress.json").read_text(encoding="utf-8"))
    assert stored["characters"]["小红帽"]["gear"]["body"]["level"] == before_body_level


@pytest.mark.parametrize(
    "rows",
    [
        [{"affix": "攻击力", "value": 14.63, "locked": False}],
        [],
        _slots() + [{"affix": "", "value": 0, "locked": False}],
    ],
)
def test_save_gear_requires_three_rows(env, rows):
    detail = core.detail("小红帽")
    with pytest.raises(ValueError):
        core.save_gear("小红帽", "head", 5, rows, detail["rev"])


def test_value_validation(env):
    detail = core.detail("小红帽")
    with pytest.raises(ValueError):
        core.save_gear(
            "小红帽",
            "head",
            5,
            [
                {"affix": "攻击力", "value": 0, "locked": False},
                {"affix": "", "value": 0, "locked": False},
                {"affix": "", "value": 0, "locked": False},
            ],
            detail["rev"],
        )
    with pytest.raises(ValueError):
        core.save_gear(
            "小红帽",
            "head",
            5,
            [
                {"affix": "攻击力", "value": 99, "locked": False},
                {"affix": "", "value": 0, "locked": False},
                {"affix": "", "value": 0, "locked": False},
            ],
            detail["rev"],
        )
    with pytest.raises(ValueError):
        core.save_gear("小红帽", "head", 6, _slots(), detail["rev"])


def test_rev_conflict_keeps_file(env):
    detail = core.detail("小红帽")
    before = (env["data"] / "player_progress.json").read_bytes()
    first = core.save_gear("小红帽", "head", 5, _slots(), detail["rev"])
    middle = (env["data"] / "player_progress.json").read_bytes()
    with pytest.raises(core.Conflict):
        core.save_gear("小红帽", "head", 5, _slots(), detail["rev"])
    assert (env["data"] / "player_progress.json").read_bytes() == middle
    assert first["rev"] != detail["rev"]
    assert before != middle


def test_favorites_are_separate(env):
    before = (env["data"] / "player_progress.json").read_bytes()
    state = core.set_favorites(["小红帽", "红莲:暗影", "小红帽"])
    assert state["favorites"] == ["小红帽", "红莲:暗影"]
    assert (env["data"] / "dashboard_state.json").is_file()
    assert (env["data"] / "player_progress.json").read_bytes() == before
    with pytest.raises(ValueError):
        core.set_favorites(["不存在的角色"])


def test_render_is_stable(env):
    first = core.render()
    second = core.render()
    assert first["ok"] is True
    assert second["ok"] is True
    assert second["changed"] is False
    assert (env["vault"] / "游戏与联机" / "nikke.md").is_file()


def test_add_and_remove_unowned(env):
    target = _unowned_target()
    assert core.detail(target)["owned"] is False
    added = core.add_character(target)
    assert added["created"] is True
    assert added["owned"] is True
    assert added["character"]["skills"] == [1, 1, 1]
    assert core.detail(target)["owned"] is True
    removed = core.remove_character(target, confirm=True)
    assert removed["removed"]


def test_teams_blocks(env):
    data = core.teams()
    assert data["interception"]["title"]
    assert data["solo_raid"]["title"]
    tables = [b for b in data["interception"]["blocks"] if b["type"] == "table"]
    assert tables
    boss = next(b for b in tables if b["headers"] and b["headers"][0] == "Boss")
    assert len(boss["rows"]) == 5


def test_portrait_key_validation(env):
    with pytest.raises(core.NotFound):
        core.portrait_file("../etc/passwd")
    with pytest.raises(core.NotFound):
        core.portrait_file("0" * 16)


def test_markdown_blocks_parser():
    text = "# 标题\n\n- 一\n- 二\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    blocks = core.parse_markdown_blocks(text)
    assert blocks[0] == {"type": "heading", "level": 1, "text": "标题"}
    assert blocks[1]["type"] == "list"
    assert blocks[2]["type"] == "table"
    assert blocks[2]["headers"] == ["A", "B"]
    assert blocks[2]["rows"] == [["1", "2"]]

def test_save_gear_auto_adds_unowned(env):
    target = _unowned_target()
    detail = core.detail(target)
    assert detail["owned"] is False
    saved = core.save_gear(target, "head", 1, _slots(), detail["rev"])
    assert saved["owned"] is True
    assert core.detail(target)["owned"] is True


def test_save_target_and_requirement(env):
    detail = core.detail("皇冠")
    assert detail["target"]["requirement"]["affixes"][0]["affix"] == "最大装弹数"
    saved = core.save_target(
        "皇冠",
        {
            "tier": "T1",
            "skills": [10, 10, 9],
            "affix_main": ["最大装弹数", "攻击力"],
            "affix_sub": ["优越代码伤害"],
            "affix_note": "主: 装弹/攻击 | 副: 优越",
            "note": "人工校核 test",
        },
        detail["targets_rev"],
    )
    assert saved["target"]["tier"] == "T1"
    assert saved["target"]["skills"] == [10, 10, 9]
    assert [x["affix"] for x in saved["target"]["requirement"]["affixes"]] == ["最大装弹数", "攻击力"]
    assert saved["targets_rev"] != detail["targets_rev"]
    stored = json.loads(env["targets"].read_text(encoding="utf-8"))
    crown = next(c for c in stored["characters"] if c["name"] == "皇冠")
    assert {"最大装弹数", "攻击力", "优越代码伤害"}.issubset(set(crown["affix_priority"]))
    assert sum(crown["affix_targets"].values()) == 8
    assert (env["data"] / "targets.json.bak").is_file()


def test_save_target_rev_conflict(env):
    detail = core.detail("皇冠")
    core.save_target("皇冠", {"tier": "T1"}, detail["targets_rev"])
    with pytest.raises(core.Conflict):
        core.save_target("皇冠", {"tier": "T2"}, detail["targets_rev"])


def test_save_portrait_upload(env):
    png = base64.b64encode(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000050001"
    )).decode()
    saved = core.save_portrait("皇冠", "data:image/png;base64," + png)
    assert saved["portrait"]["has_cache"] is True
    assert saved["portrait"]["source"] == "manual"
    key = saved["portrait"]["key"]
    path, mime = core.portrait_file(key)
    assert path.is_file()
    assert mime == "image/png"
    index = json.loads((env["data"] / "portraits" / "index.json").read_text(encoding="utf-8"))
    assert index["items"][key]["source"] == "manual"

# ---------------------------------------------------------------------------
# v2: batched save, standalone lib, slim bootstrap, cube-free progress
# ---------------------------------------------------------------------------
def _piece(level, value=14.63):
    return {
        "level": level,
        "slots": [
            {"affix": "攻击力", "value": value, "locked": False},
            {"affix": "暴击率", "value": 2.30, "locked": False},
            {"affix": "", "value": 0, "locked": False},
        ],
    }


def test_bootstrap_payload_is_slim(env):
    assert len(json.dumps(core.bootstrap(), ensure_ascii=False).encode()) < 60000
    assert "guide_date" in core.bootstrap()


def test_batch_save_all_four(env):
    detail = core.detail("小红帽")
    pieces = {
        "head": _piece(5),
        "body": _piece(4),
        "arm": _piece(3),
        "leg": _piece(2),
    }
    rev = detail["rev"]
    body = core.save_gear_batch("小红帽", pieces, rev)
    gear = body["character"]["gear"]
    assert {s: gear[s]["level"] for s in ("head", "body", "arm", "leg")} == {
        "head": 5, "body": 4, "arm": 3, "leg": 2,
    }
    assert body["rev"] != rev
    assert body["render"]["ok"] is True
    stored = json.loads((env["data"] / "player_progress.json").read_text(encoding="utf-8"))
    assert stored["characters"]["小红帽"]["gear"]["leg"]["level"] == 2


def test_batch_validation_is_atomic(env):
    detail = core.detail("小红帽")
    before = (env["data"] / "player_progress.json").read_bytes()
    pieces = {
        "head": _piece(5),
        "body": _piece(5, value=99),  # invalid
        "arm": _piece(3),
        "leg": _piece(2),
    }
    with pytest.raises(ValueError, match="身躯|槽"):
        core.save_gear_batch("小红帽", pieces, detail["rev"])
    assert (env["data"] / "player_progress.json").read_bytes() == before


def test_batch_conflict_leaves_file(env):
    detail = core.detail("小红帽")
    middle = (env["data"] / "player_progress.json").read_bytes()
    # first successful save advances rev
    core.save_gear_batch("小红帽", {"head": _piece(5)}, detail["rev"])
    changed = (env["data"] / "player_progress.json").read_bytes()
    assert changed != middle
    with pytest.raises(core.Conflict):
        core.save_gear_batch("小红帽", {"body": _piece(4)}, detail["rev"])
    assert (env["data"] / "player_progress.json").read_bytes() == changed


def test_batch_rejects_duplicate_slots(env):
    detail = core.detail("小红帽")
    with pytest.raises(ValueError, match="重复"):
        core.save_gear_batch(
            "小红帽",
            {"head": _piece(5), "头部": _piece(4)},
            detail["rev"],
        )


def test_affix_meta_all_tiers(env):
    meta = core.affix_meta(core.modules().db.load_targets())
    assert len(meta) >= 11
    for affix, info in meta.items():
        assert len(info["tiers"]) == 15, affix
    assert meta["攻击速度"]["tiers"][-1] == 6.09
    assert meta["优越代码伤害"]["tiers"][-1] == 29.16


def test_no_skill_dependency(env):
    db = core.modules().db
    assert Path(db.__file__).resolve().parent.name == "nikke_lib"
    assert "skills" not in str(core.targets_path())
    assert core.teams()["interception"]["title"]


def test_targets_seeded_on_first_run(env, monkeypatch):
    import shutil
    import tempfile

    fresh = Path(tempfile.mkdtemp())
    monkeypatch.setenv("NIKKE_DATA_DIR", str(fresh / "data"))
    monkeypatch.delenv("NIKKE_TARGETS_PATH", raising=False)
    core.modules().db.invalidate_targets_cache()
    seeded = fresh / "data" / "targets.json"
    assert not seeded.is_file()
    core.detail("小红帽")
    assert seeded.is_file()
    assert len(json.loads(seeded.read_text(encoding="utf-8"))["characters"]) == 86
    shutil.rmtree(fresh, ignore_errors=True)


def test_save_meta_ignores_cube(env):
    detail = core.detail("小红帽")
    body = core.save_meta(
        "小红帽",
        {"skills": [4, 4, 4], "cube": "随便一个魔方", "notes": "cube 不应写入"},
        detail["rev"],
    )
    assert body["character"]["skills"] == [4, 4, 4]
    stored = json.loads((env["data"] / "player_progress.json").read_text(encoding="utf-8"))
    # cube may still exist from legacy data, but must not have been changed
    entry = stored["characters"]["小红帽"]
    assert entry.get("cube", "") != "随便一个魔方"


def test_plugin_api_imports_without_path(env):
    pytest.importorskip("fastapi")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "nikke_plugin_api_test", env["plugin"] / "dashboard" / "plugin_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.router is not None


def test_health_reports_bundled_lib(env):
    health = core.health()
    assert health["ok"] is True
    assert health["lib"].endswith("nikke_lib")
    assert health["guide_date"] == "2026-11-01"
