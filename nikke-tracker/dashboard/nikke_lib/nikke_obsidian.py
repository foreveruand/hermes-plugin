#!/usr/bin/env python3
"""Render the NIKKE progression dashboard into a single Obsidian note.

Hard rule: exactly one note (``<vault>/游戏与联机/nikke.md``). No per-character
files, no sub-folders, no attachments.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .nikke_advisor import diagnose, format_terminal  # noqa: F401  (re-export for callers)
from .nikke_db import (
    affix_scale,
    canonical_affix,
    format_affix_tier,
    get_affix_tier,
    load_progress,
    load_targets,
    profile_dir,
    resolve_target,
)

NOTE_DIR = "游戏与联机"
NOTE_NAME = "nikke.md"


def vault_dir() -> Path:
    override = os.environ.get("NIKKE_VAULT_PATH") or os.environ.get("OBSIDIAN_VAULT_PATH")
    if override:
        return Path(override).expanduser()
    root = profile_dir()
    for candidate in (root / "main", root / "vault"):
        if candidate.is_dir():
            return candidate
    return root / "main"


def note_path() -> Path:
    return vault_dir() / NOTE_DIR / NOTE_NAME


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _slot_text(slot: dict[str, Any]) -> str:
    affix = slot.get("affix") or ""
    if not affix:
        return "—"
    val = float(slot.get("value", 0) or 0)
    canonical = canonical_affix(affix)
    tier = get_affix_tier(canonical, val)
    tier_tag = f" ({format_affix_tier(tier)})" if tier else ""
    text = f"{canonical} {val:.2f}%{tier_tag}"
    if slot.get("locked"):
        text += " 🔒"
    return text


def render_markdown(progress: dict[str, Any], targets_doc: dict[str, Any]) -> str:
    scale = affix_scale(targets_doc)
    diags: list[dict[str, Any]] = []
    missing: list[str] = []
    for name, entry in progress.get("characters", {}).items():
        try:
            target = resolve_target(name, targets_doc)
        except KeyError:
            missing.append(name)
            continue
        diags.append(diagnose(entry, target, scale))

    order = {"T0": 0, "T1": 1, "T2": 2}
    diags.sort(key=lambda d: (-d["score"], order.get(d["target"].get("tier", "T2"), 9), d["name"]))

    raw_updated = str(progress.get("updated") or "").strip()
    try:
        stamp = datetime.fromisoformat(raw_updated.replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone()
        now = stamp.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [
        "---",
        'title: "胜利女神：NIKKE 角色养成与装备进度"',
        f"updated: {now}",
        "tags:",
        "  - 游戏",
        "  - nikke",
        "  - 养成",
        "---",
        "",
        "# 胜利女神：NIKKE 角色养成与装备进度",
        "",
        f"> 由 `nikke-tracker` 自动生成 · 更新时间 {now} · 覆盖 {len(diags)} 名角色",
        "> 词条品质 = 数值 ÷ 满值上限；🔒 表示已锁定槽位。",
        "",
        "## 一、养成汇总",
        "",
        "| 角色 | 梯度 | 属性/职业 | 爆裂 | 技能当前/毕业 | 装备强化(头/身/手/腿) | 有效词条数(达成/目标) | 核心词条概览 | 综合评级 | 优先建议 |",
        "| --- | :---: | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for d in diags:
        t = d["target"]
        gear = "/".join(str(p["level"]) for p in d["pieces"])
        skills = "/".join(map(str, d["skills"]["current"]))
        skills_t = "/".join(map(str, d["skills"]["target"]))
        suggest = d["actions"][0] if d["actions"] else "保持现状"
        if len(suggest) > 42:
            suggest = suggest[:40] + "…"
        lines.append(
            "| {name} | **{tier}** | {elem}/{cls} | {burst} | {sk} → {skt} | {gear} | {eff}/{tot} | {core} | {grade} {pct:.0f}% | {sug} |".format(
                name=_cell(d["name"]),
                tier=_cell(t.get("tier", "T1")),
                elem=t.get("element", ""),
                cls=t.get("class", ""),
                burst=t.get("burst", ""),
                sk=skills,
                skt=skills_t,
                gear=gear,
                eff=d["effective"],
                tot=d["target_total"],
                core=_cell(d["core_summary"]),
                grade=d["grade"],
                pct=d["score"] * 100,
                sug=_cell(suggest),
            )
        )

    if not diags:
        lines.append("| — | — | — | — | — | — | — | — | 尚无角色数据，运行 `init-demo` |")

    lines += ["", "## 二、角色详情", ""]

    for d in diags:
        t = d["target"]
        note = t.get("note", "")
        no_requirement = "无要求" in note or "核心词条: 无" in note or not t.get("affix_priority")

        lines.append(f"### {d['name']} · 【{t.get('tier', 'T1')}】")
        lines.append("")
        lines.append("**目标基准**")
        lines.append("")
        lines.append("| 项目 | 目标 |")
        lines.append("| --- | --- |")
        lines.append(f"| 梯度优先级 | **{t.get('tier', 'T1')}** |")
        lines.append(
            f"| 定位 | {t.get('tier', '')} · {t.get('element', '')}/{t.get('class', '')} · "
            f"爆裂 {t.get('burst', '')} · {t.get('weapon', '')} |"
        )
        lines.append(f"| 技能加点 | {' / '.join(map(str, t.get('skills', [10, 10, 10])))} |")
        lines.append(f"| 魔方 / 收藏品 | {t.get('cube', '—')} / {t.get('collectible', '—')} |")
        if t.get("note"):
            lines.append(f"| 说明 | {_cell(t['note'])} |")
        lines.append("")

        if no_requirement:
            gear_levels = " / ".join(f"{p['label']} Lv{p['level']}" for p in d["pieces"])
            lines.append("**养成现状**")
            lines.append("")
            lines.append(f"- 技能等级：**{'/'.join(map(str, d['skills']['current']))}**（推荐目标 {'/'.join(map(str, t.get('skills', [10, 10, 10])))}）")
            lines.append(f"- 装备强化：{gear_levels}")
            lines.append("- 词条说明：一图流核心词条无特殊要求，装备穿戴成型即可，无需消耗石头洗练词条。")
            lines.append("")
            continue

        lines.append("**当前四件套**")
        lines.append("")
        lines.append("| 部位 | 等级 | 词条 1 | 词条 2 | 词条 3 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for p in d["pieces"]:
            slots = p["slots"]
            lines.append(
                f"| {p['label']} | Lv{p['level']} | {_slot_text(slots[0])} | {_slot_text(slots[1])} | {_slot_text(slots[2])} |"
            )
        lines.append("")

        lines.append("**智能诊断**")
        lines.append("")
        lines.append(
            f"综合评级 **{d['grade']}（{d['score'] * 100:.0f}%）**，"
            f"有效词条 **{d['effective']}/{d['target_total']}**（12 词条上限 {d['max_total']}），"
            f"技能 **{'/'.join(map(str, d['skills']['current']))}**。"
        )
        lines.append("")
        issue_lines = 0
        for p in d["pieces"]:
            for issue in p["issues"]:
                mark = {"error": "✗", "warn": "!", "info": "·"}.get(issue["severity"], "·")
                lines.append(f"- {mark} **{p['label']}**：{issue['text']}")
                issue_lines += 1
        if not issue_lines:
            lines.append("- ✓ 四件套均已达到目标，无待处理问题。")
        lines.append("")

        if d["pieces"]:
            worst = min(d["pieces"], key=lambda p: p["score"])
            if worst["bad"]:
                lines.append(f"**{worst['label']}洗练方案（期望 {worst['remaining_cost']:.1f} 石头）**")
                lines.append("")
                for i, step in enumerate(worst["steps"], start=1):
                    lines.append(f"{i}. {step}")
                lines.append("")

        lines.append("**下一步优化**")
        lines.append("")
        for i, action in enumerate(d["actions"], start=1):
            lines.append(f"{i}. {action}")
        lines.append("")

    if missing:
        lines += ["## 三、未匹配角色", "", "以下角色在目标库中不存在，已跳过：", ""]
        lines += [f"- {name}" for name in missing]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_note(progress: dict[str, Any] | None = None, targets_doc: dict[str, Any] | None = None) -> Path:
    progress = progress if progress is not None else load_progress()
    targets_doc = targets_doc if targets_doc is not None else load_targets()
    path = note_path()
    text = render_markdown(progress, targets_doc)
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == text:
                return path
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def render(progress: dict[str, Any] | None = None) -> Path:
    return write_note(progress=progress)
