from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PLUGIN / "dashboard"))

from nikke_lib import nikke_advisor as advisor
from nikke_lib.nikke_db import DEFAULT_AFFIX_SCALE


def _hit(q):
    return {i: advisor.SLOT_HIT[i] * q for i in (1, 2, 3)}


def test_wash_costs_and_policy():
    ideal = advisor._wash_solution({1: 1.0, 2: 0.5, 3: 0.3})["V"]
    assert ideal[(frozenset(), frozenset())] == pytest.approx(16 / 3, abs=1e-3)

    q = 5 / 11
    sol = advisor._wash_solution(_hit(q))
    V, policy = sol["V"], sol["policy"]
    assert V[(frozenset(), frozenset())] == pytest.approx(17.73, abs=0.02)
    assert V[(frozenset({2, 3}), frozenset())] == pytest.approx(3 / q, abs=1e-3)
    assert policy[(frozenset(), frozenset({3}))] == frozenset({3})
    assert policy[(frozenset(), frozenset({2}))] == frozenset()
    assert policy[(frozenset({3}), frozenset({2}))] == frozenset({2, 3})


def test_wash_plan_lock_advice():
    priority = {"攻击力", "优越代码伤害", "最大装弹数", "暴击伤害", "暴击率"}
    scale = dict(DEFAULT_AFFIX_SCALE)
    plan = advisor.wash_plan(priority, scale, set(), set(), current_locks=set())
    assert plan["q"] == pytest.approx(5 / 11)
    assert plan["lock_now"] == []
    locked = advisor.wash_plan(priority, scale, {1}, set(), current_locks={1})
    assert locked["delta"] == pytest.approx(0.888, abs=0.01)

    target = {"name": "測試甲", "note": "", "affix_priority": ["攻击力", "最大装弹数"]}
    piece = {"level": 5, "slots": [
        {"affix": "攻击力", "value": 14.63, "locked": True},
        {"affix": "暴击率", "value": 2.30, "locked": False},
        {"affix": "", "value": 0.0, "locked": False},
    ]}
    out = advisor.analyze_piece("head", piece, target, scale)
    assert out["bad"] == [2, 3]
    assert out["remaining_cost"] > 0
    assert out["steps"][0].startswith("命中概率")
    assert any("当前建议" in s for s in out["steps"])


def test_prydwen_index_parser(tmp_path, monkeypatch):
    monkeypatch.setenv("NIKKE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NIKKE_TARGETS_PATH", str(_PLUGIN / "references" / "targets.json"))
    spec = importlib.util.spec_from_file_location(
        "resolve_portraits_under_test", _PLUGIN / "scripts" / "resolve_portraits.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.allowed("https://www.prydwen.gg/nikke/characters")
    assert mod.allowed("https://cdn.prydwen.gg/images/nikke/characters/x_card.webp")
    assert mod.allowed("https://cdnimg-v2.gamekee.com/wiki2.0/pro/1253/x.png")
    assert mod.allowed("https://www.gamekee.com/v1/entry/treesByPid?pid=64599")
    assert not mod.allowed("https://example.com/evil.png")
    assert not mod.allowed("https://wiki.biligame.com/nikke/x")
    assert mod.norm("托比【珍藏品】") == "托比"
    assert mod.norm("红莲：弑神") == mod.norm("红莲:弑神")
    html = (
        '<a href="/nikke/characters/red-hood">'
        '<img src="https://cdn.prydwen.gg/images/nikke/characters/red-hood_card.webp"></a>'
        '<img data-src="//cdn.prydwen.gg/images/nikke/characters/crown_card.webp">'
        '<img src="/images/nikke/characters/alice_card.webp">'
    )
    pairs = mod.parse_prydwen_index(html)
    assert pairs["red-hood"][0].endswith("red-hood_card.webp")
    assert pairs["crown"][0] == "https://cdn.prydwen.gg/images/nikke/characters/crown_card.webp"
    assert pairs["alice"][0].endswith("alice_card.webp")


def test_gamekee_name_map_covers_targets():
    import json

    name_map = json.loads((_PLUGIN / "references" / "portrait_names.json").read_text(encoding="utf-8"))
    targets = json.loads((_PLUGIN / "references" / "targets.json").read_text(encoding="utf-8"))
    names = {c["name"] for c in targets["characters"]}
    assert set(name_map) <= names
    assert all(isinstance(v, str) and v for v in name_map.values())
    assert len(name_map) >= 25
