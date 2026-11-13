#!/usr/bin/env python3
"""Data layer for the NIKKE dashboard plugin.

Vendored from `skills/productivity/nikke-tracker/scripts/nikke_db.py`
(2026-11-13) so the plugin runs without the skill installed.  It owns:

  * the bundled baseline (`references/targets.json`, seeded into the profile)
  * the mutable player state (`data/nikke/player_progress.json`)
  * validation/normalisation helpers shared with the advisor and renderer
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PLUGIN_DIR = Path(__file__).resolve().parents[2]
BUNDLED_TARGETS = PLUGIN_DIR / "references" / "targets.json"
SLOTS: tuple[str, ...] = ("head", "body", "arm", "leg")
SLOT_LABELS = {"head": "头部", "body": "身躯", "arm": "臂部", "leg": "腿部"}
SLOT_ALIASES = {
    "head": "head", "头部": "head", "头": "head", "头盔": "head",
    "body": "body", "身躯": "body", "身体": "body", "衣服": "body", "上衣": "body",
    "arm": "arm", "臂部": "arm", "手臂": "arm", "手": "arm", "手套": "arm",
    "leg": "leg", "腿部": "leg", "腿": "leg", "鞋子": "leg", "脚": "leg",
}

AFFIX_ALIASES = {
    "攻击": "攻击力",
    "攻击力": "攻击力",
    "优越代码": "优越代码伤害",
    "优越代码伤害": "优越代码伤害",
    "优越伤害": "优越代码伤害",
    "最大装弹数": "最大装弹数",
    "装弹数": "最大装弹数",
    "装弹": "最大装弹数",
    "暴击": "暴击率",
    "暴击率": "暴击率",
    "暴击伤害": "暴击伤害",
    "爆伤": "暴击伤害",
    "蓄力伤害": "蓄力伤害",
    "蓄力速度": "蓄力速度",
    "攻击速度": "攻击速度",
    "攻速": "攻击速度",
    "防御力": "防御力",
    "防御": "防御力",
    "最大体力": "最大体力",
    "体力": "最大体力",
    "生命": "最大体力",
}

DEFAULT_AFFIX_SCALE = {
    "攻击力": 14.63,
    "优越代码伤害": 29.16,
    "最大装弹数": 85.37,
    "暴击率": 7.07,
    "暴击伤害": 20.36,
    "蓄力伤害": 14.63,
    "蓄力速度": 6.09,
    "攻击速度": 6.09,
    "防御力": 14.63,
    "最大体力": 14.63,
    "命中率": 14.63,
}

AFFIX_TIERS_DATA: dict[str, list[float]] = {
    "优越代码伤害": [9.54, 10.94, 12.34, 13.75, 15.15, 16.55, 17.95, 19.35, 20.75, 22.15, 23.56, 24.96, 26.36, 27.76, 29.16],
    "命中率": [4.77, 5.47, 6.18, 6.88, 7.59, 8.29, 9.00, 9.70, 10.40, 11.11, 11.81, 12.52, 13.22, 13.93, 14.63],
    "最大装弹数": [27.84, 31.95, 36.06, 40.17, 44.28, 48.39, 52.50, 56.60, 60.71, 64.82, 68.93, 73.04, 77.15, 81.26, 85.37],
    "攻击力": [4.77, 5.47, 6.18, 6.88, 7.59, 8.29, 9.00, 9.70, 10.40, 11.11, 11.81, 12.52, 13.22, 13.93, 14.63],
    "防御力": [4.77, 5.47, 6.18, 6.88, 7.59, 8.29, 9.00, 9.70, 10.40, 11.11, 11.81, 12.52, 13.22, 13.93, 14.63],
    "蓄力伤害": [4.77, 5.47, 6.18, 6.88, 7.59, 8.29, 9.00, 9.70, 10.40, 11.11, 11.81, 12.52, 13.22, 13.93, 14.63],
    "蓄力速度": [1.98, 2.28, 2.57, 2.86, 3.16, 3.54, 3.75, 4.04, 4.33, 4.63, 4.92, 5.21, 5.51, 5.80, 6.09],
    "暴击率": [2.30, 2.64, 2.98, 3.32, 3.66, 4.00, 4.35, 4.69, 5.03, 5.37, 5.70, 6.05, 6.39, 6.73, 7.07],
    "暴击伤害": [6.64, 7.62, 8.60, 9.58, 10.56, 11.54, 12.52, 13.50, 14.48, 15.46, 16.44, 17.42, 18.40, 19.38, 20.36],
}


def affix_tier_table(
    affix: str, scale: dict[str, float] | None = None
) -> list[float]:
    """Return the 15 tier values for one affix.

    Affixes without a dedicated table (攻击速度 / 最大体力) fall back to the
    table whose maximum matches this affix's scale, so every option offered by
    the dashboard can be expressed as a tier.
    """
    canonical = canonical_affix(affix)
    table = AFFIX_TIERS_DATA.get(canonical)
    if table:
        return table
    max_value = (scale or DEFAULT_AFFIX_SCALE).get(canonical)
    if max_value:
        for candidate in AFFIX_TIERS_DATA.values():
            if candidate and abs(candidate[-1] - float(max_value)) <= 0.011:
                return candidate
    return []


def get_affix_tier(affix: str, value: float) -> int | None:
    if not value or value <= 0:
        return None
    canonical = canonical_affix(affix)
    tiers = affix_tier_table(canonical)
    if not tiers:
        return None
    best_tier = 1
    min_diff = float("inf")
    for idx, t_val in enumerate(tiers, start=1):
        diff = abs(value - t_val)
        if diff < min_diff:
            min_diff = diff
            best_tier = idx
    return best_tier


def format_affix_tier(tier: int | None) -> str:
    if not tier:
        return ""
    if tier >= 13:
        return f"{tier}阶·黑底"
    elif tier >= 10:
        return f"{tier}阶·蓝标"
    else:
        return f"{tier}阶"



# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def profile_dir() -> Path:
    """Hermes profile root that owns this plugin.

    The plugin is installed at `<profile>/plugins/<name>/`, so its physical
    location is the most reliable signal; env and Hermes helpers are fallbacks.
    `NIKKE_PROFILE_DIR` overrides everything for tests/portability.
    """
    override = os.environ.get("NIKKE_PROFILE_DIR")
    if override:
        return Path(override).expanduser()
    for candidate in (PLUGIN_DIR, *PLUGIN_DIR.parents):
        if (candidate / "distribution.yaml").exists() or (
            candidate / "config.yaml"
        ).exists():
            return candidate.resolve()
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home()).resolve()
    except Exception:
        pass
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home).expanduser()
    return PLUGIN_DIR.parents[1]


def data_dir() -> Path:
    override = os.environ.get("NIKKE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return profile_dir() / "data" / "nikke"


def progress_path() -> Path:
    return data_dir() / "player_progress.json"


def targets_path() -> Path:
    override = os.environ.get("NIKKE_TARGETS_PATH")
    if override:
        return Path(override).expanduser()
    return data_dir() / "targets.json"


def ensure_targets() -> Path:
    """Seed the writable targets copy from the bundled baseline when missing.

    Never overwrites an existing file: local calibration always wins.  If the
    profile is read-only, fall back to reading the bundled file directly.
    """
    path = targets_path()
    if path.is_file() or not BUNDLED_TARGETS.is_file():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        shutil.copyfile(BUNDLED_TARGETS, tmp)
        os.replace(tmp, path)
    except OSError:
        return BUNDLED_TARGETS
    return path


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------
def canonical_affix(name: Any) -> str:
    """Map '优越代码伤害增加' / '装弹数' / '爆伤' to one canonical key."""
    if name is None:
        return ""
    text = str(name).strip().replace(" ", "").replace("\u3000", "")
    for suffix in ("增加", "提升", "提高", "+"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    text = text.rstrip("%％")
    return AFFIX_ALIASES.get(text, text)


def normalize_slot(slot: str) -> str:
    key = str(slot).strip().lower()
    if key in SLOT_ALIASES:
        return SLOT_ALIASES[key]
    raise ValueError(f"未知部位: {slot!r}（可用: head/body/arm/leg）")


def parse_number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace("％", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_skills(value: Any) -> list[int]:
    """Accept '10/10/10', '10,10,10', [10, 10, 10] or '7-10-10'."""
    if isinstance(value, (list, tuple)):
        parts = list(value)
    elif isinstance(value, (int, float)):
        parts = [int(value)] * 3
    else:
        text = str(value).strip()
        for sep in ("/", ",", "-", "|", " "):
            if sep in text:
                parts = [p for p in text.split(sep) if p.strip()]
                break
        else:
            parts = [text]
    if len(parts) != 3:
        raise ValueError(f"技能等级需要 3 个数值，收到: {value!r}")
    levels = []
    for part in parts:
        level = int(float(str(part).strip()))
        if not 1 <= level <= 10:
            raise ValueError(f"技能等级必须在 1-10 之间，收到: {level}")
        levels.append(level)
    return levels


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------
_TARGET_CACHE: tuple[tuple[str, int, int], dict[str, Any]] | None = None
_INDEX_CACHE: tuple[dict[str, Any], dict[str, dict[str, Any]]] | None = None


def invalidate_targets_cache() -> None:
    global _TARGET_CACHE, _INDEX_CACHE
    _TARGET_CACHE = None
    _INDEX_CACHE = None


def load_targets(path: Path | None = None) -> dict[str, Any]:
    global _TARGET_CACHE
    p = Path(path) if path else ensure_targets()
    try:
        stat = p.stat()
        key = (str(p.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (str(p), 0, 0)
    if _TARGET_CACHE is not None and _TARGET_CACHE[0] == key:
        return _TARGET_CACHE[1]
    with p.open("r", encoding="utf-8") as fh:
        doc = json.load(fh)
    chars = doc.get("characters")
    if not isinstance(chars, list) or not chars:
        raise ValueError(f"{p} 缺少非空的 characters 数组")
    seen: set[str] = set()
    for entry in chars:
        name = entry.get("name")
        if not name:
            raise ValueError(f"{p} 中存在缺少 name 的角色")
        if name in seen:
            raise ValueError(f"{p} 中角色重名: {name}")
        seen.add(name)
        for key in ("tier", "element", "class", "burst", "skills", "affix_priority", "cube"):
            if key not in entry:
                raise ValueError(f"{name} 缺少字段 {key}")
    doc.setdefault("affix_scale", DEFAULT_AFFIX_SCALE)
    _TARGET_CACHE = (key, doc)
    return doc


def target_index(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    global _INDEX_CACHE
    if _INDEX_CACHE is not None and _INDEX_CACHE[0] is doc:
        return _INDEX_CACHE[1]
    index: dict[str, dict[str, Any]] = {}
    for entry in doc["characters"]:
        keys = {entry["name"], entry.get("en", "")}
        keys.update(entry.get("aliases", []) or [])
        for key in keys:
            if key:
                index.setdefault(key.strip().lower(), entry)
                index.setdefault(key.strip(), entry)
    _INDEX_CACHE = (doc, index)
    return index


def resolve_target(name: str, doc: dict[str, Any]) -> dict[str, Any]:
    """Exact name/en/alias first, then unique substring match."""
    if not name:
        raise KeyError("角色名不能为空")
    index = target_index(doc)
    if name in index:
        return index[name]
    if name.strip().lower() in index:
        return index[name.strip().lower()]
    needle = name.strip().lower()
    hits = [e for e in doc["characters"] if needle in e["name"].lower() or needle in str(e.get("en", "")).lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        names = "、".join(e["name"] for e in hits[:8])
        raise KeyError(f"角色名 {name!r} 不唯一: {names}")
    raise KeyError(f"目标库中没有角色 {name!r}")


def affix_scale(doc: dict[str, Any]) -> dict[str, float]:
    scale = dict(DEFAULT_AFFIX_SCALE)
    scale.update(doc.get("affix_scale") or {})
    return scale


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------
def empty_slots() -> list[dict[str, Any]]:
    return [{"affix": "", "value": 0.0, "locked": False} for _ in range(3)]


def empty_gear() -> dict[str, Any]:
    return {slot: {"level": 0, "slots": empty_slots()} for slot in SLOTS}


def new_character(name: str, target: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "skills": [1, 1, 1],
        "collectible": (target or {}).get("collectible", ""),
        "gear": empty_gear(),
        "notes": "",
    }


def default_progress() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "characters": {},
    }


def load_progress(path: Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else progress_path()
    if not p.exists():
        return default_progress()
    with p.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{p} 不是合法的进度文件")
    data.setdefault("schema_version", 2)
    data.setdefault("characters", {})
    for entry in data["characters"].values():
        entry.setdefault("skills", [1, 1, 1])
        entry.setdefault("collectible", "")
        entry.setdefault("notes", "")
        gear = entry.setdefault("gear", {})
        for slot in SLOTS:
            piece = gear.setdefault(slot, {"level": 0, "slots": empty_slots()})
            piece.setdefault("level", 0)
            slots = piece.setdefault("slots", empty_slots())
            while len(slots) < 3:
                slots.append({"affix": "", "value": 0.0, "locked": False})
            del slots[3:]
            for item in slots:
                item.setdefault("affix", "")
                item.setdefault("value", 0.0)
                item.setdefault("locked", False)
    return data


def save_progress(data: dict[str, Any], path: Path | None = None) -> Path:
    p = Path(path) if path else progress_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, p)
    return p


def ensure_character(progress: dict[str, Any], name: str, target: dict[str, Any] | None = None) -> dict[str, Any]:
    chars = progress.setdefault("characters", {})
    if name not in chars:
        chars[name] = new_character(name, target)
    return chars[name]


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------
def _slot_payload(item: Any) -> dict[str, Any]:
    if isinstance(item, str):
        text = item.strip()
        for sep in (":", "：", "="):
            if sep in text:
                head, _, tail = text.partition(sep)
                return {"affix": canonical_affix(head), "value": parse_number(tail), "locked": False}
        # '攻击力 14.63'
        bits = text.split()
        if len(bits) >= 2:
            return {"affix": canonical_affix(" ".join(bits[:-1])), "value": parse_number(bits[-1]), "locked": False}
        return {"affix": canonical_affix(text), "value": 0.0, "locked": False}
    if isinstance(item, dict):
        affix = canonical_affix(item.get("affix", item.get("name", "")))
        return {
            "affix": affix,
            "value": parse_number(item.get("value", item.get("roll", 0))),
            "locked": bool(item.get("locked", item.get("is_locked", False))),
        }
    raise ValueError(f"无法解析词条: {item!r}")


def parse_affixes(value: Any) -> list[dict[str, Any]] | None:
    """Parse JSON, a ';'-separated string, or a list into exactly 3 slot dicts."""
    if value is None:
        return None
    items: Any
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return empty_slots()
        if text[0] in "[{":
            items = json.loads(text)
        else:
            items = [part for part in text.replace("|", ";").split(";") if part.strip()]
    else:
        items = value

    if isinstance(items, dict):
        if "affix" in items or "name" in items:
            items = [items]
        else:
            ordered = []
            for idx in (1, 2, 3):
                for key in (idx, str(idx), f"slot{idx}", f"slot_{idx}"):
                    if key in items:
                        ordered.append(items[key])
                        break
                else:
                    ordered.append("")
            items = ordered

    slots = [_slot_payload(item) for item in list(items)]
    if len(slots) > 3:
        raise ValueError("每件装备最多 3 个词条槽位")
    while len(slots) < 3:
        slots.append({"affix": "", "value": 0.0, "locked": False})
    return slots


def update_gear(
    name: str,
    slot: str,
    level: int | None = None,
    affixes: Any = None,
    locks: Iterable[int] | None = None,
    progress: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update one piece; unspecified fields keep their previous values."""
    progress = progress if progress is not None else load_progress()
    char = ensure_character(progress, name, target)
    key = normalize_slot(slot)
    piece = char["gear"][key]

    if level is not None:
        lvl = int(level)
        if not 0 <= lvl <= 5:
            raise ValueError(f"装备等级必须在 0-5 之间，收到: {lvl}")
        piece["level"] = lvl

    parsed = parse_affixes(affixes)
    if parsed is not None:
        previous = piece.get("slots") or []
        if locks is None:
            # A value-only edit must not silently drop an existing lock.
            for idx, item in enumerate(parsed):
                same_affix = idx < len(previous) and (
                    canonical_affix(previous[idx].get("affix", "")) == item["affix"]
                )
                if same_affix and previous[idx].get("locked"):
                    item["locked"] = True
        piece["slots"] = parsed

    if locks is not None:
        wanted = {int(x) for x in locks}
        for idx, item in enumerate(piece["slots"], start=1):
            if idx in wanted:
                item["locked"] = True
            elif parsed is None:
                item["locked"] = False
    return piece


def update_skills(
    name: str,
    skills: Any,
    progress: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    progress = progress if progress is not None else load_progress()
    char = ensure_character(progress, name, target)
    char["skills"] = parse_skills(skills)
    return char


def update_meta(
    name: str,
    collectible: str | None = None,
    notes: str | None = None,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    progress = progress if progress is not None else load_progress()
    char = ensure_character(progress, name)
    if collectible is not None:
        char["collectible"] = collectible
    if notes is not None:
        char["notes"] = notes
    return char


# ---------------------------------------------------------------------------
# demo fixture (the real screenshot data supplied by the user)
# ---------------------------------------------------------------------------
def demo_entry() -> dict[str, Any]:
    return {
        "name": "小红帽",
        "skills": [7, 10, 10],
        "cube": "堡垒",
        "collectible": "SSR",
        "gear": {
            "head": {
                "level": 5,
                "slots": [
                    {"affix": "优越代码伤害", "value": 24.96, "locked": False},
                    {"affix": "暴击率", "value": 2.30, "locked": False},
                    {"affix": "攻击力", "value": 14.63, "locked": True},
                ],
            },
            "body": {
                "level": 5,
                "slots": [
                    {"affix": "优越代码伤害", "value": 29.16, "locked": True},
                    {"affix": "攻击力", "value": 13.93, "locked": False},
                    {"affix": "暴击伤害", "value": 15.46, "locked": False},
                ],
            },
            "arm": {
                "level": 5,
                "slots": [
                    {"affix": "暴击率", "value": 4.35, "locked": False},
                    {"affix": "蓄力伤害", "value": 11.11, "locked": False},
                    {"affix": "优越代码伤害", "value": 29.16, "locked": True},
                ],
            },
            "leg": {
                "level": 1,
                "slots": [
                    {"affix": "优越代码伤害", "value": 22.15, "locked": False},
                    {"affix": "最大装弹数", "value": 85.37, "locked": True},
                    {"affix": "暴击伤害", "value": 12.52, "locked": False},
                ],
            },
        },
        "notes": "朝圣者火力型，演示数据来自用户截图。",
    }
