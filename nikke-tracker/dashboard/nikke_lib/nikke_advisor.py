#!/usr/bin/env python3
"""Reroll / resource decision engine for the NIKKE tracker.

Models the OL (Overload) gear economy:

  * slot hit rates     Slot1 100%, Slot2 50%, Slot3 30%
  * stone cost         1 + number of locked slots
  * correct order      3 -> 2 -> 1 (hardest slot first, cheapest attempts first)
"""

from __future__ import annotations

from typing import Any, Iterable

from .nikke_db import (
    SLOT_LABELS,
    SLOTS,
    affix_scale,
    canonical_affix,
    format_affix_tier,
    get_affix_tier,
)

SLOT_HIT = {1: 1.0, 2: 0.5, 3: 0.3}
LOCK_COST = {0: 1, 1: 2, 2: 3}
EFFECTIVE_Q = 0.55
GOOD_Q = 0.80
IDEAL_EMPTY_COST = round(1 / 0.3 + 2 / 0.5 + 3 / 1.0, 2)  # 10.33
SHORT = {
    "攻击力": "攻击",
    "优越代码伤害": "优越",
    "最大装弹数": "装弹",
    "暴击率": "暴击",
    "暴击伤害": "暴伤",
    "蓄力伤害": "蓄力",
    "蓄力速度": "蓄速",
    "攻击速度": "攻速",
    "防御力": "防御",
    "最大体力": "体力",
}


def quality(value: float, affix: str, scale: dict[str, float]) -> float:
    max_value = scale.get(affix)
    if not max_value or not value:
        return 0.0
    return max(0.0, min(float(value) / float(max_value), 1.0))


def expected_stones(bad_slots: Iterable[int], locked_count: int = 0) -> float:
    """Expected stones to fill ``bad_slots`` locking already-good slots."""
    total = 0.0
    locks = max(0, int(locked_count))
    for slot in sorted({int(s) for s in bad_slots}, reverse=True):
        if slot not in SLOT_HIT:
            continue
        total += (1 + locks) / SLOT_HIT[slot]
        locks += 1
    return round(total, 2)


def _target_counts(target: dict[str, Any], key: str = "affix_targets") -> dict[str, int]:
    counts = target.get(key) or {}
    return {canonical_affix(k): int(v) for k, v in counts.items() if int(v) > 0}


def _slot_view(
    item: dict[str, Any],
    idx: int,
    priority: set[str],
    scale: dict[str, float],
    no_requirement: bool = False,
) -> dict[str, Any]:
    affix = canonical_affix(item.get("affix", ""))
    value = float(item.get("value", 0) or 0)
    q = quality(value, affix, scale)
    tier = get_affix_tier(affix, value)
    tier_str = format_affix_tier(tier)
    if no_requirement:
        matched = bool(affix)
        good = bool(affix)
        effective = bool(affix)
    else:
        matched = bool(affix) and affix in priority
        good = matched and ((tier is not None and tier >= 10) or q >= GOOD_Q)
        effective = matched and ((tier is not None and tier >= 6) or q >= EFFECTIVE_Q)
    return {
        "index": idx,
        "affix": affix,
        "value": value,
        "locked": bool(item.get("locked", False)),
        "quality": q,
        "tier": tier,
        "tier_str": tier_str,
        "matched": matched,
        "effective": effective,
        "good": good,
    }


def analyze_piece(
    slot_key: str,
    piece: dict[str, Any],
    target: dict[str, Any],
    scale: dict[str, float],
) -> dict[str, Any]:
    note = target.get("note", "")
    no_requirement = (
        bool(target.get("no_requirement"))
        or "无要求" in note
        or "核心词条: 无" in note
        or not target.get("affix_priority")
    )
    priority = {canonical_affix(a) for a in target.get("affix_priority", [])}
    slots = [
        _slot_view(item, idx, priority, scale, no_requirement=no_requirement)
        for idx, item in enumerate((piece.get("slots") or [])[:3], start=1)
    ]
    while len(slots) < 3:
        slots.append(_slot_view({}, len(slots) + 1, priority, scale, no_requirement=no_requirement))

    level = int(piece.get("level", 0) or 0)
    good = [s["index"] for s in slots if s["good"]]
    bad = [s["index"] for s in slots if not s["good"]]
    locked = [s["index"] for s in slots if s["locked"]]
    effective = [s for s in slots if s["effective"]]

    if no_requirement:
        slot_score = sum(1.0 if s["affix"] else 0.5 for s in slots) / 3
        score = slot_score
        bad = []
    else:
        slot_score = sum((0.55 + 0.45 * s["quality"]) if s["matched"] else 0.0 for s in slots) / 3
        score = slot_score

    issues: list[dict[str, str]] = []

    if not no_requirement:
        for s in slots:
            if not s["affix"]:
                issues.append({"severity": "info", "text": f"槽{s['index']} 为空，需要洗条填充"})
            elif not s["matched"]:
                issues.append({"severity": "warn", "text": f"槽{s['index']}「{s['affix']}」不在该角色目标词条内（无效词条）"})
            elif s["quality"] < EFFECTIVE_Q:
                issues.append({"severity": "warn", "text": f"槽{s['index']}「{s['affix']}」{s['value']:.2f} 仅达上限 {s['quality'] * 100:.0f}%，低于可用线"})

        for s in slots:
            if not s["locked"]:
                continue
            if not s["matched"]:
                issues.append({"severity": "error", "text": f"槽{s['index']} 锁定了非目标词条「{s['affix']}」，属于纯浪费，建议立即解锁"})
            elif not s["good"]:
                issues.append({
                    "severity": "warn",
                    "text": f"槽{s['index']} 锁定了「{s['affix']}」{s['value']:.2f}（品质 {s['quality'] * 100:.0f}%），低值锁定会卡住上限",
                })

    premature = False
    if 1 in locked and (2 in bad or 3 in bad):
        premature = True
        remaining = expected_stones(bad, len(good))
        cheaper = expected_stones(bad, max(0, len(good) - 1))
        extra = round(remaining - cheaper, 2)
        issues.append({
            "severity": "error",
            "text": (
                "过早锁 1 槽：槽1 是 100% 命中，先锁它会把每次洗条从 1 石头抬到 2 石头，"
                f"槽2/3 的期望消耗翻倍（本次剩余约 +{extra:.1f} 石头）。下次务必先洗 3 槽。"
            ),
        })

    remaining_cost = expected_stones(bad, len(good))
    steps: list[str] = []
    locks = len(good)
    filled = set(good)
    for idx in sorted(bad, reverse=True):
        rate = int(SLOT_HIT[idx] * 100)
        if locks == 0:
            steps.append(f"解锁全部槽位（1 石头/次），先洗槽 {idx}（命中率 {rate}%）")
        else:
            held = "、".join(str(i) for i in sorted(filled))
            steps.append(f"锁定槽 {held}（{1 + locks} 石头/次），洗槽 {idx}（命中率 {rate}%）")
        steps.append(f"槽 {idx} 命中目标词条后锁定")
        filled.add(idx)
        locks += 1
    if not bad:
        steps.append("三槽均已达标，停手")

    return {
        "slot": slot_key,
        "label": SLOT_LABELS.get(slot_key, slot_key),
        "level": level,
        "slots": slots,
        "score": score,
        "good": good,
        "bad": bad,
        "locked": locked,
        "effective": len(effective),
        "remaining_cost": remaining_cost,
        "steps": steps,
        "issues": issues,
        "premature_lock": premature,
    }


def diagnose(entry: dict[str, Any], target: dict[str, Any], scale: dict[str, float] | None = None) -> dict[str, Any]:
    scale = scale or affix_scale({})
    pieces = [analyze_piece(slot, entry.get("gear", {}).get(slot, {}), target, scale) for slot in SLOTS]

    counts: dict[str, int] = {}
    qualities: dict[str, list[float]] = {}
    for piece in pieces:
        for s in piece["slots"]:
            if s["effective"]:
                counts[s["affix"]] = counts.get(s["affix"], 0) + 1
                qualities.setdefault(s["affix"], []).append(s["quality"])

    target_counts = _target_counts(target)
    max_counts = _target_counts(target, "affix_targets_max")
    raw_target_total = target.get("target_affix_count")
    if raw_target_total is not None:
        target_total = max(0, int(raw_target_total))
    else:
        target_total = sum(target_counts.values()) or 8
    max_total = sum(max_counts.values()) or 12
    effective_total = sum(counts.values())

    priority_order = [canonical_affix(a) for a in target.get("affix_priority", [])]
    core_bits = [f"{SHORT.get(a, a)}×{counts[a]}" for a in priority_order if counts.get(a)]
    core_summary = " · ".join(core_bits) if core_bits else "暂无有效词条"

    score = sum(p["score"] for p in pieces) / len(pieces)
    if score >= 0.85:
        grade = "毕业"
    elif score >= 0.68:
        grade = "成型"
    elif score >= 0.50:
        grade = "过渡"
    else:
        grade = "起步"

    skills = list(entry.get("skills") or [1, 1, 1])
    skills_target = list(target.get("skills") or [10, 10, 10])
    skill_gap = sum(max(0, t - c) for c, t in zip(skills, skills_target))

    actions: list[str] = []
    overflow: list[str] = []
    note = target.get("note", "")
    no_requirement = "无要求" in note or "核心词条: 无" in note or not target.get("affix_priority")

    if no_requirement:
        grade = "达标"
        core_summary = "核心词条无特殊要求（一图流）"
        target_total = 0
        if skill_gap:
            labels = ("技能1", "技能2", "爆裂")
            gaps = [f"{labels[i]} {skills[i]}→{skills_target[i]}" for i in range(3) if skills[i] < skills_target[i]]
            actions.append(f"补技能等级：{'，'.join(gaps)}（共 +{skill_gap} 级）")
        actions.append("一图流核心词条无特殊要求，现有词条已可正常使用，无需洗练")
    else:
        if skill_gap:
            labels = ("技能1", "技能2", "爆裂")
            gaps = [f"{labels[i]} {skills[i]}→{skills_target[i]}" for i in range(3) if skills[i] < skills_target[i]]
            actions.append(f"补技能等级：{'，'.join(gaps)}（共 +{skill_gap} 级）")

        bad_pieces = [p for p in pieces if p["bad"]]
        if bad_pieces:
            focus = min(bad_pieces, key=lambda p: (p["score"], p["slot"]))
            actions.append(
                f"优先洗 {focus['label']}（当前 {focus['score'] * 100:.0f}%，"
                f"缺口 {3 - len(focus['good'])} 槽，预计 {focus['remaining_cost']:.1f} 石头）"
            )

        overflow = []
        for affix, count in counts.items():
            cap = max_counts.get(affix)
            if cap and count > cap:
                overflow.append(f"{affix} {count} 条 > 上限 {cap} 条")
        if overflow:
            actions.append(f"词条溢出：{'；'.join(overflow)}，优先洗掉品质最低的一条")

        open_slots = sum(len(p["bad"]) for p in pieces)
        if effective_total < target_total or open_slots:
            actions.append(
                f"有效词条 {effective_total} 条，仍有 {open_slots} 个槽位未达标："
                "先补齐关键部位，再考虑冲满词条"
            )
        else:
            actions.append(f"有效词条已达 {effective_total} 条：先停手，资源转投下一角色或冲满词条")

        actions.append("停手标准：目标词条品质 ≥80% 上限即停；60%-80% 仅在资源充裕时补")

    return {
        "name": entry.get("name", target.get("name", "")),
        "target": target,
        "pieces": pieces,
        "counts": counts,
        "qualities": qualities,
        "effective": effective_total,
        "target_total": target_total,
        "max_total": max_total,
        "core_summary": core_summary,
        "score": score,
        "grade": grade,
        "skills": {"current": skills, "target": skills_target, "gap": skill_gap},
        "actions": actions,
        "priority": actions[0] if actions else "保持现状",
        "overflow": overflow if not no_requirement else [],
    }


def format_terminal(diag: dict[str, Any]) -> str:
    target = diag["target"]
    tier = target.get("tier", "")
    tier_tag = f"【{tier}】 " if tier else ""
    lines: list[str] = []
    lines.append(f"=== {tier_tag}{diag['name']} · {target.get('element', '')}/{target.get('class', '')} · 爆裂 {target.get('burst', '')} ===")
    lines.append(
        f"综合评级: {diag['grade']} ({diag['score'] * 100:.0f}%) | "
        f"有效词条 {diag['effective']}/{diag['target_total']}（12 词条上限 {diag['max_total']}）"
    )
    sk = diag["skills"]
    lines.append(f"技能: {'/'.join(map(str, sk['current']))} → 目标 {'/'.join(map(str, sk['target']))}")
    lines.append("装备: " + " | ".join(f"{p['label']} Lv{p['level']}" for p in diag["pieces"]))
    lines.append("")

    for p in diag["pieces"]:
        lines.append(f"[{p['label']}] {p['score'] * 100:.0f}%  Lv{p['level']}  有效 {p['effective']}/3")
        for s in p["slots"]:
            if not s["affix"]:
                lines.append(f"  槽{s['index']} 空")
                continue
            lock = " 🔒" if s["locked"] else ""
            if not s["matched"]:
                verdict = "无效词条"
            elif s["good"]:
                verdict = "优质"
            elif s["effective"]:
                verdict = "可用"
            else:
                verdict = "偏低"
            tier_part = f" [{s['tier_str']}]" if s.get("tier_str") else ""
            lines.append(
                f"  槽{s['index']} {s['affix']} {s['value']:.2f}%{tier_part} "
                f"({s['quality'] * 100:.0f}%) {verdict}{lock}"
            )
        if p["issues"]:
            lines.append("  诊断:")
            for issue in p["issues"]:
                mark = {"error": "✗", "warn": "!", "info": "·"}.get(issue["severity"], "·")
                lines.append(f"    {mark} {issue['text']}")
        if p["bad"]:
            lines.append(f"  预计剩余: {p['remaining_cost']:.1f} 石头")
            for step in p["steps"]:
                lines.append(f"    - {step}")
        lines.append("")

    lines.append("行动建议:")
    for i, action in enumerate(diag["actions"], start=1):
        lines.append(f"{i}. {action}")
    return "\n".join(lines)


def recommend(entry: dict[str, Any], target: dict[str, Any], scale: dict[str, float] | None = None) -> dict[str, Any]:
    return diagnose(entry, target, scale)