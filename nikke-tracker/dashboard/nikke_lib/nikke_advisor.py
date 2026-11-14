#!/usr/bin/env python3
"""Reroll / resource decision engine for the NIKKE tracker.

Models the OL (Overload) gear reroll (洗词条) economy:

  * refresh mechanic  each refresh re-rolls every unlocked slot independently:
                      slot 1 always shows a new affix, slot 2 with 50%,
                      slot 3 with 30%; a slot that does not appear is EMPTY.
                      Locked slots keep their affix across refreshes.
  * hit probability   an appearing affix counts as a hit when its type is in
                      the character's ``affix_priority``.  With a uniform pool
                      of ``len(scale)`` affixes the per-refresh hit rate of an
                      unlocked slot i is ``SLOT_HIT[i] * |priority| / |scale|``.
                      Value polish (数值精洗) is a later phase and is not costed.
  * stone cost        LOCK_COST: 1 / 2 / 3 stones per refresh with 0 / 1 / 2
                      locked slots.

Expected stones and the optimal lock policy are solved exactly with value
iteration over the 27 (locked, matched-unlocked) states, cached by hit rates.
``analyze_piece`` turns the solution into per-piece steps plus a
lock-now recommendation.
"""

from __future__ import annotations

from itertools import combinations, product
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


# ---------------------------------------------------------------------------
# wash (洗词条) expected-stone solver
#
# State (L, U): disjoint slot subsets.  L = matched AND locked (persist across
# refreshes), U = matched but unlocked (ephemeral — after the next refresh an
# unlocked slot is matched with probability h_i regardless of history).
# L ∪ U = {1, 2, 3} is terminal: stop washing and the affixes stay.
# Action: before each refresh pick the lock set L' ⊆ currently matched slots
# (|L'| ≤ 2; locks can be re-chosen freely between refreshes); the refresh
# costs LOCK_COST[|L'|] and every slot outside L' is matched with probability
# h_i, independently.
# ---------------------------------------------------------------------------
_WASH_SLOTS = (1, 2, 3)
_WASH_ALL = frozenset(_WASH_SLOTS)
_WASH_STATES: list[tuple[frozenset, frozenset]] = [
    (
        frozenset(i for i, c in zip(_WASH_SLOTS, codes) if c == 1),
        frozenset(i for i, c in zip(_WASH_SLOTS, codes) if c == 2),
    )
    for codes in product((0, 1, 2), repeat=3)
]
_WASH_CACHE: dict[tuple[float, float, float], dict[str, Any]] = {}
_WASH_TOL = 1e-9
_WASH_MAX_ITER = 20000


def _lock_choices(matched: frozenset) -> list[frozenset]:
    """Lock subsets (≤ 2) of the currently matched slots; the empty set is
    first so ties deterministically prefer not locking."""
    out = [frozenset()]
    for size in (1, 2):
        if len(matched) < size:
            break
        out.extend(frozenset(c) for c in combinations(sorted(matched), size))
    return out


def _action_value(
    V: dict[tuple[frozenset, frozenset], float],
    locks: frozenset,
    h: dict[int, float],
) -> float:
    """Expected total cost of one refresh with ``locks`` applied, playing
    optimally afterwards (``V`` is the converged value table)."""
    total = float(LOCK_COST[len(locks)])
    free = tuple(i for i in _WASH_SLOTS if i not in locks)
    for hits in product((True, False), repeat=len(free)):
        prob = 1.0
        unlocked: list[int] = []
        for i, hit in zip(free, hits):
            prob *= h[i] if hit else 1.0 - h[i]
            if hit:
                unlocked.append(i)
        total += prob * V[(locks, frozenset(unlocked))]
    return total


def _wash_solution(h: dict[int, float]) -> dict[str, Any]:
    """Value iteration over the 27 wash states; solutions cached by hit rates
    (q only takes a handful of distinct values across characters)."""
    key = (round(h[1], 9), round(h[2], 9), round(h[3], 9))
    cached = _WASH_CACHE.get(key)
    if cached is not None:
        return cached
    V: dict[tuple[frozenset, frozenset], float] = {st: 0.0 for st in _WASH_STATES}
    for _ in range(_WASH_MAX_ITER):
        new: dict[tuple[frozenset, frozenset], float] = {}
        for state in _WASH_STATES:
            matched = state[0] | state[1]
            if matched == _WASH_ALL:
                new[state] = 0.0
                continue
            new[state] = min(_action_value(V, locks, h) for locks in _lock_choices(matched))
        delta = max(abs(new[st] - V[st]) for st in _WASH_STATES)
        V = new
        if delta < _WASH_TOL:
            break
    policy: dict[tuple[frozenset, frozenset], frozenset | None] = {}
    for state in _WASH_STATES:
        matched = state[0] | state[1]
        if matched == _WASH_ALL:
            policy[state] = None
            continue
        best: float | None = None
        best_locks = frozenset()
        for locks in _lock_choices(matched):
            value = _action_value(V, locks, h)
            if best is None or value < best - 1e-12:
                best = value
                best_locks = locks
        policy[state] = best_locks
    solution = {"V": V, "policy": policy}
    _WASH_CACHE[key] = solution
    return solution


def wash_plan(
    priority: set[str],
    scale: dict[str, float],
    matched_locked: Iterable[int],
    matched_unlocked: Iterable[int],
    current_locks: Iterable[int] | None = None,
) -> dict[str, Any]:
    """Expected stones and lock advice for one piece's type-completion phase.

    ``priority``/``scale`` derive the hit rates (uniform affix-pool
    assumption).  ``matched_locked``/``matched_unlocked`` describe the piece.
    When ``current_locks`` (the matched slots the player keeps locked right
    now) differs from the optimal first move, ``delta`` is the expected stone
    cost of that suboptimal locking for one refresh.
    """
    pool = len(scale) or 1
    wanted = {a for a in priority if a in scale}
    q = min(1.0, len(wanted) / pool) if wanted else 0.0
    h = {i: SLOT_HIT[i] * q for i in _WASH_SLOTS}
    state = (frozenset(matched_locked), frozenset(matched_unlocked))
    if not wanted or state[0] | state[1] == _WASH_ALL:
        return {"q": q, "hit": h, "cost": 0.0, "lock_now": [], "delta": 0.0, "solution": None}
    solution = _wash_solution(h)
    V = solution["V"]
    cost = V[state]
    lock_now = solution["policy"][state] or frozenset()
    delta = 0.0
    if current_locks is not None:
        held = frozenset(current_locks) & (state[0] | state[1])
        if held != lock_now:
            delta = max(0.0, _action_value(V, held, h) - cost)
    return {
        "q": q,
        "hit": h,
        "cost": cost,
        "lock_now": sorted(lock_now),
        "delta": delta,
        "solution": solution,
    }


def _wash_steps(
    plan: dict[str, Any],
    matched: set[int],
    locked: set[int],
    bad: list[int],
) -> list[str]:
    """Human-readable wash plan: probabilities, the lock-now advice and
    contingent lock rules for each pending slot (hardest hit rate first)."""
    h = plan["hit"]
    q = plan["q"]
    policy = plan["solution"]["policy"]
    optimal = set(plan["lock_now"])
    held = matched & locked
    steps: list[str] = [
        f"命中概率：词条1 {h[1]:.0%}（必出 × 目标词条 {q:.0%}）、"
        f"词条2 {h[2]:.0%}（50% × 目标词条 {q:.0%}）、"
        f"词条3 {h[3]:.0%}（30% × 目标词条 {q:.0%}）"
    ]
    bits: list[str] = []
    unlock = sorted(held - optimal)
    if unlock:
        bits.append(f"先解锁槽 {'、'.join(map(str, unlock))}")
    if optimal:
        bits.append(f"锁定槽 {'、'.join(map(str, sorted(optimal)))}（{LOCK_COST[len(optimal)]} 石头/次）")
    if not optimal:
        bits.append("不锁定（1 石头/次）" if not unlock else "之后不锁定（1 石头/次）")
    steps.append(f"当前建议：{'，'.join(bits)}，刷新")
    accumulated = set(optimal)
    # every slot that will NOT be locked by the current recommendation has to
    # (re-)hit before the piece is complete — unmatched slots plus matched
    # slots the policy leaves unlocked (their affix is ephemeral)
    pending = set(bad) | (matched - optimal)
    order = sorted(pending, key=lambda i: (h[i], -i))  # hardest slot first: 3 → 2 → 1
    for i in order:
        nxt = policy.get((frozenset(accumulated), frozenset({i})))
        if nxt is None:
            steps.append(f"词条{i} 命中目标词条后三槽类型集齐，停手")
            continue
        nxt_set = set(nxt)
        if nxt_set == accumulated:
            steps.append(
                f"词条{i} 命中后暂不锁定（维持 {LOCK_COST[len(accumulated)]} 石头/次），先攒更难的槽位"
            )
        else:
            steps.append(
                f"词条{i} 命中后：锁定槽 {'、'.join(map(str, sorted(nxt_set)))}（{LOCK_COST[len(nxt_set)]} 石头/次）"
            )
        accumulated = nxt_set
    closing = "三槽类型集齐即停手；冲高数值（阶数）属于后续精洗，另行消耗"
    if len(order) >= 2:
        easiest = order[-1]
        probe = policy.get((frozenset(optimal), frozenset({easiest})))
        if probe is not None and set(probe) == optimal:
            closing += f"（较易的词条{easiest} 先命中时暂不锁定，优先攒更难的词条）"
    steps.append(closing)
    return steps


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
    matched = [s["index"] for s in slots if s["matched"]]
    bad = [s["index"] for s in slots if not s["matched"]]
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
    remaining_cost = 0.0
    steps: list[str] = []
    if no_requirement or not priority:
        steps.append("核心词条无特殊要求，无需洗练")
    elif not bad:
        steps.append("三槽类型均已命中，停手；数值精洗另行消耗")
    else:
        matched_set = set(matched)
        locked_set = set(locked)
        plan = wash_plan(
            priority,
            scale,
            matched_set & locked_set,
            matched_set - locked_set,
            current_locks=matched_set & locked_set,
        )
        remaining_cost = round(plan["cost"], 2)
        if plan["solution"] is None:
            steps.append("目标词条均不在词条池内，无法估算洗练消耗")
        else:
            steps = _wash_steps(plan, matched_set, locked_set, bad)
            held = matched_set & locked_set
            optimal = set(plan["lock_now"])
            if held != optimal and plan["delta"] > 0.05:
                premature = 1 in (held - optimal) and (2 in bad or 3 in bad)
                actions = [f"锁定槽 {i}" for i in sorted(optimal - held)]
                actions += [f"解锁槽 {i}" for i in sorted(held - optimal)]
                if premature:
                    issues.append({
                        "severity": "error",
                        "text": (
                            f"过早锁词条1：每次刷新多付 1 石头，而词条1 未锁定时命中率也有 "
                            f"{plan['hit'][1]:.0%}，丢了也容易刷回。建议{'、'.join(actions)}，"
                            f"期望省约 {plan['delta']:.1f} 石头"
                        ),
                    })
                else:
                    issues.append({
                        "severity": "warn",
                        "text": f"当前锁定非最优：建议{'、'.join(actions)}，期望省约 {plan['delta']:.1f} 石头",
                    })

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
                f"缺口 {len(focus['bad'])} 槽，期望 {focus['remaining_cost']:.1f} 石头）"
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
            lines.append(f"  期望剩余: {p['remaining_cost']:.1f} 石头")
            for step in p["steps"]:
                lines.append(f"    - {step}")
        lines.append("")

    lines.append("行动建议:")
    for i, action in enumerate(diag["actions"], start=1):
        lines.append(f"{i}. {action}")
    return "\n".join(lines)


def recommend(entry: dict[str, Any], target: dict[str, Any], scale: dict[str, float] | None = None) -> dict[str, Any]:
    return diagnose(entry, target, scale)