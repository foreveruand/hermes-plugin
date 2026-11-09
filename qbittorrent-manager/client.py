"""qBittorrent WebUI client for the ``qbittorrent-manager`` plugin.

Paths are derived from ``HERMES_HOME`` and this file's own location, never
hardcoded, so the plugin moves with the profile.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

PLUGIN_DIR = Path(__file__).resolve().parent


def _load_plugin_env() -> None:
    """Overlay the plugin's own ``.env`` without clobbering real environment values."""
    try:
        text = (PLUGIN_DIR / ".env").read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_plugin_env()


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def cache_file() -> Path:
    return hermes_home() / "data" / "qbittorrent-manager" / "search_cache.json"


def qb_url() -> str:
    return os.environ.get("QB_URL", "http://localhost:8080").rstrip("/")


def qb_username() -> str:
    return os.environ.get("QB_USERNAME", "admin")


def qb_password() -> str:
    return os.environ.get("QB_PASSWORD", "")


def qb_rule() -> str:
    return os.environ.get("QB_RULE", "Sub")


def format_size(bytes_val: int) -> str:
    if bytes_val >= 1024 * 1024 * 1024:
        return f"{bytes_val / (1024**3):.1f} GB"
    if bytes_val >= 1024 * 1024:
        return f"{bytes_val / (1024**2):.1f} MB"
    return f"{bytes_val / 1024:.1f} KB"


def format_state(state: str) -> str:
    state_map = {
        "downloading": "⬇️ 下载中",
        "stalledDL": "⏳ 等待下载",
        "uploading": "⬆️ 做种中",
        "stalledUP": "✅ 完成",
        "pausedDL": "⏸️ 暂停",
        "pausedUP": "⏸️ 暂停",
        "queuedDL": "🕒 排队中",
        "queuedUP": "🕒 排队做种",
        "checkingDL": "🔍 校验中",
        "checkingUP": "🔍 校验中",
        "error": "❌ 错误",
        "missingFiles": "⚠️ 缺失文件",
    }
    return state_map.get(state, state)


def save_cache(torrents: list[dict[str, Any]]) -> None:
    path = cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        cache_data = [
            {
                "hash": t["hash"],
                "name": t.get("name", ""),
                "size": t.get("size", 0),
                "tags": t.get("tags", ""),
                "state": t.get("state", ""),
                "completion_on": t.get("completion_on", 0),
            }
            for t in torrents
        ]
        path.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_cache() -> list[dict[str, Any]]:
    path = cache_file()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


class QBClient:
    def __init__(self) -> None:
        self.url = qb_url()
        self.username = qb_username()
        self.password = qb_password()
        self.client = httpx.AsyncClient(base_url=self.url, timeout=30.0, follow_redirects=True)
        self.is_logged_in = False

    async def __aenter__(self) -> "QBClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def ensure_login(self) -> None:
        if self.is_logged_in:
            return
        resp = await self.client.post(
            "/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
        )
        if resp.text != "Ok.":
            raise RuntimeError(f"qBittorrent login failed: {resp.text}")
        self.is_logged_in = True

    async def request(self, method: str, endpoint: str, **kwargs: Any) -> httpx.Response:
        await self.ensure_login()
        resp = await self.client.request(method, endpoint, **kwargs)
        if resp.status_code == 403:
            self.is_logged_in = False
            await self.ensure_login()
            resp = await self.client.request(method, endpoint, **kwargs)
        return resp

    async def close(self) -> None:
        await self.client.aclose()

    async def get_all_torrents(
        self, filter_status: str = "all", limit: int | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if filter_status:
            params["filter"] = filter_status
        if limit:
            params["limit"] = limit
        resp = await self.request("GET", "/api/v2/torrents/info", params=params)
        resp.raise_for_status()
        return resp.json()

    async def search_torrents(self, query: str) -> list[dict[str, Any]]:
        torrents = await self.get_all_torrents()
        q_lower = query.lower()
        return [t for t in torrents if q_lower in t["name"].lower()]

    async def delete_torrents(self, hashes: list[str], delete_files: bool = True) -> bool:
        resp = await self.request(
            "POST",
            "/api/v2/torrents/delete",
            data={"hashes": "|".join(hashes), "deleteFiles": "true" if delete_files else "false"},
        )
        return resp.status_code == 200

    async def add_tags(self, hashes: list[str], tags: str) -> bool:
        resp = await self.request(
            "POST",
            "/api/v2/torrents/addTags",
            data={"hashes": "|".join(hashes), "tags": tags},
        )
        return resp.status_code == 200

    async def get_rules(self) -> dict[str, Any]:
        resp = await self.request("GET", "/api/v2/rss/rules")
        resp.raise_for_status()
        return resp.json()

    async def set_rule(self, rule_name: str, rule_def: dict[str, Any]) -> bool:
        resp = await self.request(
            "POST",
            "/api/v2/rss/setRule",
            data={"ruleName": rule_name, "ruleDef": json.dumps(rule_def)},
        )
        return resp.status_code == 200

    async def get_rss_keywords(self, rule_name: str | None = None) -> list[str]:
        rules = await self.get_rules()
        rule = rules.get(rule_name or qb_rule(), {})
        return self._split_keywords(rule.get("mustContain", ""))

    async def add_rss_keywords(
        self, keywords: list[str], rule_name: str | None = None
    ) -> tuple[bool, list[str]]:
        rule_name = rule_name or qb_rule()
        rules = await self.get_rules()
        rule = rules.get(rule_name, {})
        if not rule:
            rule = {
                "enabled": True,
                "mustContain": "",
                "mustNotContain": "",
                "useRegex": False,
                "episodeFilter": "",
                "smartFilter": False,
                "previouslyMatchedEpisodes": [],
                "affectedFeeds": [],
                "ignoreDays": 0,
                "lastMatch": "",
                "addPaused": False,
                "assignedCategory": "",
                "savePath": "",
            }
        current = set(self._split_keywords(rule.get("mustContain", "")))
        for kw in keywords:
            kw = kw.strip()
            if kw and kw not in current:
                current.add(kw)
        rule["mustContain"] = "|".join(sorted(current))
        ok = await self.set_rule(rule_name, rule)
        return ok, sorted(current)

    async def remove_rss_keyword(
        self, keyword: str, rule_name: str | None = None
    ) -> tuple[bool, list[str]]:
        rule_name = rule_name or qb_rule()
        rules = await self.get_rules()
        rule = rules.get(rule_name, {})
        if not rule:
            return False, []
        current = set(self._split_keywords(rule.get("mustContain", "")))
        kw_strip = keyword.strip()
        for m in [k for k in current if k.lower() == kw_strip.lower()]:
            current.remove(m)
        rule["mustContain"] = "|".join(sorted(current))
        ok = await self.set_rule(rule_name, rule)
        return ok, sorted(current)

    async def clean_rss_keywords(
        self, rule_name: str | None = None
    ) -> tuple[int, int, list[dict[str, str]]]:
        """Drop keywords that already matched a downloaded torrent."""
        rule_name = rule_name or qb_rule()
        rules = await self.get_rules()
        rule = rules.get(rule_name, {})
        if not rule:
            return 0, 0, []
        current = set(self._split_keywords(rule.get("mustContain", "")))
        if not current:
            return 0, 0, []

        torrents = await self.get_all_torrents()
        removed_list: list[dict[str, str]] = []
        for kw in list(current):
            kw_lower = kw.lower()
            matched = [t["name"] for t in torrents if kw_lower in t["name"].lower()]
            if matched:
                current.remove(kw)
                removed_list.append({"keyword": kw, "matched_torrent": matched[0]})

        if removed_list:
            rule["mustContain"] = "|".join(sorted(current))
            await self.set_rule(rule_name, rule)
        return len(removed_list), len(current), removed_list

    def _split_keywords(self, expr: str) -> list[str]:
        return [k.strip() for k in expr.split("|") if k.strip()]


async def resolve_targets(target_str: str, qb: QBClient) -> list[dict[str, Any]]:
    """Resolve an index list, a 40-char hash, or a search query to torrent entries."""
    cache = load_cache()
    tokens = [t.strip() for t in target_str.replace(",", " ").split() if t.strip()]

    if tokens and all(t.isdigit() for t in tokens):
        results = []
        for t in tokens:
            idx = int(t) - 1
            if 0 <= idx < len(cache):
                results.append(cache[idx])
        if results:
            return results

    if len(tokens) == 1 and len(tokens[0]) == 40 and all(c in "0123456789abcdefABCDEF" for c in tokens[0]):
        h = tokens[0].lower()
        for item in cache:
            if item["hash"].lower() == h:
                return [item]
        torrents = await qb.get_all_torrents()
        for t in torrents:
            if t["hash"].lower() == h:
                return [_entry(t)]
        return [{"hash": h, "name": h, "completion_on": 0}]

    matched = await qb.search_torrents(target_str)
    if matched:
        save_cache(matched)
        return [_entry(t) for t in matched]

    return []


def _entry(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "hash": t["hash"],
        "name": t.get("name", ""),
        "size": t.get("size", 0),
        "tags": t.get("tags", ""),
        "state": t.get("state", ""),
        "completion_on": t.get("completion_on", 0),
    }


def default_tag(completion_on: int | None) -> str:
    """MM-DD, seven days after completion (or after now when still downloading)."""
    if completion_on:
        target_dt = datetime.fromtimestamp(completion_on) + timedelta(days=7)
    else:
        target_dt = datetime.now() + timedelta(days=7)
    return target_dt.strftime("%m-%d")
