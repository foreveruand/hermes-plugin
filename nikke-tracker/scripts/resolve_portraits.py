#!/usr/bin/env python3
"""One-time portrait resolver for the NIKKE dashboard plugin.

Order:
  1. local override (data/nikke/portraits/overrides/)
  2. GameKee NIKKE character catalogue (https://www.gamekee.com/nikke) --
     PRIMARY source: its API returns a Chinese name + portrait PNG per character
  3. Prydwen character list (https://www.prydwen.gg/nikke/characters) --
     fallback: real image URLs keyed by character slug
  4. curated Prydwen slug (dashboard/portrait_map.json) -> CDN card URL
  5. unresolved -> placeholder in the UI

BiliGame was retired (the wiki no longer serves character images).  Runtime
never fetches from the dashboard process itself; the resolver script runs with
the system Python and the dashboard only reads cached WebP files.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
from PIL import Image, ImageOps

PLUGIN = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN / "dashboard"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from nikke_lib import nikke_db as db  # noqa: E402

TARGETS = db.ensure_targets()
MAP_FILE = PLUGIN / "dashboard" / "portrait_map.json"
NAME_MAP_FILE = PLUGIN / "references" / "portrait_names.json"
OUT = db.data_dir() / "portraits"
INDEX = OUT / "index.json"
OVERRIDES = OUT / "overrides"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.gamekee.com/",
}
# First-party hosts only; both are matched by suffix in allowed().
GAMEKEE_INDEX = "https://www.gamekee.com/v1/entry/treesByPid?pid=64599"
GAMEKEE_HEADERS = {"game-alias": "nikke", "game-id": "1253", "Accept": "application/json"}
PRYDWEN_INDEX = "https://www.prydwen.gg/nikke/characters"
PRYDWEN = "https://cdn.prydwen.gg/images/nikke/characters/{slug}_card.webp"
EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def key_for(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "-", name).strip()


def norm(text: str) -> str:
    """Normalise a character name for GameKee catalogue matching.

    Strips full/half-width punctuation and the plugin's own decorators such as
    【珍藏品】 so "托比【珍藏品】" matches the catalogue entry "托比".
    """
    text = text.strip().lower().replace("：", ":").replace("【", "[").replace("】", "]")
    text = re.sub(r"\[(?:珍藏品|改称)[^\]]*\]", "", text)
    return re.sub(r"[\s:·・\-_（）()\[\]]+", "", text)


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
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return (
        host == "prydwen.gg" or host.endswith(".prydwen.gg")
        or host == "gamekee.com" or host.endswith(".gamekee.com")
    )


def _fetch(
    url: str,
    *,
    timeout: int = 15,
    image: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    if not allowed(url):
        raise ValueError(f"不允许的图源: {url}")
    headers = dict(HEADERS)
    headers["Accept"] = (
        "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
        if image
        else "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    )
    host = urlparse(url).hostname or ""
    if "gamekee" in host:
        headers["Referer"] = "https://www.gamekee.com/"
    elif "prydwen" in host:
        headers["Referer"] = "https://www.prydwen.gg/"
    if extra_headers:
        headers.update(extra_headers)
    res = requests.get(url, timeout=timeout, headers=headers, stream=True)
    if res.status_code != 200:
        raise ValueError(f"HTTP {res.status_code}")
    if not allowed(res.url):
        raise ValueError(f"重定向到不允许的图源: {res.url}")
    content_type = (res.headers.get("content-type") or "").lower()
    # Some CDNs serve WebP as application/octet-stream; a strict image/*
    # check silently rejected valid portraits.  save_webp() validates the
    # payload with PIL, so only obvious HTML/JSON error pages are rejected.
    if image and ("text/html" in content_type or "json" in content_type):
        raise ValueError(f"不是图片: {content_type}")
    chunks: list[bytes] = []
    total = 0
    limit = 3 * 1024 * 1024 if image else 8 * 1024 * 1024
    for chunk in res.iter_content(64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise ValueError("响应超过大小限制")
        chunks.append(chunk)
    return b"".join(chunks)


def download(url: str, *, timeout: int = 12) -> bytes:
    return _fetch(url, timeout=timeout, image=True)


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


def try_prydwen(slug: str, reasons: list[str] | None = None) -> tuple[bytes, str] | None:
    if not slug:
        return None
    url = prydwen_url(slug)
    try:
        return download(url), url
    except Exception as exc:
        if reasons is not None:
            reasons.append(f"Prydwen CDN {slug}: {exc}")
        return None


# ---------------------------------------------------------------------------
# primary source: the GameKee character catalogue API
# ---------------------------------------------------------------------------
def gamekee_index(reasons: list[str]) -> dict[str, str]:
    """norm(Chinese name) -> portrait image URL, fetched once per process."""
    if GAMEKEE_INDEX in _INDEX_CACHE:
        return _INDEX_CACHE[GAMEKEE_INDEX]
    pairs: dict[str, str] = {}
    try:
        raw = _fetch(GAMEKEE_INDEX, timeout=20, extra_headers=GAMEKEE_HEADERS)
        rows = json.loads(raw.decode("utf-8", "replace")).get("data") or []
        for row in rows:
            name = str(row.get("name") or "").strip()
            icon = str(row.get("icon") or "").strip()
            if not name or not icon:
                continue
            url = "https:" + icon if icon.startswith("//") else icon
            pairs.setdefault(norm(name), url)
    except Exception as exc:
        reasons.append(f"GameKee 图鉴: {exc}")
    _INDEX_CACHE[GAMEKEE_INDEX] = pairs
    return pairs


def gamekee_name_candidates(name: str, target: dict, name_map: dict) -> list[str]:
    out: list[str] = []

    def add(value: str) -> None:
        value = (value or "").strip()
        if value and value not in out:
            out.append(value)

    # A name corrected in the dashboard 校核 form wins over every guess.
    add(str(target.get("portrait_name") or ""))
    add(str(name_map.get(name) or ""))
    add(name)
    for alias in target.get("aliases") or []:
        add(str(alias or ""))
    return out


def try_gamekee(
    name: str, target: dict, name_map: dict, reasons: list[str]
) -> tuple[bytes, str] | None:
    pairs = gamekee_index(reasons)
    if not pairs:
        return None
    for candidate in gamekee_name_candidates(name, target, name_map):
        url = pairs.get(norm(candidate))
        if not url:
            continue
        try:
            return download(url), url
        except Exception as exc:
            reasons.append(f"GameKee 头像 {candidate}: {exc}")
    return None


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


# ---------------------------------------------------------------------------
# fallback source: the Prydwen character list page
# ---------------------------------------------------------------------------
_INDEX_CACHE: dict[str, Any] = {}
_IMG_PATH = (
    r"/images/nikke/characters/"
    r"([a-z0-9][a-z0-9\-]*?)(?:_(?:card|icon|portrait|avatar|full|thumb))?"
    r"\.(?:webp|png|jpe?g)"
)
_INDEX_IMG_RE = re.compile(
    r"(?:https?://(?:[a-z0-9\-]+\.)?prydwen\.gg|//(?:[a-z0-9\-]+\.)?prydwen\.gg)?" + _IMG_PATH,
    re.I,
)


def prydwen_index(reasons: list[str]) -> str:
    """Fetch the Prydwen character list once per process (in-memory cache)."""
    if PRYDWEN_INDEX in _INDEX_CACHE:
        return _INDEX_CACHE[PRYDWEN_INDEX]
    try:
        html = _fetch(PRYDWEN_INDEX, timeout=15).decode("utf-8", "replace")
    except Exception as exc:
        reasons.append(f"Prydwen 角色页: {exc}")
        html = ""
    _INDEX_CACHE[PRYDWEN_INDEX] = html
    return html


def parse_prydwen_index(html: str) -> dict[str, list[str]]:
    """slug -> real character image URLs found on the Prydwen list page.

    Handles plain CDN URLs, protocol-relative/relative paths, JSON-escaped
    URLs and Next.js /_next/image?url=... proxies.
    """
    text = html.replace("\\/", "/").replace("\\u002F", "/").replace("&amp;", "&")
    for proxy in re.findall(r'/_next/image\?[^"\'\s<>]+', text):
        inner = unquote(
            parse_qs(urlparse("https://www.prydwen.gg" + proxy).query).get("url", [""])[0]
        )
        if "/images/nikke/characters/" in inner:
            text += " " + inner
    out: dict[str, list[str]] = {}
    for match in _INDEX_IMG_RE.finditer(text):
        slug = match.group(1).lower()
        url = match.group(0)
        if url.startswith("//"):
            url = "https:" + url
        elif not url.startswith("http"):
            url = "https://cdn.prydwen.gg" + (url if url.startswith("/") else "/" + url)
        bucket = out.setdefault(slug, [])
        if url not in bucket:
            if "_card." in url:
                bucket.insert(0, url)
            else:
                bucket.append(url)
    return out


def prydwen_slug_candidates(name: str, target: dict, mapping: dict) -> list[str]:
    out: list[str] = []

    def add(value: str) -> None:
        slug = slugify(value or "")
        if slug and slug not in out:
            out.append(slug)

    add((mapping.get(name) or {}).get("slug", ""))
    # A name corrected in the dashboard 校核 form may be an English name/slug.
    add(str(target.get("portrait_name") or ""))
    english = (target.get("en") or "").strip()
    if english:
        add(english)
        words = english.split()
        if len(words) >= 2:
            add(" ".join(reversed(words[:2])))
    for alias in target.get("aliases") or []:
        if re.search(r"[A-Za-z]", alias or ""):
            add(alias)
    return out


def try_prydwen_index(
    name: str, target: dict, mapping: dict, reasons: list[str]
) -> tuple[bytes, str] | None:
    html = prydwen_index(reasons)
    if not html:
        return None
    pairs = parse_prydwen_index(html)
    if not pairs:
        reasons.append("Prydwen 角色页未解析到头像链接")
        return None
    for slug in prydwen_slug_candidates(name, target, mapping):
        urls = list(pairs.get(slug) or [])
        if not urls:  # obvious slug variant, e.g. rapi-red-hood vs rapi
            urls = [
                u
                for key, bucket in pairs.items()
                if key.startswith(slug + "-") or slug.startswith(key + "-")
                for u in bucket
            ]
        for url in urls[:2]:
            try:
                return download(url), url
            except Exception as exc:
                reasons.append(f"Prydwen 头像 {slug}: {exc}")
    return None


def resolve_one(
    name: str, target: dict, mapping: dict, name_map: dict, reasons: list[str]
) -> tuple[bytes | None, str, str]:
    override = local_override(name)
    if override:
        return override.read_bytes(), "local", str(override)

    # PRIMARY: GameKee catalogue, matched by corrected Chinese name
    hit = try_gamekee(name, target, name_map, reasons)
    if hit:
        return hit[0], "gamekee", hit[1]

    # FALLBACK: real image URLs scraped from the Prydwen character list
    hit = try_prydwen_index(name, target, mapping, reasons)
    if hit:
        return hit[0], "prydwen", hit[1]

    entry = mapping.get(name) or {}
    if entry.get("source") == "prydwen" and entry.get("slug"):
        hit = try_prydwen(entry["slug"], reasons)
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
    name_map = load_json(NAME_MAP_FILE, {})
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

    counts = {"local": 0, "gamekee": 0, "prydwen": 0, "unresolved": 0, "skipped": 0}
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
            plan = "override" if local_override(name) else "gamekee → prydwen"
            print("plan:", plan, "| name:", name_map.get(name, name))
            continue

        reasons: list[str] = []
        raw, source, source_url = resolve_one(name, target, mapping, name_map, reasons)
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
            print("unresolved |", "；".join(reasons[-3:]) if reasons else "无可用图源")
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
        print("可把图片放到", OVERRIDES, "后加 --only 重跑；或在面板「校核」里填写头像名称")
    return 0


if __name__ == "__main__":
    sys.exit(main())