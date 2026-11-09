"""airss 仪表盘「RSS 展板」后端（Hermes dashboard plugin）。

路由由 Hermes 挂到 /api/plugins/airss/ 下。

安全约束：只读 airss 自有表（subscriptions / articles / groups），绝不写它们
——articles.is_sent 与 article_sent 是 Telegram 推送语义，改了会吞推送。
展板自己的已读/反馈状态只存本模块新建的 board_state 表。

数据源与 airss/__init__.py 一致：
$HERMES_HOME/data/airss/rss.db（HERMES_HOME 环境变量，没有则 ~/.hermes）。
数据库文件不存在时不抛 500：meta 返回 available:false，items 返回空列表。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from html import escape as _html_escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, HTTPException

router = APIRouter()


# ---------------------------------------------------------------------------
# 数据库连接
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return hermes_home / "data" / "airss" / "rss.db"


def _ensure_board_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS board_state (
            article_id INTEGER PRIMARY KEY,
            read_at TEXT,
            feedback INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()


def _connect() -> Optional[sqlite3.Connection]:
    """返回连接；数据库文件不存在时返回 None（调用方据此报 available:false）。"""
    path = _db_path()
    if not path.is_file():
        return None
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    _ensure_board_table(conn)
    return conn


# 插件加载时若库已存在，先把 board_state 建好（失败不影响后续请求里的懒建）。
try:
    _boot_conn = _connect()
    if _boot_conn is not None:
        _boot_conn.close()
except Exception:
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_image_urls(raw) -> list:
    """image_urls 的实际存储格式以 airss/database.py 为准：'|||'.join(...)。

    同时兼容 JSON 数组字符串与单个裸 URL。
    """
    if not raw:
        return []
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(u) for u in data if u]
        except Exception:
            pass
    return [p.strip() for p in text.split("|||") if p.strip()]


# ---------------------------------------------------------------------------
# 正文 HTML 清洗（只用标准库）：去掉危险标签及其内容、on* 属性、javascript: 链接
# ---------------------------------------------------------------------------

_DROP_WITH_CONTENT = {"script", "iframe", "style", "object", "form"}
_DROP_TAG_ONLY = {"embed", "link", "meta", "base"}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
_URL_ATTRS = {"href", "src", "xlink:href", "action", "formaction"}


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list = []
        self._skip = 0  # 位于被删标签（含内容）内部的深度

    def _clean_attrs(self, attrs) -> list:
        cleaned = []
        for name, value in attrs:
            lname = (name or "").lower()
            if lname.startswith("on"):
                continue
            if value is not None and lname in _URL_ATTRS:
                v = value.strip().lower()
                if v.startswith("javascript:") or v.startswith("vbscript:"):
                    continue
            cleaned.append((lname, value))
        return cleaned

    @staticmethod
    def _render_attrs(attrs) -> str:
        parts = []
        for name, value in attrs:
            if value is None:
                parts.append(" " + name)
            else:
                parts.append(' %s="%s"' % (name, _html_escape(value, quote=True)))
        return "".join(parts)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip:
            if tag in _DROP_WITH_CONTENT:
                self._skip += 1
            return
        if tag in _DROP_WITH_CONTENT:
            self._skip = 1
            return
        if tag in _DROP_TAG_ONLY:
            return
        self._out.append("<" + tag + self._render_attrs(self._clean_attrs(attrs)) + ">")

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self._skip or tag in _DROP_WITH_CONTENT or tag in _DROP_TAG_ONLY:
            return
        self._out.append("<" + tag + self._render_attrs(self._clean_attrs(attrs)) + ">")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip:
            if tag in _DROP_WITH_CONTENT:
                self._skip -= 1
            return
        if tag in _DROP_WITH_CONTENT or tag in _DROP_TAG_ONLY or tag in _VOID_TAGS:
            return
        self._out.append("</" + tag + ">")

    def handle_data(self, data):
        if self._skip:
            return
        self._out.append(_html_escape(data, quote=False))

    def result(self) -> str:
        return "".join(self._out)


def sanitize_html(raw) -> str:
    if not raw:
        return ""
    parser = _Sanitizer()
    try:
        parser.feed(str(raw))
        parser.close()
    except Exception:
        return _html_escape(str(raw), quote=False)
    return parser.result()


# ---------------------------------------------------------------------------
# 查询辅助
# ---------------------------------------------------------------------------

_ITEM_COLUMNS = """
    a.id, a.subscription_id, a.title, a.link, a.author, a.published_at,
    a.fetched_at, a.is_sent, a.ai_filter_result,
    s.name AS subscription_name, s.source_group_id AS group_id,
    bs.read_at AS board_read_at, bs.feedback AS board_feedback
"""

_ITEM_FROM = """
FROM articles a
JOIN subscriptions s ON s.id = a.subscription_id
LEFT JOIN board_state bs ON bs.article_id = a.id
"""


def _row_to_item(row: sqlite3.Row, image_urls: list) -> dict:
    return {
        "id": row["id"],
        "title": row["title"] or "",
        "subscription_id": row["subscription_id"],
        "subscription_name": row["subscription_name"],
        "group_id": row["group_id"],
        "author": row["author"] or "",
        "published_at": row["published_at"],
        "fetched_at": row["fetched_at"],
        "link": row["link"] or "",
        "image": image_urls[0] if image_urls else None,
        "has_content": bool(row["has_content"]) if "has_content" in row.keys() else False,
        "is_sent": bool(row["is_sent"]),
        "ai_filter_result": row["ai_filter_result"],
        "read": row["board_read_at"] is not None,
        "feedback": row["board_feedback"] if row["board_feedback"] is not None else 0,
    }


def _filter_clauses(view, group_id, subscription_id, q):
    clauses, params = [], []
    if view != "all":
        clauses.append("bs.read_at IS NULL")
    if subscription_id is not None:
        clauses.append("a.subscription_id = ?")
        params.append(subscription_id)
    if group_id is not None:
        clauses.append("s.source_group_id = ?")
        params.append(group_id)
    if q:
        clauses.append("(a.title LIKE ? OR a.author LIKE ?)")
        like = "%" + q + "%"
        params.extend([like, like])
    return clauses, params


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

@router.get("/board/meta")
def board_meta():
    conn = _connect()
    if conn is None:
        return {
            "available": False,
            "counts": {"total": 0, "unread": 0},
            "groups": [],
            "subscriptions": [],
        }
    try:
        total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        unread = conn.execute(
            "SELECT COUNT(*) FROM articles a "
            "LEFT JOIN board_state bs ON bs.article_id = a.id "
            "WHERE bs.read_at IS NULL"
        ).fetchone()[0]
        groups = [
            {"id": r["id"], "name": r["name"]}
            for r in conn.execute("SELECT id, name FROM groups ORDER BY id")
        ]
        subscriptions = [
            {
                "id": r["id"],
                "name": r["name"],
                "url": r["url"],
                "group_id": r["source_group_id"],
            }
            for r in conn.execute(
                "SELECT id, name, url, source_group_id FROM subscriptions ORDER BY id"
            )
        ]
        return {
            "available": True,
            "counts": {"total": total, "unread": unread},
            "groups": groups,
            "subscriptions": subscriptions,
        }
    except sqlite3.Error:
        return {
            "available": False,
            "counts": {"total": 0, "unread": 0},
            "groups": [],
            "subscriptions": [],
        }
    finally:
        conn.close()


@router.get("/board/items")
def board_items(
    view: str = "unread",
    group_id: Optional[int] = None,
    subscription_id: Optional[int] = None,
    q: str = "",
    limit: int = 50,
    offset: int = 0,
):
    conn = _connect()
    if conn is None:
        return {"available": False, "items": [], "total": 0}
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    clauses, params = _filter_clauses(view, group_id, subscription_id, q)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        total = conn.execute(
            "SELECT COUNT(*) " + _ITEM_FROM + where, params
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT " + _ITEM_COLUMNS + ", a.image_urls, "
            "(a.content IS NOT NULL AND TRIM(a.content) != '') AS has_content "
            + _ITEM_FROM + where
            + " ORDER BY a.fetched_at DESC, a.id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        items = [_row_to_item(r, _parse_image_urls(r["image_urls"])) for r in rows]
        return {"available": True, "items": items, "total": total}
    except sqlite3.Error:
        return {"available": False, "items": [], "total": 0}
    finally:
        conn.close()


@router.get("/board/items/{item_id}")
def board_item_detail(item_id: int):
    conn = _connect()
    if conn is None:
        return {"available": False}
    try:
        row = conn.execute(
            "SELECT " + _ITEM_COLUMNS + ", a.image_urls, a.content, 1 AS has_content "
            + _ITEM_FROM + " WHERE a.id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="article not found")
        image_urls = _parse_image_urls(row["image_urls"])
        item = _row_to_item(row, image_urls)
        item["has_content"] = bool(row["content"] and str(row["content"]).strip())
        item["content"] = sanitize_html(row["content"])
        item["image_urls"] = image_urls
        item["available"] = True
        return item
    finally:
        conn.close()


@router.post("/board/items/{item_id}/read")
def board_mark_read(item_id: int):
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        conn.execute(
            """
            INSERT INTO board_state (article_id, read_at, feedback) VALUES (?, ?, 0)
            ON CONFLICT(article_id) DO UPDATE
            SET read_at = COALESCE(board_state.read_at, excluded.read_at)
            """,
            (item_id, _now_iso()),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.post("/board/items/{item_id}/feedback")
def board_set_feedback(item_id: int, payload: dict = Body(...)):
    value = payload.get("value") if isinstance(payload, dict) else None
    if value not in (-1, 0, 1):
        raise HTTPException(status_code=400, detail="value must be -1, 0 or 1")
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        conn.execute(
            """
            INSERT INTO board_state (article_id, read_at, feedback) VALUES (?, NULL, ?)
            ON CONFLICT(article_id) DO UPDATE SET feedback = excluded.feedback
            """,
            (item_id, value),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.post("/board/read-all")
def board_read_all(payload: Optional[dict] = Body(default=None)):
    payload = payload or {}
    subscription_id = payload.get("subscription_id")
    group_id = payload.get("group_id")
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False, "marked": 0}
    try:
        clauses, params = [], []
        if subscription_id is not None:
            clauses.append("a.subscription_id = ?")
            params.append(subscription_id)
        if group_id is not None:
            clauses.append(
                "a.subscription_id IN "
                "(SELECT id FROM subscriptions WHERE source_group_id = ?)"
            )
            params.append(group_id)
        id_query = "SELECT a.id FROM articles a"
        if clauses:
            id_query += " WHERE " + " AND ".join(clauses)
        now = _now_iso()
        # 尚无 board_state 行的：插入
        cur = conn.execute(
            "INSERT INTO board_state (article_id, read_at, feedback) "
            "SELECT id, ?, 0 FROM (" + id_query + ") "
            "WHERE id NOT IN (SELECT article_id FROM board_state)",
            [now] + params,
        )
        marked = cur.rowcount or 0
        # 已有行但未读的（如只给过反馈）：补 read_at
        cur = conn.execute(
            "UPDATE board_state SET read_at = ? "
            "WHERE read_at IS NULL AND article_id IN (" + id_query + ")",
            [now] + params,
        )
        marked += cur.rowcount or 0
        conn.commit()
        return {"ok": True, "marked": marked}
    finally:
        conn.close()
