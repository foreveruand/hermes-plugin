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

        def put(self, *_a, **_k):
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
            VALUES (2, '已推送文章', 'https://example.com/a'),
                   (2, '未推送文章', 'https://example.com/b');
        INSERT INTO article_sent (article_id, subscriber_id) VALUES (1, 10);
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


def test_pushed_article_is_read_on_board(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db

    meta = api.board_meta()
    assert meta["counts"]["total"] == 2
    assert meta["counts"]["unread"] == 1
    assert meta["subscriptions"][0]["unread"] == 1

    unread = api.board_items(view="unread")
    assert [item["title"] for item in unread["items"]] == ["未推送文章"]

    pushed = api.board_item_detail(1)
    assert pushed["delivered"] is True
    assert pushed["read"] is True

    unsent = api.board_item_detail(2)
    assert unsent["delivered"] is False
    assert unsent["read"] is False


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


# ---------------------------------------------------------------------------
# 阅读器接口（/subscriptions、/articles、/groups）
# ---------------------------------------------------------------------------

def _add_subscriber(db: Path, subscription_id: int = 2, umo: str = "telegram:100") -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO subscribers (subscription_id, umo, personal_config) VALUES (?, ?, ?)",
        (subscription_id, umo, '{"only_title": true}'),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def test_reader_subscriptions_include_counts(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db
    _add_subscriber(db)

    subs = api.list_subscriptions()["subscriptions"]
    assert len(subs) == 1
    sub = subs[0]
    assert sub["unread_count"] == 1
    assert sub["unread"] == 1
    assert sub["subscriber_count"] == 1
    assert sub["has_gateway_subscriber"] is True
    assert sub["interval"] == 7 and sub["group_id"] == 2


def test_reader_articles_filters(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db

    everything = api.list_articles()
    assert everything["available"] is True
    assert everything["total"] == 2

    unread = api.list_articles(unread_only=True)
    assert [item["title"] for item in unread["items"]] == ["未推送文章"]

    by_sub = api.list_articles(subscription_id=2)
    assert by_sub["total"] == 2
    assert api.list_articles(subscription_id=99)["total"] == 0

    by_keyword = api.list_articles(keyword="已推送")
    assert [item["title"] for item in by_keyword["items"]] == ["已推送文章"]

    paged = api.list_articles(limit=1, offset=1)
    assert len(paged["items"]) == 1
    assert paged["total"] == 2


def test_reader_mark_read_and_star(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db

    marked = api.mark_article_read(2)
    assert marked["ok"] is True
    assert marked["read_at"]

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT read_at, is_sent FROM board_state bs JOIN articles a "
        "ON a.id = bs.article_id WHERE a.id = 2"
    ).fetchone()
    assert row[0] is not None and row[1] == 0
    conn.close()

    assert api.list_articles(unread_only=True)["total"] == 0

    starred = api.star_article(1, {"value": True})
    assert starred["starred"] is True
    toggled = api.star_article(1, None)
    assert toggled["starred"] is False

    with pytest.raises(api.HTTPException) as exc:
        api.mark_article_read(999)
    assert exc.value.status_code == 404


def test_reader_subscription_crud_and_whitelist(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db

    created = api.create_subscription(
        {"url": "https://example.com/reader.xml", "name": "阅读器订阅", "interval": 8}
    )
    assert created["ok"] is True
    new_id = created["id"]
    assert created["subscription"]["subscriber_count"] == 0

    updated = api.update_subscription(
        new_id, {"interval": 12, "group_id": 2, "is_sent": 1}
    )
    sub = updated["subscription"]
    assert sub["interval"] == 12
    assert sub["group_id"] == 2

    conn = sqlite3.connect(db)
    is_sent = conn.execute(
        "SELECT is_sent FROM articles WHERE subscription_id = ?", (new_id,)
    ).fetchall()
    conn.close()
    assert is_sent == []

    with pytest.raises(api.HTTPException) as exc:
        api.update_subscription(new_id, {"is_sent": 1})
    assert exc.value.status_code == 400

    with pytest.raises(api.HTTPException) as exc:
        api.update_subscription(new_id, {"interval": 0})
    assert exc.value.status_code == 400

    deleted = api.delete_subscription(new_id)
    assert deleted["ok"] is True and deleted["stop"] is True

    # 软删除：订阅行保留并暂停，历史文章不动
    kept = [s for s in api.list_subscriptions()["subscriptions"] if s["id"] == new_id][0]
    assert kept["stop"] is True
    assert kept["subscriber_count"] == 0


def test_reader_subscribers_and_last_delete_stops(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db
    subscriber_id = _add_subscriber(db)

    subs = api.list_subscribers(2)["subscribers"]
    assert len(subs) == 1
    assert subs[0]["id"] == subscriber_id
    assert subs[0]["adapter_type"] == "telegram"
    assert subs[0]["personal_config"] == {"only_title": True}

    with pytest.raises(api.HTTPException) as exc:
        api.list_subscribers(999)
    assert exc.value.status_code == 404

    result = api.delete_subscriber(2, subscriber_id)
    assert result["ok"] is True
    assert result["stopped"] is True
    assert result["remaining_subscribers"] == 0

    conn = sqlite3.connect(db)
    stop = conn.execute("SELECT stop FROM subscriptions WHERE id = 2").fetchone()[0]
    orphan = conn.execute(
        "SELECT COUNT(*) FROM article_sent WHERE subscriber_id = ?", (subscriber_id,)
    ).fetchone()[0]
    conn.close()
    assert stop == 1 and orphan == 0

    # 文章与展板状态不随订阅者删除而丢失
    sub = [s for s in api.list_subscriptions()["subscriptions"] if s["id"] == 2][0]
    assert sub["stop"] is True and sub["subscriber_count"] == 0
    assert api.list_articles(subscription_id=2)["total"] == 2

    with pytest.raises(api.HTTPException) as exc:
        api.delete_subscriber(2, subscriber_id)
    assert exc.value.status_code == 404


def test_reader_delete_one_subscriber_keeps_subscription_running(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db
    first = _add_subscriber(db, subscription_id=2, umo="telegram:100")
    second = _add_subscriber(db, subscription_id=2, umo="telegram:200")

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO article_sent (article_id, subscriber_id) VALUES (2, ?), (2, ?)",
        (first, second),
    )
    conn.commit()
    conn.close()

    result = api.delete_subscriber(2, first)
    assert result["ok"] is True
    assert result["stopped"] is False
    assert result["remaining_subscribers"] == 1

    conn = sqlite3.connect(db)
    stop = conn.execute("SELECT stop FROM subscriptions WHERE id = 2").fetchone()[0]
    sent = conn.execute(
        "SELECT subscriber_id FROM article_sent WHERE article_id = 2"
    ).fetchall()
    conn.close()
    # 只剩一个接收端：不停更，另一个接收端的推送记录保持不动
    assert stop == 0
    assert sent == [(second,)]

    # 权限隔离：用别的订阅 id 删同一个 subscriber 必须 404，且该行不受影响
    other = api.create_subscription(
        {"url": "https://example.com/other.xml", "name": "另一个订阅"}
    )
    with pytest.raises(api.HTTPException) as exc:
        api.delete_subscriber(other["id"], second)
    assert exc.value.status_code == 404
    assert api.list_subscribers(2)["subscribers"][0]["id"] == second


def test_reader_groups_list_and_update(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db

    groups = api.list_groups()["groups"]
    assert [g["id"] for g in groups] == [1, 2]
    finance = [g for g in groups if g["id"] == 2][0]
    assert finance["schedules"] == ["08:00"]
    assert finance["persona_prompt"] == "group prompt"
    assert finance["subscription_count"] == 1

    updated = api.update_group(
        2, {"name": "财经改", "schedules": ["09:30"], "persona_prompt": "新提示"}
    )
    assert updated["group"]["name"] == "财经改"
    assert updated["group"]["schedules"] == ["09:30"]
    assert updated["group"]["persona_prompt"] == "新提示"

    with pytest.raises(api.HTTPException) as exc:
        api.update_group(999, {"name": "x"})
    assert exc.value.status_code == 404

    with pytest.raises(api.HTTPException) as exc:
        api.update_group(2, {"unknown": 1})
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# 阅读增强：批量已读 / 全文 / 注解 / 翻译 / 可见性
# ---------------------------------------------------------------------------

class _FakeLlm:
    def __init__(self, text: str):
        self.text = text
        self.calls = []

    def complete(self, messages, **_kwargs):
        self.calls.append(messages)
        return types.SimpleNamespace(text=self.text)


def test_schema_creates_reader_tables(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db
    api._connect().close()

    conn = sqlite3.connect(db)
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    cols = {r[1] for r in conn.execute("PRAGMA table_info(subscriptions)")}
    conn.close()
    assert {"article_notes", "translate_config", "article_translations"} <= names
    assert "visible_in_panel" in cols


def test_batch_mark_read_and_unread(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db

    marked = api.batch_mark_read({"article_ids": [1, 2, 999], "read": True})
    assert marked == {"ok": True, "count": 2, "read": True}
    assert api.list_articles(unread_only=True)["total"] == 0

    unread = api.batch_mark_read({"article_ids": [2], "read": False})
    assert unread["count"] == 1
    assert [i["id"] for i in api.list_articles(unread_only=True)["items"]] == [2]

    # 已推送文章即使清掉 board_state.read_at，仍按 delivered 显示已读
    api.batch_mark_read({"article_ids": [1], "read": False})
    assert api.board_item_detail(1)["read"] is True

    conn = sqlite3.connect(db)
    is_sent = conn.execute("SELECT is_sent FROM articles WHERE id = 2").fetchone()[0]
    sent_rows = conn.execute("SELECT COUNT(*) FROM article_sent").fetchone()[0]
    conn.close()
    assert is_sent == 0 and sent_rows == 1

    with pytest.raises(api.HTTPException) as exc:
        api.batch_mark_read({"article_ids": [1], "read": "yes"})
    assert exc.value.status_code == 400
    with pytest.raises(api.HTTPException) as exc:
        api.batch_mark_read({"article_ids": "1,2"})
    assert exc.value.status_code == 400


def test_full_content_returns_sanitized_existing_content(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE articles SET content = ? WHERE id = 1",
        ("<p>正文</p><script>alert(1)</script><a href=\"javascript:x\">x</a>",),
    )
    conn.commit()
    conn.close()

    result = api.get_full_content(1)
    assert result["id"] == 1
    assert result["is_fetched"] is False
    assert "正文" in result["content"]
    assert "script" not in result["content"].lower()
    assert "javascript:" not in result["content"].lower()

    with pytest.raises(api.HTTPException) as exc:
        api.get_full_content(999)
    assert exc.value.status_code == 404


def test_article_notes_crud(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db

    empty = api.get_article_notes(1)
    assert empty == {"id": 1, "note_text": "", "created_at": None}

    saved = api.save_article_notes(1, {"note_text": "  重要  "})
    assert saved["ok"] is True and saved["note_text"] == "重要"
    assert saved["created_at"]

    loaded = api.get_article_notes(1)
    assert loaded["note_text"] == "重要"

    api.save_article_notes(1, {"note_text": "更新后的注解"})
    assert api.get_article_notes(1)["note_text"] == "更新后的注解"

    deleted = api.save_article_notes(1, {"note_text": ""})
    assert deleted["deleted"] is True
    assert api.get_article_notes(1)["note_text"] == ""

    with pytest.raises(api.HTTPException) as exc:
        api.save_article_notes(999, {"note_text": "x"})
    assert exc.value.status_code == 404
    with pytest.raises(api.HTTPException) as exc:
        api.save_article_notes(1, {"note_text": 1})
    assert exc.value.status_code == 400


def test_translate_config_crud(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db

    default = api.get_translate_config(2)
    assert default["target_language"] == "zh-CN"
    assert default["auto_translate"] is False
    assert default["configured"] is False

    saved = api.update_translate_config(
        2, {"target_language": "ja", "auto_translate": True}
    )
    assert saved == {
        "ok": True,
        "subscription_id": 2,
        "target_language": "ja",
        "auto_translate": True,
    }
    loaded = api.get_translate_config(2)
    assert loaded["target_language"] == "ja"
    assert loaded["auto_translate"] is True
    assert loaded["configured"] is True

    with pytest.raises(api.HTTPException) as exc:
        api.update_translate_config(999, {"target_language": "ja"})
    assert exc.value.status_code == 404
    with pytest.raises(api.HTTPException) as exc:
        api.update_translate_config(2, {"target_language": "not a lang!"})
    assert exc.value.status_code == 400
    with pytest.raises(api.HTTPException) as exc:
        api.update_translate_config(2, {"auto_translate": "yes"})
    assert exc.value.status_code == 400


def test_subscription_visibility_filters_board_and_list(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db

    hidden = api.update_subscription_visibility(2, {"visible_in_panel": False})
    assert hidden == {"ok": True, "subscription_id": 2, "visible_in_panel": False}

    assert api.list_subscriptions()["subscriptions"] == []
    assert len(api.list_subscriptions(include_hidden=True)["subscriptions"]) == 1
    assert api.board_items()["total"] == 0
    assert api.board_items(view="all", include_hidden=True)["total"] == 2
    assert api.board_meta()["counts"]["total"] == 0

    api.update_subscription_visibility(2, {"visible_in_panel": True})
    assert api.board_meta()["counts"]["total"] == 2

    with pytest.raises(api.HTTPException) as exc:
        api.update_subscription_visibility(999, {"visible_in_panel": True})
    assert exc.value.status_code == 404
    with pytest.raises(api.HTTPException) as exc:
        api.update_subscription_visibility(2, {"visible_in_panel": 1})
    assert exc.value.status_code == 400


def test_translate_article_uses_cache_and_sanitizes(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE articles SET content = ? WHERE id = 2",
        ("<p>Original body</p>",),
    )
    conn.commit()
    conn.close()

    fake = _FakeLlm(
        '{"title": "译文标题", "content": "<p>你好</p><script>bad()</script>"}'
    )
    api.set_llm_client(fake)

    first = api.translate_article(2, {"target_language": "zh-CN"})
    assert first["translated_title"] == "译文标题"
    assert "你好" in first["translated_content"]
    assert "script" not in first["translated_content"].lower()
    assert first["cached"] is False
    assert len(fake.calls) == 1

    second = api.translate_article(2, {"target_language": "zh-CN"})
    assert second["cached"] is True
    assert second["translated_content"] == first["translated_content"]
    assert len(fake.calls) == 1

    # force 重新翻译会再调一次 LLM
    api.translate_article(2, {"target_language": "zh-CN", "force": True})
    assert len(fake.calls) == 2

    with pytest.raises(api.HTTPException) as exc:
        api.translate_article(2, {"target_language": "bad lang!"})
    assert exc.value.status_code == 400


def test_translate_article_without_llm_returns_503(api, tmp_path):
    db = tmp_path / "rss.db"
    _make_db(db)
    api._db_path = lambda: db
    api.set_llm_client(None)

    with pytest.raises(api.HTTPException) as exc:
        api.translate_article(2, {"target_language": "zh-CN"})
    assert exc.value.status_code == 503
