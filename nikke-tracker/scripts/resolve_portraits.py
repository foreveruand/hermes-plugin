#!/usr/bin/env python3
"""One-time portrait resolver for the NIKKE dashboard plugin.

Order:
  1. local override (data/nikke/portraits/overrides/)
  2. curated Prydwen slug (dashboard/portrait_map.json)
  3. BiliGame Chinese character page
  4. Prydwen slug from targets.json "en"
  5. unresolved -> placeholder in the UI

Runtime never fetches; the dashboard only reads cached WebP files.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from PIL import Image, ImageOps

PLUGIN = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN / "dashboard"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from nikke_lib import nikke_db as db  # noqa: E402

TARGETS = db.ensure_targets()
MAP_FILE = PLUGIN / "dashboard" / "portrait_map.json"
OUT = db.data_dir() / "portraits"
INDEX = OUT / "index.json"
OVERRIDES = OUT / "overrides"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Hermes-nikke-tracker/0.1"
ALLOWED_HOSTS = {"cdn.prydwen.gg", "patchwiki.biligame.com", "wiki.biligame.com"}
PRYDWEN = "https://cdn.prydwen.gg/images/nikke/characters/{slug}_card.webp"
BILIGAME = "https://wiki.biligame.com/nikke/{name}"
EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def key_for(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "-", name).strip()


def norm(text: str) -> str:
    return re.sub(r"[\s:：·・\-_（）()【】\[\]]+", "", text).lower()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:
        raise SystemExit(f"读取 {path} 失败: {exc}")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def allowed(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "") in ALLOWED_HOSTS
    except ValueError:
        return False


def download(url: str, *, timeout: int = 12) -> bytes:
    if not allowed(url):
        raise ValueError(f"不允许的图源: {url}")
    res = requests.get(url, timeout=timeout, headers={"User-Agent": UA}, stream=True)
    if res.status_code != 200:
        raise ValueError(f"HTTP {res.status_code}")
    if not allowed(res.url):
        raise ValueError(f"重定向到不允许的图源: {res.url}")
    content_type = (res.headers.get("content-type") or "").lower()
    if not content_type.startswith("image/"):
        raise ValueError(f"不是图片: {content_type}")
    chunks = []
    total = 0
    for chunk in res.iter_content(64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > 3 * 1024 * 1024:
            raise ValueError("图片超过 3MB")
        chunks.append(chunk)
    return b"".join(chunks)


def save_webp(raw: bytes, path: Path) -> tuple[int, int]:
    image = Image.open(io.BytesIO(raw))
    image = image.convert("RGB")
    image = ImageOps.fit(image, (256, 341), method=Image.Resampling.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    image.save(tmp, format="WEBP", quality=82, method=6)
    tmp.replace(path)
    return image.size


def local_override(name: str) -> Path | None:
    if not OVERRIDES.is_dir():
        return None
    candidates = [name, safe_name(name)]
    for stem in candidates:
        for ext in EXTENSIONS:
            path = OVERRIDES / (stem + ext)
            if path.is_file():
                return path
    return None


def prydwen_url(slug: str) -> str:
    return PRYDWEN.format(slug=slug)


def try_prydwen(slug: str) -> tuple[bytes, str] | None:
    if not slug:
        return None
    url = prydwen_url(slug)
    try:
        return download(url), url
    except Exception:
        return None


def biligame_candidates(name: str, aliases: list[str]) -> list[str]:
    out: list[str] = []
    for value in [name, *aliases]:
        if not value:
            continue
        for candidate in (
            value,
            re.sub(r"【[^】]*】", "", value).strip(),
            re.split(r"[:：]", value)[0].strip(),
        ):
            candidate = candidate.strip()
            if candidate and candidate not in out:
                out.append(candidate)
    return out


def try_biligame(name: str, aliases: list[str]) -> tuple[bytes, str] | None:
    for candidate in biligame_candidates(name, aliases):
        url = BILIGAME.format(name=quote(candidate))
        try:
            res = requests.get(url, timeout=12, headers={"User-Agent": UA})
            if res.status_code != 200:
                continue
            html = res.text
            title_match = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
            title = norm(title_match.group(1)) if title_match else ""
            if title and norm(candidate) not in title:
                continue
            images = re.findall(
                r'https://patchwiki\.biligame\.com/images/nikke/thumb/[^"\']+?/240px-[^"\']+?\.png',
                html,
            )
            if not images:
                continue
            preferred = [url for url in images if re.search(r"_s\.png$", url)]
            choices = preferred or images
            choices = [
                url for url in choices
                if not re.search(r"(SSR|SR/|/R\.png|武器|步枪|机枪|狙击|霰弹|冲锋|图标|icon)", url, re.I)
            ]
            for image_url in choices[:1]:
                try:
                    return download(image_url), image_url
                except Exception:
                    continue
        except Exception:
            continue
        finally:
            time.sleep(0.15)
    return None


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def resolve_one(name: str, target: dict, mapping: dict) -> tuple[bytes | None, str, str]:
    override = local_override(name)
    if override:
        return override.read_bytes(), "local", str(override)

    entry = mapping.get(name) or {}
    if entry.get("source") == "prydwen" and entry.get("slug"):
        hit = try_prydwen(entry["slug"])
        if hit:
            return hit[0], "prydwen", hit[1]

    aliases = target.get("aliases") or []
    hit = try_biligame(name, aliases)
    if hit:
        return hit[0], "biligame", hit[1]

    english = (target.get("en") or "").strip()
    if english:
        hit = try_prydwen(slugify(english))
        if hit:
            return hit[0], "prydwen", hit[1]

    return None, "unresolved", ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve and cache NIKKE character portraits.")
    parser.add_argument("--only", action="append", default=[], help="只处理指定角色（可重复）")
    parser.add_argument("--owned-only", action="store_true", help="只处理已拥有的角色")
    parser.add_argument("--force", action="store_true", help="重新下载已有缓存")
    parser.add_argument("--refresh", action="store_true", help="重新尝试未解析的角色")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 个角色（调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只打印解析计划，不下载")
    args = parser.parse_args()

    targets = load_json(TARGETS, {}).get("characters") or []
    by_name = {item["name"]: item for item in targets}
    mapping = load_json(MAP_FILE, {})
    progress = load_json(OUT.parent / "player_progress.json", {})
    owned = set((progress.get("characters") or {}).keys())

    names = args.only or [item["name"] for item in targets]
    if args.owned_only and not args.only:
        names = [name for name in names if name in owned]
    if args.limit:
        names = names[: args.limit]

    OUT.mkdir(parents=True, exist_ok=True)
    index_doc = load_json(INDEX, {})
    items = index_doc.get("items") if isinstance(index_doc, dict) else {}
    items = dict(items) if isinstance(items, dict) else {}

    counts = {"local": 0, "prydwen": 0, "biligame": 0, "unresolved": 0, "skipped": 0}
    unresolved: list[str] = []

    for position, name in enumerate(names, start=1):
        target = by_name.get(name) or {}
        key = key_for(name)
        cache = OUT / (key + ".webp")
        existing = items.get(key) or {}
        if cache.is_file() and not args.force:
            if not existing.get("file"):
                items[key] = {
                    "name": name,
                    "file": cache.name,
                    "mime": "image/webp",
                    "source": "cache",
                    "source_url": "",
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            counts["skipped"] += 1
            continue
        if existing.get("source") == "unresolved" and not args.refresh and not args.force:
            counts["unresolved"] += 1
            unresolved.append(name)
            continue

        print(f"[{position}/{len(names)}] {name} ...", end=" ", flush=True)
        if args.dry_run:
            source = "override" if local_override(name) else (mapping.get(name) or {}).get("slug", "biligame/en")
            print("plan:", source)
            continue

        raw, source, source_url = resolve_one(name, target, mapping)
        if not raw:
            items[key] = {
                "name": name,
                "file": None,
                "mime": None,
                "source": "unresolved",
                "source_url": "",
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            counts["unresolved"] += 1
            unresolved.append(name)
            print("unresolved")
            continue

        try:
            width, height = save_webp(raw, cache)
        except Exception as exc:
            counts["unresolved"] += 1
            unresolved.append(name)
            print("bad image:", exc)
            continue
        items[key] = {
            "name": name,
            "file": cache.name,
            "mime": "image/webp",
            "source": source,
            "source_url": source_url,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "width": width,
            "height": height,
        }
        counts[source] = counts.get(source, 0) + 1
        print("ok", source, f"{width}x{height}")

    write_json(
        INDEX,
        {
            "schema_version": 1,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "items": items,
        },
    )

    print()
    print("完成:", ", ".join(f"{key}={value}" for key, value in counts.items()))
    if unresolved:
        print("未解析:", "、".join(unresolved))
        print("可把图片放到", OVERRIDES, "后加 --only 重跑")
    return 0


if __name__ == "__main__":
    sys.exit(main())