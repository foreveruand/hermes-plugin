#!/usr/bin/env python3
"""Core logic for the NIKKE dashboard plugin.

All progression data stays in the existing skill's player_progress.json.
This module resolves the skill, validates requests, mutates through the
skill's helpers, and shapes JSON for the browser.  No FastAPI imports here,
so the logic can be tested without the dashboard environment.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

SLOTS = ("head", "body", "arm", "leg")
SLOT_LABELS = {"head": "头部", "body": "身躯", "arm": "臂部", "leg": "腿部"}
STATE_FILE = "dashboard_state.json"
NOTE_REL = "游戏与联机/nikke.md"


# ---------------------------------------------------------------------------
# errors -> HTTP status in plugin_api
# ---------------------------------------------------------------------------
class PluginError(Exception):
    status_code = 400


class NotFound(PluginError):
    status_code = 404


class Conflict(PluginError):
    status_code = 409


class ServiceUnavailable(PluginError):
    status_code = 503


# ---------------------------------------------------------------------------
# bundled library (dashboard/nikke_lib) — no skill dependency
# ---------------------------------------------------------------------------
_DASHBOARD_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = _DASHBOARD_DIR.parent
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))

from nikke_lib import nikke_advisor as advisor  # noqa: E402
from nikke_lib import nikke_db as db  # noqa: E402
from nikke_lib import nikke_obsidian as obsidian  # noqa: E402

MODULES = SimpleNamespace(db=db, advisor=advisor, obsidian=obsidian, root=PLUGIN_ROOT)
_PORTRAIT_LOCK = threading.Lock()
_TARGETS_LOCK = threading.Lock()
_PROCESS_LOCK = threading.RLock()


def modules() -> SimpleNamespace:
    return MODULES


# ---------------------------------------------------------------------------
# paths / small helpers
# ---------------------------------------------------------------------------
def profile_dir() -> Path:
    return modules().db.profile_dir()


def data_dir() -> Path:
    return modules().db.data_dir()


def progress_path() -> Path:
    return modules().db.progress_path()


def targets_path() -> Path:
    return modules().db.targets_path()


def note_path() -> Path:
    return modules().obsidian.note_path()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return ""


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:  # noqa: BLE001 - surfaced to the API
        raise RuntimeError(f"读取 {path.name} 失败: {exc}") from exc


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _round(value: Any, digits: int = 4) -> Any:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return value


# ---------------------------------------------------------------------------
# state (favorites) — kept out of player_progress.json
# ---------------------------------------------------------------------------
def load_state() -> dict[str, Any]:
    raw = _read_json(data_dir() / STATE_FILE, {})
    favorites = raw.get("favorites") if isinstance(raw, dict) else []
    if not isinstance(favorites, list):
        favorites = []
    return {
        "schema_version": 1,
        "updated": raw.get("updated", "") if isinstance(raw, dict) else "",
        "favorites": _dedupe(str(x) for x in favorites if isinstance(x, str) and x),
    }


def _save_state(state: dict[str, Any]) -> None:
    state["updated"] = _iso_now()
    _write_json(data_dir() / STATE_FILE, state)


# ---------------------------------------------------------------------------
# revision / write lock
# ---------------------------------------------------------------------------
_REV_CACHE: dict[Path, tuple[tuple[str, int, int], str]] = {}


def _file_revision(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return "0" * 12
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    hit = _REV_CACHE.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    value = hashlib.sha1(path.read_bytes()).hexdigest()[:12]
    _REV_CACHE[path] = (key, value)
    return value


def revision() -> str:
    return _file_revision(progress_path())


def targets_revision() -> str:
    return _file_revision(modules().db.ensure_targets())


@contextmanager
def write_lock(timeout: float = 10.0):
    """Serialise read-modify-write across threads and processes."""
    data_dir().mkdir(parents=True, exist_ok=True)
    lock_path = data_dir() / ".write.lock"
    with _PROCESS_LOCK:
        handle = lock_path.open("a+b")
        fcntl = None
        try:
            try:
                import fcntl as _fcntl  # type: ignore

                fcntl = _fcntl
            except ImportError:
                fcntl = None
            if fcntl is not None:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            raise ServiceUnavailable("写锁等待超时，请稍后重试")
                        time.sleep(0.05)
            yield
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------
def _normalise_gear(entry: dict[str, Any]) -> dict[str, Any]:
    gear = entry.get("gear") or {}
    out: dict[str, Any] = {}
    for slot in SLOTS:
        piece = gear.get(slot) or {}
        slots = list(piece.get("slots") or [])[:3]
        while len(slots) < 3:
            slots.append({"affix": "", "value": 0.0, "locked": False})
        out[slot] = {
            "level": int(piece.get("level", 0) or 0),
            "slots": [
                {
                    "affix": str(item.get("affix", "") or ""),
                    "value": _round(item.get("value", 0) or 0, 2),
                    "locked": bool(item.get("locked", False)),
                }
                for item in slots
            ],
        }
    return out


def _entry_map(progress: dict[str, Any], doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map canonical target name -> progress entry (aliases collapse)."""
    db = modules().db
    chars = progress.get("characters") or {}
    out: dict[str, dict[str, Any]] = {}
    for key, entry in chars.items():
        try:
            target = db.resolve_target(key, doc)
        except Exception:
            continue
        name = target["name"]
        if name not in out or key == name:
            out[name] = entry
    return out


def _find_entry(
    progress: dict[str, Any], doc: dict[str, Any], target: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    db = modules().db
    chars = progress.get("characters") or {}
    name = target["name"]
    if name in chars:
        return name, chars[name]
    for key, entry in chars.items():
        try:
            resolved = db.resolve_target(key, doc)
        except Exception:
            continue
        if resolved["name"] == name:
            return key, entry
    return None, None


def _canonicalise_entry_key(
    progress: dict[str, Any], actual: str | None, target: dict[str, Any]
) -> str:
    """Move an alias-keyed entry to the canonical target name; return the key."""
    name = target["name"]
    chars = progress.setdefault("characters", {})
    if actual and actual != name and actual in chars:
        chars[name] = chars.pop(actual)
        return name
    return name


# ---------------------------------------------------------------------------
# payload shaping
# ---------------------------------------------------------------------------
def affix_meta(doc: dict[str, Any]) -> dict[str, Any]:
    db = modules().db
    scale = db.affix_scale(doc)
    out: dict[str, Any] = {}
    for affix, max_value in scale.items():
        table = db.affix_tier_table(affix, scale)
        out[affix] = {
            "max": _round(max_value, 2),
            "tiers": [_round(x, 2) for x in table] if table else [],
        }
    return out


def _affix_options(doc: dict[str, Any]) -> list[str]:
    return list(modules().db.affix_scale(doc).keys())


def _core_text(note: str) -> str:
    if not note:
        return ""
    match = re.search(r"核心词条\s*[:：]\s*([^|]+)", note)
    if match:
        return match.group(1).strip()
    if "核心词条无特殊要求" in note:
        return "无要求"
    return ""


def requirement_info(target: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """Read the authoritative core-affix requirement from the target note.

    The target library's affix_priority/affix_targets are generic defaults and
    must not be shown as recommendations.  The note's 核心词条 line is the
    source of truth; a missing line is reported as unknown, not filled with
    defaults.
    """
    db = modules().db
    scale = db.affix_scale(doc)

    # Explicit fields from the latest one-image guide win over note parsing.
    explicit_main = target.get("affix_main")
    explicit_sub = target.get("affix_sub")
    if isinstance(explicit_main, list) or isinstance(explicit_sub, list):
        main_list = [
            {"affix": affix, "text": affix}
            for affix in (explicit_main or [])
            if affix in scale
        ]
        sub_list = [
            {"affix": affix, "text": affix}
            for affix in (explicit_sub or [])
            if affix in scale
        ]
        return {
            "text": target.get("affix_note") or "目标库已核对主/副词条",
            "affixes": main_list,
            "sub_affixes": sub_list,
            "no_requirement": not main_list and not sub_list,
            "known": True,
        }

    text = _core_text(target.get("note", ""))
    if not text:
        return {
            "text": "目标库未注明核心词条",
            "affixes": [],
            "sub_affixes": [],
            "no_requirement": False,
            "known": False,
        }
    if "无要求" in text or text.strip() in ("无", "无特殊要求"):
        return {
            "text": "核心词条无特殊要求（一图流）",
            "affixes": [],
            "sub_affixes": [],
            "no_requirement": True,
            "known": True,
        }
    parts = [part for part in re.split(r"[/、,，]", text) if part.strip()]
    affixes: list[dict[str, str]] = []
    seen: set[str] = set()
    for part in parts:
        clean = re.sub(r"[（(].*?[）)]", "", part).strip()
        clean = re.sub(r"(增加|提升|提高|\+)$", "", clean).strip()
        key = db.canonical_affix(clean)
        if key not in scale:
            key = next((affix for affix in scale if affix in clean), "")
        if not key:
            for alias, canonical in (
                ("优越", "优越代码伤害"),
                ("装弹", "最大装弹数"),
                ("爆伤", "暴击伤害"),
                ("暴击", "暴击率"),
            ):
                if alias in clean:
                    key = canonical
                    break
        if key and key in scale and key not in seen:
            seen.add(key)
            affixes.append({"affix": key, "text": part.strip()})
    return {
        "text": text,
        "affixes": affixes,
        "sub_affixes": [],
        "no_requirement": False,
        "known": True,
    }


def public_target(target: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """Target payload for the browser; removes generic affix defaults."""
    out = dict(target)
    out["requirement"] = requirement_info(target, doc)
    out["guide_date"] = doc.get("guide_date", "")
    for key in ("affix_priority", "affix_targets", "affix_targets_max"):
        out.pop(key, None)
    return out


def _slot_meta() -> list[dict[str, str]]:
    return [{"key": slot, "label": SLOT_LABELS[slot]} for slot in SLOTS]


def _trim_diag(diag: dict[str, Any]) -> dict[str, Any]:
    pieces: list[dict[str, Any]] = []
    for piece in diag.get("pieces", []):
        slots = []
        for item in piece.get("slots", []):
            slots.append(
                {
                    "index": item.get("index"),
                    "affix": item.get("affix", ""),
                    "value": _round(item.get("value", 0), 2),
                    "locked": bool(item.get("locked")),
                    "quality": _round(item.get("quality", 0), 4),
                    "tier": item.get("tier"),
                    "tier_str": item.get("tier_str", ""),
                    "matched": bool(item.get("matched")),
                    "effective": bool(item.get("effective")),
                    "good": bool(item.get("good")),
                }
            )
        pieces.append(
            {
                "slot": piece.get("slot"),
                "label": piece.get("label", ""),
                "level": piece.get("level", 0),
                "score": _round(piece.get("score", 0)),
                "good": piece.get("good", []),
                "bad": piece.get("bad", []),
                "locked": piece.get("locked", []),
                "effective": piece.get("effective", 0),
                "remaining_cost": _round(piece.get("remaining_cost", 0), 2),
                "steps": piece.get("steps", []),
                "issues": piece.get("issues", []),
                "slots": slots,
            }
        )
    return {
        "score": _round(diag.get("score", 0)),
        "grade": diag.get("grade", ""),
        "effective": diag.get("effective", 0),
        "target_total": diag.get("target_total", 0),
        "max_total": diag.get("max_total", 0),
        "core_summary": diag.get("core_summary", ""),
        "counts": diag.get("counts", {}),
        "skills": diag.get("skills", {}),
        "actions": diag.get("actions", []),
        "priority": diag.get("priority", ""),
        "overflow": diag.get("overflow", []),
        "pieces": pieces,
    }


def _character(
    target: dict[str, Any], entry: dict[str, Any] | None
) -> dict[str, Any]:
    owned = entry is not None
    return {
        "name": target["name"],
        "en": target.get("en", ""),
        "tier": target.get("tier", ""),
        "element": target.get("element", ""),
        "class": target.get("class", ""),
        "burst": target.get("burst", ""),
        "weapon": target.get("weapon", ""),
        "role": target.get("role", ""),
        "owned": owned,
        "skills": list(entry.get("skills") or [1, 1, 1]) if owned else None,
        "cube": target.get("cube", ""),
        "collectible": entry.get("collectible", "") if owned else "",
        "notes": entry.get("notes", "") if owned else "",
        "gear": _normalise_gear(entry) if owned else None,
        "gear_levels": (
            {slot: int((entry.get("gear") or {}).get(slot, {}).get("level", 0) or 0) for slot in SLOTS}
            if owned
            else None
        ),
    }


# ---------------------------------------------------------------------------
# portraits
# ---------------------------------------------------------------------------
def portrait_dir() -> Path:
    return data_dir() / "portraits"


def portrait_key(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]


def load_portrait_index() -> dict[str, Any]:
    raw = _read_json(portrait_dir() / "index.json", {})
    items = raw.get("items") if isinstance(raw, dict) else None
    return items if isinstance(items, dict) else {}


def portrait_info(name: str, index: dict[str, Any] | None = None) -> dict[str, Any]:
    key = portrait_key(name)
    item = (index if index is not None else load_portrait_index()).get(key) or {}
    rel = item.get("file")
    has_cache = False
    version = 0
    if isinstance(rel, str) and rel and Path(rel).name == rel:
        path = portrait_dir() / rel
        if path.is_file():
            has_cache = True
            try:
                version = int(path.stat().st_mtime_ns)
            except OSError:
                version = 0
    return {
        "key": key,
        "has_cache": has_cache,
        "source": item.get("source", ""),
        "version": version,
    }


def fetch_portrait(name: str) -> dict[str, Any]:
    """Resolve and cache one portrait on demand.

    The resolver script runs with the system Python because the dashboard's
    runtime does not ship requests/Pillow.  It is only invoked for a missing
    image; cached images return immediately.  The child must not inherit the
    dashboard's PYTHONPATH: it points at the host's Python 3.14 site-packages,
    which makes the system Python load a Pillow C extension built for a
    different ABI ("cannot import name '_imaging' from 'PIL'").
    """
    db = modules().db
    doc = db.load_targets()
    try:
        target = db.resolve_target(name, doc)
    except KeyError as exc:
        raise NotFound(str(exc)) from exc
    canonical = target["name"]
    info = portrait_info(canonical)
    if info.get("has_cache"):
        return {"ok": True, "name": canonical, "fetched": False, "portrait": info}

    script = Path(__file__).resolve().parents[1] / "scripts" / "resolve_portraits.py"
    if not script.is_file():
        raise ServiceUnavailable("找不到头像抓取脚本 resolve_portraits.py")
    python = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else (shutil.which("python3") or sys.executable)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"

    with _PORTRAIT_LOCK:
        info = portrait_info(canonical)
        if info.get("has_cache"):
            return {"ok": True, "name": canonical, "fetched": False, "portrait": info}
        try:
            proc = subprocess.run(
                [python, "-I", str(script), "--only", canonical, "--force"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(script.parent),
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ServiceUnavailable("头像抓取超时（60 秒）") from exc
        except OSError as exc:
            raise ServiceUnavailable(f"头像抓取脚本无法启动: {exc}") from exc
        if proc.returncode != 0:
            detail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-400:]
            raise ServiceUnavailable(f"头像抓取失败: {detail}")

    info = portrait_info(canonical)
    fetched = bool(info.get("has_cache"))
    result = {"ok": True, "name": canonical, "fetched": fetched, "portrait": info}
    if not fetched:
        detail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-300:]
        if detail:
            result["reason"] = detail
    return result


def save_portrait(name: str, data_url: str) -> dict[str, Any]:
    """Save a user-uploaded portrait (PNG/JPEG/WebP data URL) as the override."""
    if not isinstance(data_url, str):
        raise ValueError("缺少图片数据")
    match = re.match(r"^data:image/(png|jpe?g|webp);base64,(.+)$", data_url.strip(), re.I | re.S)
    if not match:
        raise ValueError("只支持 PNG / JPEG / WebP 图片")
    subtype = match.group(1).lower()
    mime = "image/jpeg" if subtype in ("jpg", "jpeg") else "image/" + subtype
    ext = "jpg" if mime == "image/jpeg" else subtype
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise ValueError("图片 base64 解码失败") from exc
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("图片不能超过 8MB")
    is_png = raw.startswith(b"\x89PNG\r\n\x1a\n")
    is_jpg = raw.startswith(b"\xff\xd8\xff")
    is_webp = raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    if not (is_png or is_jpg or is_webp):
        raise ValueError("文件内容不是有效图片")

    db = modules().db
    doc = db.load_targets()
    try:
        target = db.resolve_target(name, doc)
    except KeyError as exc:
        raise NotFound(str(exc)) from exc
    canonical = target["name"]
    key = portrait_key(canonical)
    with _PORTRAIT_LOCK:
        portrait_dir().mkdir(parents=True, exist_ok=True)
        path = portrait_dir() / (key + "." + ext)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, path)
        index = load_portrait_index()
        index[key] = {
            "name": canonical,
            "file": path.name,
            "mime": mime,
            "source": "manual",
            "uploaded_at": _iso_now(),
            "width": 0,
            "height": 0,
        }
        _write_json(
            portrait_dir() / "index.json",
            {"schema_version": 1, "updated": _iso_now(), "items": index},
        )
    return {"ok": True, "name": canonical, "portrait": portrait_info(canonical)}


def portrait_file(key: str) -> tuple[Path, str]:
    if not re.fullmatch(r"[0-9a-f]{16,32}", key or ""):
        raise NotFound("头像不存在")
    item = load_portrait_index().get(key)
    if not isinstance(item, dict):
        raise NotFound("头像不存在")
    rel = item.get("file", "")
    if not isinstance(rel, str) or not rel or Path(rel).name != rel:
        raise NotFound("头像不存在")
    base = portrait_dir().resolve()
    path = (base / rel).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise NotFound("头像不存在") from exc
    if not path.is_file():
        raise NotFound("头像不存在")
    return path, str(item.get("mime") or "image/webp")


# ---------------------------------------------------------------------------
# bootstrap / detail
# ---------------------------------------------------------------------------
def _note_info() -> dict[str, Any]:
    try:
        path = note_path()
        return {"path": NOTE_REL, "mtime": path.stat().st_mtime if path.is_file() else 0, "ok": path.is_file()}
    except Exception:
        return {"path": NOTE_REL, "mtime": 0, "ok": False}


def bootstrap() -> dict[str, Any]:
    db = modules().db
    advisor = modules().advisor
    doc = db.load_targets()
    progress = db.load_progress()
    state = load_state()
    scale = db.affix_scale(doc)
    entries = _entry_map(progress, doc)
    favorites = set(state["favorites"])
    portrait_index = load_portrait_index()

    characters: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    for target in doc.get("characters", []):
        name = target["name"]
        entry = entries.get(name)
        diag = advisor.diagnose(entry, target, scale) if entry else None
        item: dict[str, Any] = {
            "name": name,
            "en": target.get("en", ""),
            "tier": target.get("tier", ""),
            "element": target.get("element", ""),
            "class": target.get("class", ""),
            "burst": target.get("burst", ""),
            "weapon": target.get("weapon", ""),
            "role": target.get("role", ""),
            "owned": entry is not None,
            "favorite": name in favorites,
            "score": _round(diag.get("score", 0)) if diag else None,
            "grade": diag.get("grade", "") if diag else "",
            "effective": diag.get("effective", 0) if diag else 0,
            "target_total": diag.get("target_total", 0) if diag else 0,
            "max_total": diag.get("max_total", 0) if diag else 0,
            "core_summary": diag.get("core_summary", "") if diag else "",
            "priority": diag.get("priority", "") if diag else "",
            "skills": list(entry.get("skills") or [1, 1, 1]) if entry else None,
            "gear_levels": (
                {slot: int((entry.get("gear") or {}).get(slot, {}).get("level", 0) or 0) for slot in SLOTS}
                if entry
                else None
            ),
            "portrait": portrait_info(name, portrait_index),
        }
        characters.append(item)

    known = {c["name"] for c in characters}
    for key in (progress.get("characters") or {}):
        if key not in known:
            try:
                db.resolve_target(key, doc)
            except KeyError:
                warnings.append({"type": "unresolved", "name": key})

    scores = [c["score"] for c in characters if c["score"] is not None]
    return {
        "schema": 1,
        "rev": revision(),
        "targets_rev": targets_revision(),
        "generated_at": _iso_now(),
        "counts": {
            "targets": len(characters),
            "owned": len(entries),
            "favorites": len(favorites),
        },
        "average_score": _round(sum(scores) / len(scores)) if scores else 0,
        "guide_date": doc.get("guide_date", ""),
        "affix_meta": affix_meta(doc),
        "affix_options": _affix_options(doc),
        "slots": _slot_meta(),
        "characters": characters,
        "favorites": sorted(favorites),
        "note": _note_info(),
        "warnings": warnings,
    }


def _detail_payload(
    target: dict[str, Any],
    entry: dict[str, Any] | None,
    doc: dict[str, Any],
    *,
    rev: str,
    targets_rev: str,
    favorite: bool = False,
    include_static: bool = True,
) -> dict[str, Any]:
    """Build the browser payload from already-loaded objects."""
    scale = modules().db.affix_scale(doc)
    diag = modules().advisor.diagnose(entry, target, scale) if entry else None
    payload: dict[str, Any] = {
        "rev": rev,
        "targets_rev": targets_rev,
        "owned": entry is not None,
        "favorite": bool(favorite),
        "character": _character(target, entry),
        "target": public_target(target, doc),
        "diag": _trim_diag(diag) if diag else None,
    }
    if include_static:
        payload["affix_meta"] = affix_meta(doc)
        payload["affix_options"] = _affix_options(doc)
        payload["slots"] = _slot_meta()
    return payload


def detail(name: str) -> dict[str, Any]:
    db = modules().db
    doc = db.load_targets()
    try:
        target = db.resolve_target(name, doc)
    except KeyError as exc:
        raise NotFound(str(exc)) from exc
    progress = db.load_progress()
    state = load_state()
    _key, entry = _find_entry(progress, doc, target)
    return _detail_payload(
        target,
        entry,
        doc,
        rev=revision(),
        targets_rev=targets_revision(),
        favorite=target["name"] in set(state["favorites"]),
    )


# ---------------------------------------------------------------------------
# validation / mutations
# ---------------------------------------------------------------------------
def _validate_slots(
    rows: Any, scale: dict[str, float], slot_label: str = "装备"
) -> list[dict[str, Any]]:
    db = modules().db
    if not isinstance(rows, list) or len(rows) != 3:
        raise ValueError(f"{slot_label}必须提交 3 个词条槽位")
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        prefix = f"{slot_label}槽{index}"
        if not isinstance(row, dict):
            raise ValueError(f"{prefix}格式错误")
        affix = db.canonical_affix(row.get("affix", ""))
        try:
            value = float(row.get("value", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{prefix}数值不是数字") from exc
        if not math.isfinite(value):
            raise ValueError(f"{prefix}数值无效")
        if not affix:
            if abs(value) > 1e-9:
                raise ValueError(f"{prefix}没有词条时数值必须为 0")
            value = 0.0
        else:
            if affix not in scale:
                raise ValueError(f"{prefix}未知词条: {affix}")
            if value <= 0:
                raise ValueError(f"{prefix}「{affix}」数值必须大于 0")
            if value > float(scale[affix]) + 0.01:
                raise ValueError(
                    f"{prefix}「{affix}」数值 {value:g} 超过上限 {float(scale[affix]):g}"
                )
        out.append(
            {
                "affix": affix,
                "value": round(value, 2),
                "locked": bool(row.get("locked", False)),
            }
        )
    return out


def save_gear_batch(
    name: str, pieces: Any, rev: str | None = None
) -> dict[str, Any]:
    """Validate every submitted piece first, then write all of them once.

    One lock, one progress write, one diagnosis and one Markdown render — the
    dashboard uses this so editing four pieces costs a single save round-trip.
    """
    db = modules().db
    doc = db.load_targets()
    try:
        target = db.resolve_target(name, doc)
    except KeyError as exc:
        raise NotFound(str(exc)) from exc
    if not isinstance(pieces, dict) or not pieces:
        raise ValueError("至少提交一件装备")

    scale = db.affix_scale(doc)
    validated: dict[str, dict[str, Any]] = {}
    for raw_slot, piece in pieces.items():
        try:
            slot_key = db.normalize_slot(raw_slot)
        except ValueError as exc:
            raise ValueError(f"未知装备部位: {raw_slot}") from exc
        label = SLOT_LABELS[slot_key]
        if slot_key in validated:
            raise ValueError(f"重复的装备部位: {label}")
        if not isinstance(piece, dict):
            raise ValueError(f"{label}格式错误")
        try:
            level_value = int(piece.get("level", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}装备等级必须是 0-5 的整数") from exc
        if not 0 <= level_value <= 5:
            raise ValueError(f"{label}装备等级必须在 0-5 之间")
        validated[slot_key] = {
            "level": level_value,
            "slots": _validate_slots(piece.get("slots"), scale, label),
        }

    favorites = set(load_state()["favorites"])
    with write_lock():
        current = revision()
        if rev is not None and str(rev) != current:
            raise Conflict("数据已被其他页面更新，请刷新后重试")
        progress = db.load_progress()
        actual, entry = _find_entry(progress, doc, target)
        if entry is None:
            db.ensure_character(progress, target["name"], target)
            actual = target["name"]
        key = _canonicalise_entry_key(progress, actual, target)
        for slot_key, piece in validated.items():
            db.update_gear(
                key,
                slot_key,
                level=piece["level"],
                affixes=piece["slots"],
                progress=progress,
                target=target,
            )
            stored = progress["characters"][key]["gear"][slot_key]
            for index, row in enumerate(piece["slots"]):
                stored["slots"][index]["locked"] = row["locked"]
        db.save_progress(progress)
        new_rev = revision()
        entry = progress["characters"][key]

    render_result = render(progress, doc)
    payload = _detail_payload(
        target,
        entry,
        doc,
        rev=new_rev,
        targets_rev=targets_revision(),
        favorite=target["name"] in favorites,
    )
    payload["render"] = render_result
    return {"ok": True, **payload}


def save_gear(
    name: str, slot: str, level: Any, rows: Any, rev: str | None = None
) -> dict[str, Any]:
    """Single-piece wrapper kept for scripts and older cached bundles."""
    return save_gear_batch(name, {slot: {"level": level, "slots": rows}}, rev)


def _clean_text(value: Any, field: str, max_len: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field}必须是文本")
    text = value.replace("\x00", "").strip()
    if len(text) > max_len:
        raise ValueError(f"{field}最多 {max_len} 个字符")
    return text


def save_meta(name: str, body: dict[str, Any], rev: str | None = None) -> dict[str, Any]:
    db = modules().db
    doc = db.load_targets()
    try:
        target = db.resolve_target(name, doc)
    except KeyError as exc:
        raise NotFound(str(exc)) from exc
    body = dict(body)
    # Cube is free to swap and lives in the target baseline; ignore it for
    # older clients instead of failing the whole request.
    body.pop("cube", None)
    allowed = {"skills", "collectible", "notes"}
    unknown = set(body) - allowed
    if unknown:
        raise ValueError(f"不支持的字段: {', '.join(sorted(unknown))}")
    if not body:
        raise ValueError("没有要保存的内容")
    skills = db.parse_skills(body["skills"]) if "skills" in body else None
    collectible = _clean_text(body.get("collectible"), "收藏品", 100) if "collectible" in body else None
    notes = _clean_text(body.get("notes"), "备注", 2000) if "notes" in body else None
    favorites = set(load_state()["favorites"])

    with write_lock():
        current = revision()
        if rev is not None and str(rev) != current:
            raise Conflict("数据已被其他页面更新，请刷新后重试")
        progress = db.load_progress()
        actual, entry = _find_entry(progress, doc, target)
        if entry is None:
            db.ensure_character(progress, target["name"], target)
            actual = target["name"]
        key = _canonicalise_entry_key(progress, actual, target)
        if skills is not None:
            db.update_skills(key, skills, progress=progress, target=target)
        if collectible is not None or notes is not None:
            db.update_meta(key, collectible=collectible, notes=notes, progress=progress)
        db.save_progress(progress)
        new_rev = revision()
        entry = progress["characters"][key]

    render_result = render(progress, doc)
    payload = _detail_payload(
        target,
        entry,
        doc,
        rev=new_rev,
        targets_rev=targets_revision(),
        favorite=target["name"] in favorites,
    )
    payload["render"] = render_result
    return {"ok": True, **payload}


_SHORT_AFFIX = {
    "优越代码伤害": "优越",
    "攻击力": "攻击",
    "最大装弹数": "装弹",
    "暴击率": "暴击",
    "暴击伤害": "爆伤",
    "蓄力伤害": "蓄伤",
    "蓄力速度": "蓄速",
    "攻击速度": "攻速",
    "防御力": "防御",
    "最大体力": "体力",
    "命中率": "命中",
}


def _affix_list(value: Any, scale: dict[str, float], field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是数组")
    db = modules().db
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field} 只能包含词条名")
        affix = db.canonical_affix(item)
        if affix not in scale:
            raise ValueError(f"{field} 中未知词条: {item}")
        if affix not in out:
            out.append(affix)
    return out


def _derived_affix_note(main: list[str], sub: list[str]) -> str:
    def text(items: list[str]) -> str:
        return "/".join(_SHORT_AFFIX.get(item, item) for item in items) or "无"

    return "主: " + text(main) + " | 副: " + text(sub)


def save_target(name: str, body: dict[str, Any], targets_rev: str | None = None) -> dict[str, Any]:
    """Manually correct a target's tier/skills/main-sub affixes and note."""
    db = modules().db
    with _TARGETS_LOCK:
        doc = db.load_targets()
        current = targets_revision()
        if targets_rev is not None and str(targets_rev) != current:
            raise Conflict("目标库已被其他页面更新，请刷新后重试")
        try:
            target = db.resolve_target(name, doc)
        except KeyError as exc:
            raise NotFound(str(exc)) from exc

        tier = str(body.get("tier", target.get("tier", ""))).strip().upper()
        if tier not in ("T0", "T1", "T2"):
            raise ValueError("梯度只能是 T0 / T1 / T2")
        skills = db.parse_skills(body["skills"]) if "skills" in body else list(target.get("skills") or [10, 10, 10])
        scale = db.affix_scale(doc)
        if "affix_main" in body:
            main = _affix_list(body.get("affix_main"), scale, "主词条")
        else:
            main = [affix for affix in (target.get("affix_main") or []) if affix in scale]
        if "affix_sub" in body:
            sub = _affix_list(body.get("affix_sub"), scale, "副词条")
        else:
            sub = [affix for affix in (target.get("affix_sub") or []) if affix in scale]
        sub = [affix for affix in sub if affix not in main]

        affix_note = _clean_text(body.get("affix_note"), "校核说明", 300) if "affix_note" in body else str(target.get("affix_note") or "")
        if not affix_note:
            affix_note = _derived_affix_note(main, sub)
        note = _clean_text(body.get("note"), "备注", 3000) if "note" in body else str(target.get("note") or "")

        target["tier"] = tier
        target["skills"] = skills
        target["affix_main"] = main
        target["affix_sub"] = sub
        target["affix_note"] = affix_note
        target["note"] = note
        # Keep the 8/12 target counts intact for the advisor's score and the
        # skill's schema; just make sure the corrected main/sub affixes are
        # included in the priority list.
        priority = list(target.get("affix_priority") or [])
        for affix in list((target.get("affix_targets") or {}).keys()) + main + sub:
            if affix not in priority:
                priority.append(affix)
        target["affix_priority"] = priority

        path = targets_path()
        try:
            data_dir().mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, data_dir() / "targets.json.bak")
        except OSError:
            pass
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        db.invalidate_targets_cache()
        new_rev = targets_revision()

    render_result = render()
    payload = detail(target["name"])
    payload["targets_rev"] = new_rev
    payload["render"] = render_result
    return {"ok": True, **payload}


def add_character(name: str, rev: str | None = None) -> dict[str, Any]:
    db = modules().db
    doc = db.load_targets()
    try:
        target = db.resolve_target(name, doc)
    except KeyError as exc:
        raise NotFound(str(exc)) from exc

    favorites = set(load_state()["favorites"])
    with write_lock():
        current = revision()
        if rev is not None and str(rev) != current:
            raise Conflict("数据已被其他页面更新，请刷新后重试")
        progress = db.load_progress()
        actual, entry = _find_entry(progress, doc, target)
        created = entry is None
        if created:
            db.ensure_character(progress, target["name"], target)
            db.save_progress(progress)
        elif actual != target["name"]:
            _canonicalise_entry_key(progress, actual, target)
            db.save_progress(progress)
        new_rev = revision()
        entry = progress["characters"][target["name"]]

    render_result = render(progress, doc)
    payload = _detail_payload(
        target,
        entry,
        doc,
        rev=new_rev,
        targets_rev=targets_revision(),
        favorite=target["name"] in favorites,
    )
    payload["render"] = render_result
    return {"ok": True, "created": created, **payload}


def remove_character(name: str, confirm: bool = False, rev: str | None = None) -> dict[str, Any]:
    if not confirm:
        raise ValueError("移出看板需要确认")
    db = modules().db
    doc = db.load_targets()
    try:
        target = db.resolve_target(name, doc)
    except KeyError as exc:
        raise NotFound(str(exc)) from exc

    with write_lock():
        current = revision()
        if rev is not None and str(rev) != current:
            raise Conflict("数据已被其他页面更新，请刷新后重试")
        progress = db.load_progress()
        actual, entry = _find_entry(progress, doc, target)
        if entry is None:
            raise NotFound("该角色不在看板中")
        progress["characters"].pop(actual or target["name"], None)
        db.save_progress(progress)
        state = load_state()
        state["favorites"] = [x for x in state["favorites"] if x != target["name"]]
        _save_state(state)
        new_rev = revision()

    render_result = render(progress, doc)
    return {
        "ok": True,
        "removed": target["name"],
        "rev": new_rev,
        "render": render_result,
    }


def set_favorites(names: Any) -> dict[str, Any]:
    db = modules().db
    doc = db.load_targets()
    if not isinstance(names, list):
        raise ValueError("favorites 必须是数组")
    if len(names) > 500:
        raise ValueError("收藏数量过多")
    out: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            target = db.resolve_target(name, doc)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        if target["name"] not in out:
            out.append(target["name"])
    with write_lock():
        state = load_state()
        state["favorites"] = out
        _save_state(state)
    return state


# ---------------------------------------------------------------------------
# rendering / teams
# ---------------------------------------------------------------------------
def _note_display_path() -> str:
    try:
        obsidian = modules().obsidian
        return f"{obsidian.NOTE_DIR}/{obsidian.NOTE_NAME}"
    except Exception:
        return NOTE_REL


def render(progress: dict[str, Any] | None = None, doc: dict[str, Any] | None = None) -> dict[str, Any]:
    db = modules().db
    obsidian = modules().obsidian
    display = _note_display_path()
    try:
        progress = progress if progress is not None else db.load_progress()
        doc = doc if doc is not None else db.load_targets()
        path = obsidian.note_path()
        text = obsidian.render_markdown(progress, doc)
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return {"ok": True, "path": display, "changed": False}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return {"ok": True, "path": display, "changed": True}
    except Exception as exc:  # noqa: BLE001 - render failure must not roll back the save
        return {"ok": False, "path": display, "changed": False, "error": str(exc)}


def _clean_md(text: str) -> str:
    text = text.replace("\\|", "|")
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\x60([^\x60]*)\x60", r"\1", text)
    return text.strip()


def parse_markdown_blocks(text: str) -> list[dict[str, Any]]:
    """Conservative Markdown -> blocks parser for the two team references."""
    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []
    i = 0

    def special(line: str) -> bool:
        s = line.strip()
        return not s or s.startswith("#") or s.startswith("|") or s.startswith("- ") or s.startswith("* ")

    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if s.startswith("#"):
            level = len(s) - len(s.lstrip("#"))
            blocks.append({"type": "heading", "level": min(level, 6), "text": _clean_md(s[level:].strip())})
            i += 1
            continue
        if s.startswith("|"):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                raw = lines[i].strip()
                cells = [_clean_md(cell) for cell in raw.strip("|").split("|")]
                rows.append(cells)
                i += 1
            if len(rows) >= 2 and all(
                re.fullmatch(r":?-{2,}:?", cell.replace(" ", "")) for cell in rows[1]
            ):
                headers, body = rows[0], rows[2:]
            else:
                headers, body = (rows[0] if rows else []), rows[1:]
            blocks.append({"type": "table", "headers": headers, "rows": body})
            continue
        if s.startswith("- ") or s.startswith("* "):
            items: list[str] = []
            while i < len(lines):
                t = lines[i].strip()
                if t.startswith("- ") or t.startswith("* "):
                    items.append(_clean_md(t[2:].strip()))
                    i += 1
                elif t and not t.startswith("#") and not t.startswith("|"):
                    if items:
                        items[-1] += " " + _clean_md(t)
                        i += 1
                    else:
                        break
                else:
                    break
            blocks.append({"type": "list", "items": items})
            continue
        parts = [s]
        i += 1
        while i < len(lines) and not special(lines[i]):
            parts.append(lines[i].strip())
            i += 1
        blocks.append({"type": "paragraph", "text": _clean_md(" ".join(parts))})
    return blocks


def teams() -> dict[str, Any]:
    refs = PLUGIN_ROOT / "references"
    out: dict[str, Any] = {}
    for key, filename in (
        ("interception", "interception-teams.md"),
        ("solo_raid", "solo-raid-teams.md"),
    ):
        path = refs / filename
        source = f"references/{filename}"
        if not path.is_file():
            out[key] = {
                "title": "",
                "source": source,
                "updated": "",
                "blocks": [],
                "raw": "",
            }
            continue
        text = path.read_text(encoding="utf-8")
        title = ""
        for line in text.splitlines():
            if line.startswith("# "):
                title = _clean_md(line[2:].strip())
                break
        out[key] = {
            "title": title,
            "source": source,
            "updated": _mtime_iso(path),
            "blocks": parse_markdown_blocks(text),
            "raw": text,
        }
    return out


def health() -> dict[str, Any]:
    try:
        db = modules().db
        doc = db.load_targets()
        progress = db.load_progress()
        return {
            "ok": True,
            "lib": str(Path(db.__file__).resolve().parent),
            "profile": str(profile_dir()),
            "data": str(data_dir()),
            "targets": len(doc.get("characters", [])),
            "guide_date": doc.get("guide_date", ""),
            "owned": len(progress.get("characters", {})),
            "rev": revision(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


__all__ = [
    "PluginError",
    "NotFound",
    "Conflict",
    "ServiceUnavailable",
    "add_character",
    "bootstrap",
    "detail",
    "fetch_portrait",
    "health",
    "load_state",
    "parse_markdown_blocks",
    "public_target",
    "requirement_info",
    "portrait_file",
    "remove_character",
    "render",
    "save_gear",
    "save_meta",
    "save_portrait",
    "save_target",
    "set_favorites",
    "targets_revision",
    "teams",
]