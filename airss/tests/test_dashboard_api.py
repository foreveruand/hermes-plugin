"""Unit tests for the airss dashboard plugin API.

Hermes dashboard 进程才装 fastapi，测试里用最小 stub 加载模块，并直接
调用路由函数；数据库用临时 sqlite，避免读写真实 airss 库。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pytest


def _load_api(hermes_home: Path):
    fastapi = types.ModuleType("fastapi")

    class APIRouter:
        def get(self, *_a, **_k):
            return lambda fn: fn

        def post(self, *_a, **_k):
            return lambda fn: fn

        def patch(self, *_a, **_k):
            return lambda fn: fn

        def delete(self, *_a, **_k):
            return lambda fn: fn

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            self.status_code = status_code
            self.detail = detail
            super().__init__(detail)

    fastapi.APIRouter = APIRouter
    fastapi.Body = lambda *_a, **_k: None
    fastapi.HTTPException = HTTPException
    sys.modules["fastapi"] = fastapi

    import os

    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(hermes_home)
    try:
        path = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
        name = "airss_dashboard_api_test_" + str(id(hermes_home))
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


@pytest.fixture()
def api(tmp_path):
    return _load_api(tmp_path / "hermes-empty")


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            schedules TEXT DEFAULT '[]',
            persona_id TEXT,
            persona_prompt TEXT
        );
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            interval INTEGER DEFAULT 5,
            source_group_id INTEGER DEFAULT 1,
            cookies TEXT,
            black_keyword TEXT,
            content_to_remove TEXT,
            max_image_number INTEGER DEFAULT 0,
            ai_summary_enabled INTEGER DEFAULT 0,
            enable_proxy INTEGER DEFAULT 0,
            stop INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            last_fetch TEXT,
            etag TEXT,
            last_modified TEXT
        );
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            title TEXT,
            content TEXT,
            link TEXT,
            guid TEXT,
            author TEXT,
            published_at TEXT,
            fetched_at TEXT,
            is_sent INTEGER DEFAULT 0,
            image_urls TEXT,
            content_hash TEXT,
            ai_filter_result INTEGER,
            ai_filter_checked_at TEXT
        );
        CREATE TABLE subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER,
            umo TEXT,
            personal_config TEXT
        );
        CREATE TABLE article_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER,
            subscriber_id INTEGER,
            sent_at TEXT
        );
        CREATE TABLE board_config (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE board_state (
            article_id INTEGER PRIMARY KEY,
            read_at TEXT,
            feedback INTEGER NOT NULL DEFAULT 0,
            starred INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO groups (id, name, schedules, persona_prompt)
            VALUES (1, '新闻', '[]', NULL), (2, '财经', '["08:00"]', 'group prompt');
        INSERT INTO subscriptions (
            id, name, url, interval, source_group_id, cookies, black_keyword,
            max_image_number, ai_summary_enabled, enable_proxy, stop, error_count, last_fetch
        ) VALUES (
            2, '示例订阅', 'https://example.com/feed.xml', 7, 2, 'a=b', 'bad',
            3, 1, 1, 0, 0, '2026-11-10T10:00:00+00:00'
        );
        INSERT INTO articles (subscription_id, title, link)
            VALUES (2, '文章', 'https://example.com/a');
        INSERT INTO board_config (key, value) VALUES ('summary_prompt', 'global prompt');
        """
    )
    conn.commit()
    conn.close()


def test_profile_dir_prefers_install_layout(api, tmp_path):
    fake = tmp_path / "profiles" / "p1" / "plugins" / "airss" / "dashboard" / "plugin_api.py"
    original = api.__file__
    try:
        api.__file__ = str(fake)
        assert api._profile_dir() == tmp_path / "profiles" / "p1"
        assert api._db_path() == tmp_path / "profiles" / "p1" / "data" / "airss" / "rss.db"
    finally:
        api.__file__ = original


def test_meta_returns_full_subscription_config(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db

    meta = api.board_meta()
    assert meta["available"] is True
    sub = meta["subscriptions"][0]
    assert sub["interval"] == 7
    assert sub["group_id"] == 2
    assert sub["cookies"] == "a=b"
    assert sub["black_keyword"] == "bad"
    assert sub["max_image_number"] == 3
    assert sub["ai_summary_enabled"] is True
    assert sub["enable_proxy"] is True
    assert sub["last_fetch"] == "2026-11-10T10:00:00+00:00"


def test_summary_uses_same_config_and_global_prompt(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db

    summary = api.board_summary_config()
    assert summary["available"] is True
    assert summary["global_prompt"] == "global prompt"
    finance = [g for g in summary["groups"] if g["id"] == 2][0]
    assert finance["prompt_source"] == "group"
    assert finance["subscriptions"][0]["interval"] == 7
    assert summary["subscriptions"][0]["url"] == "https://example.com/feed.xml"


def test_subscription_crud(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db

    created = api.board_create_subscription(
        {
            "url": "https://example.com/new.xml",
            "name": "新订阅",
            "interval": 9,
            "source_group_id": 1,
        }
    )
    assert created["ok"] is True
    new_id = created["subscription"]["id"]

    updated = api.board_update_subscription(
        new_id,
        {"interval": 11, "black_keyword": "x", "source_group_id": 2, "enable_proxy": True},
    )
    assert updated["subscription"]["interval"] == 11
    assert updated["subscription"]["group_id"] == 2
    assert updated["subscription"]["enable_proxy"] is True

    with pytest.raises(api.HTTPException) as exc:
        api.board_update_subscription(new_id, {"interval": 0})
    assert exc.value.status_code == 400

    with pytest.raises(api.HTTPException) as exc:
        api.board_create_subscription({"url": "https://example.com/feed.xml", "name": "重复"})
    assert exc.value.status_code == 409

    deleted = api.board_delete_subscription(new_id)
    assert deleted["ok"] is True


def test_group_crud_and_delete_guard(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db

    created = api.board_create_group({"name": "新分组", "schedules": ["09:00"]})
    group_id = created["group_id"]
    api.board_update_group(group_id, {"name": "改名分组", "persona_prompt": "p"})

    with pytest.raises(api.HTTPException) as exc:
        api.board_delete_group(2)
    assert exc.value.status_code == 409

    assert api.board_delete_group(group_id)["ok"] is True
