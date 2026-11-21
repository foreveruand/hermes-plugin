"""airss 仪表盘「RSS 展板」后端（Hermes dashboard plugin）。

路由由 Hermes 挂到 /api/plugins/airss/ 下。

安全约束：绝不写推送语义字段——articles.is_sent 与 article_sent 是
Telegram 推送语义，改了会吞推送，展板任何接口都不碰它们。
展板自己的已读/收藏/反馈状态只存本模块新建的 board_state 表；
面板设置存 board_config 表。在此之外，用户明确授权展板写「配置字段」
（2026-11-10）：subscriptions 的 GLOBAL_CONFIGURABLE_FIELDS（名称、URL、
抓取间隔、分组、关键词、Cookies、图片上限、代理、总结开关、暂停开关）
以及 groups 的 name / persona_prompt / schedules（分组名、总结提示词与
排期）——都是配置，不是投递状态。

数据源与 airss 插件一致：<profile>/data/airss/rss.db。
profile 优先从插件安装位置推断（<profile>/plugins/airss/dashboard/plugin_api.py），
因为 gateway/dashboard 多 profile 进程里的 HERMES_HOME 可能指向 Hermes 根目录，
而不是当前插件所属的 profile；推断失败才回退 HERMES_HOME、~/.hermes。
数据库文件不存在时不抛 500：meta 返回 available:false，items 返回空列表。

文件末尾另有一组不带 /board 前缀的「阅读器」接口（/subscriptions、/articles、
/groups），供独立 RSS 阅读界面使用。面板用户是全局唯一虚拟用户，不落
subscribers 表；因此 subscribers 表里的行都是真实的 Gateway/命令行接收端，
has_gateway_subscriber 直接由它的行数判定。

阅读增强接口（批量已读、翻译、注解、订阅可见性）只写 board_state /
article_notes / translate_config / article_translations 与订阅的
visible_in_panel 配置列，同样不碰 articles.is_sent / article_sent。
"""

from __future__ import annotations

import json
import os
import re
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

def _profile_dir() -> Path:
    """解析当前插件所属 profile 目录。

    安装路径是 <profile>/plugins/airss/dashboard/plugin_api.py，所以
    parents[2] 是 plugins、parents[3] 是 profile。仓库 checkout 里路径
    不满足这个形状，回退 HERMES_HOME / ~/.hermes，避免影响本地调试。
    """
    # 不用 resolve()：插件目录本身可能是软链接，跟随到仓库会丢掉 profile 形状。
    here = Path(__file__).absolute()
    if len(here.parents) > 3 and here.parents[2].name == "plugins":
        return here.parents[3]
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home)
    return Path.home() / ".hermes"


def _db_path() -> Path:
    return _profile_dir() / "data" / "airss" / "rss.db"


def _ensure_board_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS board_state (
            article_id INTEGER PRIMARY KEY,
            read_at TEXT,
            feedback INTEGER NOT NULL DEFAULT 0,
            starred INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS board_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    # 阅读器扩展表（与数据库本体共用 rss.db；只服务面板，不参与推送）。
    # article_notes：每篇文章一条站内注解。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_notes (
            id INTEGER PRIMARY KEY,
            article_id INTEGER NOT NULL,
            note_text TEXT,
            created_at TEXT,
            UNIQUE(article_id)
        )
        """
    )
    # translate_config：按订阅保存默认目标语言与是否自动翻译。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS translate_config (
            subscription_id INTEGER PRIMARY KEY,
            target_language TEXT,
            auto_translate BOOLEAN DEFAULT 0,
            created_at TEXT
        )
        """
    )
    # article_translations：翻译结果按（文章, 语言）缓存，重复打开不再调 LLM。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_translations (
            id INTEGER PRIMARY KEY,
            article_id INTEGER NOT NULL,
            target_language TEXT NOT NULL,
            translated_title TEXT,
            translated_content TEXT,
            created_at TEXT,
            UNIQUE(article_id, target_language)
        )
        """
    )
    # 老库迁移：board_state 早期版本没有 starred 列
    cols = [r[1] for r in conn.execute("PRAGMA table_info(board_state)")]
    if cols and "starred" not in cols:
        conn.execute(
            "ALTER TABLE board_state ADD COLUMN starred INTEGER NOT NULL DEFAULT 0"
        )
    # 老库迁移：subscriptions 早期版本没有 visible_in_panel 列。
    # 只影响面板展示，默认 1 让现有订阅保持可见；Gateway 推送不受影响。
    sub_cols = [r[1] for r in conn.execute("PRAGMA table_info(subscriptions)")]
    if sub_cols and "visible_in_panel" not in sub_cols:
        conn.execute(
            "ALTER TABLE subscriptions "
            "ADD COLUMN visible_in_panel INTEGER NOT NULL DEFAULT 1"
        )
    conn.commit()


def _get_board_config(conn: sqlite3.Connection, key: str):
    try:
        row = conn.execute(
            "SELECT value FROM board_config WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _set_board_config(conn: sqlite3.Connection, key: str, value) -> None:
    if value is None:
        conn.execute("DELETE FROM board_config WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO board_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.commit()


# 与 airss/models.py 的 GLOBAL_CONFIGURABLE_FIELDS 保持一致。展板进程
# 无法 import 插件本体，只能在写库前用白名单校验列名。
_SUB_CONFIG_FIELDS = (
    "name",
    "url",
    "black_keyword",
    "cookies",
    "content_to_remove",
    "max_image_number",
    "interval",
    "ai_summary_enabled",
    "enable_proxy",
    "stop",
    "source_group_id",
    # 面板专有字段，不属于 GLOBAL_CONFIGURABLE_FIELDS：只控制是否在面板
    # 展示订阅及其文章，不影响 Gateway 推送。
    "visible_in_panel",
)

_SUB_SELECT = (
    "id, name, url, interval, source_group_id, cookies, black_keyword, "
    "content_to_remove, max_image_number, ai_summary_enabled, enable_proxy, "
    "stop, visible_in_panel, error_count, last_fetch"
)

_BOOL_SUB_FIELDS = {"ai_summary_enabled", "enable_proxy", "stop", "visible_in_panel"}


def _subscription_dict(row: sqlite3.Row, unread: int = 0) -> dict:
    """把 subscriptions 行转成展板使用的完整配置（只读状态另外附带）。"""
    return {
        "id": row["id"],
        "name": row["name"] or "",
        "url": row["url"] or "",
        "group_id": row["source_group_id"],
        "source_group_id": row["source_group_id"],
        "interval": row["interval"] if row["interval"] is not None else 5,
        "cookies": row["cookies"] or "",
        "black_keyword": row["black_keyword"] or "",
        "content_to_remove": row["content_to_remove"] or "",
        "max_image_number": row["max_image_number"] or 0,
        "ai_summary_enabled": bool(row["ai_summary_enabled"]),
        "enable_proxy": bool(row["enable_proxy"]),
        "stop": bool(row["stop"]),
        "visible_in_panel": bool(row["visible_in_panel"]),
        "error_count": row["error_count"] or 0,
        "last_fetch": row["last_fetch"],
        "unread": unread,
    }


def _clean_sub_value(key: str, value):
    """校验并归一化单个订阅配置值；非法值直接转成 400。"""
    def bad(message: str):
        raise HTTPException(status_code=400, detail=message)

    if key in _BOOL_SUB_FIELDS:
        if not isinstance(value, bool):
            bad(f"{key} must be a boolean")
        return 1 if value else 0
    if key in ("interval", "source_group_id", "max_image_number"):
        if isinstance(value, bool) or value is None:
            bad(f"{key} must be an integer")
        try:
            num = int(value)
        except (TypeError, ValueError):
            bad(f"{key} must be an integer")
        if key == "interval" and not (1 <= num <= 10080):
            bad("interval must be between 1 and 10080 minutes")
        if key == "source_group_id" and num <= 0:
            bad("source_group_id must be positive")
        if key == "max_image_number" and num < 0:
            bad("max_image_number must be >= 0")
        return num
    if not isinstance(value, str):
        bad(f"{key} must be a string")
    if key in ("name", "url"):
        value = value.strip()
        if not value:
            bad(f"{key} must not be empty")
        if len(value) > (500 if key == "url" else 200):
            bad(f"{key} is too long")
        return value
    if len(value) > 10000:
        bad(f"{key} is too long (max 10000 chars)")
    return value


def _require_group(conn: sqlite3.Connection, group_id: int) -> None:
    row = conn.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail=f"group {group_id} not found")


def _get_subscription(conn: sqlite3.Connection, sub_id: int):
    return conn.execute(
        "SELECT " + _SUB_SELECT + " FROM subscriptions WHERE id = ?", (sub_id,)
    ).fetchone()


# 与 airss/digest.py 的 DEFAULT_SYSTEM_PROMPT 保持一致（展板进程无法直接
# import 插件本体，这里存一份只读副本，仅用于面板展示与「恢复默认」对照）。
DEFAULT_SUMMARY_PROMPT = (
    "你是一位资深资讯编辑，负责把零散文章整理成可以快速扫读的中文日报。\n"
    "本任务为纯归纳模式：严禁尝试调用任何外部工具或网络搜索，禁止提及工具调用，"
    "必须且只能基于下方提供的 RSS 文章内容进行归纳。\n"
    "输出会被直接排版成一张卡片，请严格按以下格式输出 Markdown：\n"
    "1. 第一行用 `# ` 写一个 24 字以内的总标题，概括当天最值得关注的主线；\n"
    "2. 用 3-6 个 `## ` 小节归类内容，小节标题控制在 12 字以内；\n"
    "3. 每个小节写 2-5 条 `- ` 要点，先陈述事实再点出影响或看点，单条不超过 70 字；\n"
    "4. 用 **粗体** 标出关键主体、数字或结论，不要整句加粗；\n"
    "5. 信息足够时，用 `## 观察` 收尾，写 1-2 句编辑判断；\n"
    "6. 只依据提供的文章内容归纳，不编造、不复述原文链接，不使用代码块、表格、图片与多级嵌套；\n"
    "7. 严格禁止开场白与规划语：不要输出「好的」「接下来我将」「先核对」等任何问候或思考过程，"
    "从第一行 `# ` 标题直接开始。"
)


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
    bs.read_at AS board_read_at, bs.feedback AS board_feedback,
    bs.starred AS board_starred,
    EXISTS(SELECT 1 FROM article_sent _sent WHERE _sent.article_id = a.id) AS delivered
"""

_YT_ID_PATTERNS = (
    "youtube.com/watch?v=",
    "youtube.com/shorts/",
    "youtube.com/live/",
    "youtu.be/",
)


def _youtube_thumb(link: str):
    """YouTube 条目没图时按视频 ID 拼封面（与 Muse RSS 展板同款兜底）。"""
    if not link:
        return None
    vid = None
    for marker in _YT_ID_PATTERNS:
        idx = link.find(marker)
        if idx >= 0:
            rest = link[idx + len(marker):]
            vid = ""
            for ch in rest:
                if ch.isalnum() or ch in "-_":
                    vid += ch
                else:
                    break
            break
    if vid and len(vid) >= 6:
        return "https://i.ytimg.com/vi/" + vid + "/hqdefault.jpg"
    return None

_ITEM_FROM = """
FROM articles a
JOIN subscriptions s ON s.id = a.subscription_id
LEFT JOIN board_state bs ON bs.article_id = a.id
"""


def _row_to_item(row: sqlite3.Row, image_urls: list) -> dict:
    keys = row.keys()
    image = image_urls[0] if image_urls else None
    if not image:
        image = _youtube_thumb(row["link"] or "")
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
        "image": image,
        "has_content": bool(row["has_content"]) if "has_content" in keys else False,
        "is_sent": bool(row["is_sent"]),
        "delivered": bool(row["delivered"]) if "delivered" in keys else False,
        "ai_filter_result": row["ai_filter_result"],
        # 推送成功过（article_sent 有任一接收端记录）也算已读；
        # 展板自己的 read_at 只是站内阅读状态，两者取并集。
        "read": (row["board_read_at"] is not None)
        or (bool(row["delivered"]) if "delivered" in keys else False),
        "feedback": row["board_feedback"] if row["board_feedback"] is not None else 0,
        "starred": bool(row["board_starred"]) if "board_starred" in keys and row["board_starred"] is not None else False,
    }


def _filter_clauses(view, group_id, subscription_id, q, include_hidden: bool = False):
    clauses, params = [], []
    if view == "unread":
        clauses.append("bs.read_at IS NULL")
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM article_sent _sent WHERE _sent.article_id = a.id)"
        )
    elif view == "starred":
        clauses.append("bs.starred = 1")
    elif view == "filtered":
        clauses.append("a.ai_filter_result = 0")
    # view == "all" 不加状态条件
    # 面板视图默认隐藏 visible_in_panel=0 的订阅；订阅管理/调试可传 include_hidden。
    if not include_hidden:
        clauses.append("s.visible_in_panel = 1")
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
            "error": "airss 数据库不存在",
            "counts": {"total": 0, "unread": 0, "starred": 0, "filtered": 0},
            "groups": [],
            "subscriptions": [],
        }
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM articles a "
            "JOIN subscriptions s ON s.id = a.subscription_id "
            "WHERE s.visible_in_panel = 1"
        ).fetchone()[0]
        unread = conn.execute(
            "SELECT COUNT(*) FROM articles a "
            "JOIN subscriptions s ON s.id = a.subscription_id "
            "LEFT JOIN board_state bs ON bs.article_id = a.id "
            "WHERE s.visible_in_panel = 1 AND bs.read_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM article_sent _sent WHERE _sent.article_id = a.id)"
        ).fetchone()[0]
        starred = conn.execute(
            "SELECT COUNT(*) FROM board_state bs "
            "JOIN articles a ON a.id = bs.article_id "
            "JOIN subscriptions s ON s.id = a.subscription_id "
            "WHERE bs.starred = 1 AND s.visible_in_panel = 1"
        ).fetchone()[0]
        filtered = conn.execute(
            "SELECT COUNT(*) FROM articles a "
            "JOIN subscriptions s ON s.id = a.subscription_id "
            "WHERE a.ai_filter_result = 0 AND s.visible_in_panel = 1"
        ).fetchone()[0]
        unread_by_sub = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT a.subscription_id, COUNT(*) FROM articles a "
                "JOIN subscriptions s ON s.id = a.subscription_id "
                "LEFT JOIN board_state bs ON bs.article_id = a.id "
                "WHERE s.visible_in_panel = 1 AND bs.read_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM article_sent _sent WHERE _sent.article_id = a.id) "
                "GROUP BY a.subscription_id"
            )
        }
        unread_by_group = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT s.source_group_id, COUNT(*) FROM articles a "
                "JOIN subscriptions s ON s.id = a.subscription_id "
                "LEFT JOIN board_state bs ON bs.article_id = a.id "
                "WHERE s.visible_in_panel = 1 AND bs.read_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM article_sent _sent WHERE _sent.article_id = a.id) "
                "GROUP BY s.source_group_id"
            )
        }
        groups = []
        for r in conn.execute(
            "SELECT id, name, schedules, persona_prompt FROM groups ORDER BY id"
        ):
            try:
                schedules = json.loads(r["schedules"] or "[]")
            except Exception:
                schedules = []
            groups.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "schedules": schedules if isinstance(schedules, list) else [],
                    "has_persona_prompt": bool((r["persona_prompt"] or "").strip()),
                    "unread": unread_by_group.get(r["id"], 0),
                }
            )
        subscriptions = [
            _subscription_dict(r, unread_by_sub.get(r["id"], 0))
            for r in conn.execute(
                "SELECT " + _SUB_SELECT + " FROM subscriptions ORDER BY id"
            )
        ]
        return {
            "available": True,
            "counts": {
                "total": total,
                "unread": unread,
                "starred": starred,
                "filtered": filtered,
            },
            "groups": groups,
            "subscriptions": subscriptions,
        }
    except sqlite3.Error as exc:
        return {
            "available": False,
            "error": str(exc),
            "counts": {"total": 0, "unread": 0, "starred": 0, "filtered": 0},
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
    include_hidden: bool = False,
):
    conn = _connect()
    if conn is None:
        return {"available": False, "items": [], "total": 0}
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    clauses, params = _filter_clauses(
        view, group_id, subscription_id, q, include_hidden=include_hidden
    )
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


@router.post("/board/items/{item_id}/star")
def board_set_star(item_id: int, payload: dict = Body(...)):
    value = payload.get("value") if isinstance(payload, dict) else None
    if value not in (0, 1, True, False):
        raise HTTPException(status_code=400, detail="value must be 0 or 1")
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        conn.execute(
            """
            INSERT INTO board_state (article_id, read_at, feedback, starred)
            VALUES (?, NULL, 0, ?)
            ON CONFLICT(article_id) DO UPDATE SET starred = excluded.starred
            """,
            (item_id, 1 if value else 0),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.post("/board/read-many")
def board_read_many(payload: dict = Body(...)):
    """批量标已读：滚动到底自动已读用，一次最多 200 条。"""
    ids = payload.get("ids") if isinstance(payload, dict) else None
    if not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list")
    ids = [int(i) for i in ids if isinstance(i, int) or str(i).isdigit()][:200]
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False, "marked": 0}
    try:
        now = _now_iso()
        marked = 0
        for article_id in ids:
            cur = conn.execute(
                """
                INSERT INTO board_state (article_id, read_at, feedback, starred)
                VALUES (?, ?, 0, 0)
                ON CONFLICT(article_id) DO UPDATE
                SET read_at = COALESCE(board_state.read_at, excluded.read_at)
                """,
                (article_id, now),
            )
            marked += cur.rowcount or 0
        conn.commit()
        return {"ok": True, "marked": marked}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 订阅配置（用户授权展板可写的配置字段：总结开关 / 暂停开关）
# ---------------------------------------------------------------------------

@router.post("/board/subscriptions/{sub_id}/summary")
def board_set_subscription_summary(sub_id: int, payload: dict = Body(...)):
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be a boolean")
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        cur = conn.execute(
            "UPDATE subscriptions SET ai_summary_enabled = ? WHERE id = ?",
            (1 if enabled else 0, sub_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="subscription not found")
        return {"ok": True, "ai_summary_enabled": enabled}
    finally:
        conn.close()


@router.post("/board/subscriptions/{sub_id}/stop")
def board_set_subscription_stop(sub_id: int, payload: dict = Body(...)):
    stopped = payload.get("stopped") if isinstance(payload, dict) else None
    if not isinstance(stopped, bool):
        raise HTTPException(status_code=400, detail="stopped must be a boolean")
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        cur = conn.execute(
            "UPDATE subscriptions SET stop = ? WHERE id = ?",
            (1 if stopped else 0, sub_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="subscription not found")
        return {"ok": True, "stop": stopped}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 订阅完整配置：与 airss 的 GLOBAL_CONFIGURABLE_FIELDS 对齐
# 展板可读写 name / url / interval / source_group_id / cookies /
# black_keyword / content_to_remove / max_image_number / ai_summary_enabled /
# enable_proxy / stop；error_count / last_fetch 只读展示。
# ---------------------------------------------------------------------------

@router.get("/board/subscriptions")
def board_subscriptions():
    conn = _connect()
    if conn is None:
        return {"available": False, "subscriptions": []}
    try:
        subs = [
            _subscription_dict(r)
            for r in conn.execute("SELECT " + _SUB_SELECT + " FROM subscriptions ORDER BY id")
        ]
        return {"available": True, "subscriptions": subs}
    except sqlite3.Error as exc:
        return {"available": False, "error": str(exc), "subscriptions": []}
    finally:
        conn.close()


@router.get("/board/subscriptions/{sub_id}")
def board_subscription_detail(sub_id: int):
    conn = _connect()
    if conn is None:
        return {"available": False}
    try:
        row = _get_subscription(conn, sub_id)
        if row is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        group = conn.execute(
            "SELECT name FROM groups WHERE id = ?", (row["source_group_id"],)
        ).fetchone()
        item = _subscription_dict(row)
        item["group_name"] = group["name"] if group else ""
        item["available"] = True
        return item
    finally:
        conn.close()


@router.post("/board/subscriptions")
def board_create_subscription(payload: dict = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    url = _clean_sub_value("url", payload.get("url"))
    name = _clean_sub_value("name", payload.get("name") or url[:200])
    interval = _clean_sub_value("interval", payload.get("interval", 5))
    group_id = _clean_sub_value("source_group_id", payload.get("source_group_id", 1))
    values = {
        "name": name,
        "url": url,
        "interval": interval,
        "source_group_id": group_id,
        "cookies": _clean_sub_value("cookies", payload.get("cookies", "")),
        "black_keyword": _clean_sub_value(
            "black_keyword", payload.get("black_keyword", "")
        ),
        "content_to_remove": _clean_sub_value(
            "content_to_remove", payload.get("content_to_remove", "")
        ),
        "max_image_number": _clean_sub_value(
            "max_image_number", payload.get("max_image_number", 0)
        ),
        "ai_summary_enabled": _clean_sub_value(
            "ai_summary_enabled", payload.get("ai_summary_enabled", False)
        ),
        "enable_proxy": _clean_sub_value(
            "enable_proxy", payload.get("enable_proxy", False)
        ),
        "stop": _clean_sub_value("stop", payload.get("stop", False)),
        "visible_in_panel": _clean_sub_value(
            "visible_in_panel", payload.get("visible_in_panel", True)
        ),
    }
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        _require_group(conn, values["source_group_id"])
        existing = conn.execute(
            "SELECT id FROM subscriptions WHERE url = ?", (values["url"],)
        ).fetchone()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"subscription with this URL already exists (id={existing['id']})",
            )
        columns = list(values.keys())
        cur = conn.execute(
            "INSERT INTO subscriptions (" + ", ".join(columns) + ") VALUES ("
            + ", ".join("?" for _ in columns) + ")",
            [values[c] for c in columns],
        )
        conn.commit()
        row = _get_subscription(conn, cur.lastrowid)
        return {"ok": True, "subscription": _subscription_dict(row)}
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    finally:
        conn.close()


@router.patch("/board/subscriptions/{sub_id}")
def board_update_subscription(sub_id: int, payload: dict = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    sets, values = [], []
    for key in _SUB_CONFIG_FIELDS:
        if key not in payload:
            continue
        value = _clean_sub_value(key, payload[key])
        sets.append(key + " = ?")
        values.append(value)
    if not sets:
        raise HTTPException(status_code=400, detail="no configurable fields provided")

    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        if _get_subscription(conn, sub_id) is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        if "source_group_id" in payload:
            _require_group(conn, int(payload["source_group_id"]))
        if "url" in payload:
            other = conn.execute(
                "SELECT id FROM subscriptions WHERE url = ? AND id != ?",
                (_clean_sub_value("url", payload["url"]), sub_id),
            ).fetchone()
            if other is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"subscription with this URL already exists (id={other['id']})",
                )
        values.append(sub_id)
        conn.execute(
            "UPDATE subscriptions SET " + ", ".join(sets) + " WHERE id = ?", values
        )
        conn.commit()
        row = _get_subscription(conn, sub_id)
        return {"ok": True, "subscription": _subscription_dict(row)}
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    finally:
        conn.close()


@router.delete("/board/subscriptions/{sub_id}")
def board_delete_subscription(sub_id: int):
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        row = _get_subscription(conn, sub_id)
        if row is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        article_count = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE subscription_id = ?", (sub_id,)
        ).fetchone()[0]
        subscriber_count = conn.execute(
            "SELECT COUNT(*) FROM subscribers WHERE subscription_id = ?", (sub_id,)
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM board_state WHERE article_id IN "
            "(SELECT id FROM articles WHERE subscription_id = ?)",
            (sub_id,),
        )
        conn.execute(
            "DELETE FROM article_sent WHERE article_id IN "
            "(SELECT id FROM articles WHERE subscription_id = ?)",
            (sub_id,),
        )
        conn.execute(
            "DELETE FROM article_notes WHERE article_id IN "
            "(SELECT id FROM articles WHERE subscription_id = ?)",
            (sub_id,),
        )
        conn.execute(
            "DELETE FROM article_translations WHERE article_id IN "
            "(SELECT id FROM articles WHERE subscription_id = ?)",
            (sub_id,),
        )
        conn.execute("DELETE FROM translate_config WHERE subscription_id = ?", (sub_id,))
        conn.execute("DELETE FROM subscribers WHERE subscription_id = ?", (sub_id,))
        conn.execute("DELETE FROM articles WHERE subscription_id = ?", (sub_id,))
        conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
        conn.commit()
        return {
            "ok": True,
            "deleted_articles": article_count,
            "deleted_subscribers": subscriber_count,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 分组配置：名称 / 排期 / 提示词与 airss 的 rss_group 工具保持一致
# ---------------------------------------------------------------------------

def _clean_schedules(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail="schedules must be a list")
    cleaned = []
    for item in value:
        if not isinstance(item, str):
            raise HTTPException(status_code=400, detail="schedules must be strings")
        item = item.strip()
        if item:
            cleaned.append(item)
    if len(cleaned) > 10:
        raise HTTPException(status_code=400, detail="at most 10 schedules per group")
    return cleaned


@router.post("/board/groups")
def board_create_group(payload: dict = Body(...)):
    name = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    name = name.strip()
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="name too long (max 100 chars)")
    schedules = _clean_schedules(payload.get("schedules"))
    prompt = payload.get("persona_prompt") or ""
    if not isinstance(prompt, str):
        raise HTTPException(status_code=400, detail="persona_prompt must be a string")
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        cur = conn.execute(
            "INSERT INTO groups (name, schedules, persona_id, persona_prompt) "
            "VALUES (?, ?, NULL, ?)",
            (name, json.dumps(schedules), prompt.strip() or None),
        )
        conn.commit()
        return {"ok": True, "group_id": cur.lastrowid}
    finally:
        conn.close()


@router.patch("/board/groups/{group_id}")
def board_update_group(group_id: int, payload: dict = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    sets, values = [], []
    if "name" in payload:
        name = payload["name"]
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(status_code=400, detail="name must not be empty")
        name = name.strip()
        if len(name) > 100:
            raise HTTPException(status_code=400, detail="name too long (max 100 chars)")
        sets.append("name = ?")
        values.append(name)
    if "schedules" in payload:
        sets.append("schedules = ?")
        values.append(json.dumps(_clean_schedules(payload["schedules"])))
    if "persona_prompt" in payload:
        prompt = payload["persona_prompt"]
        if not isinstance(prompt, str):
            raise HTTPException(status_code=400, detail="persona_prompt must be a string")
        prompt = prompt.strip()
        if len(prompt) > 8000:
            raise HTTPException(status_code=400, detail="prompt too long (max 8000 chars)")
        sets.append("persona_prompt = ?")
        values.append(prompt or None)
    if not sets:
        raise HTTPException(status_code=400, detail="no fields provided")
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        values.append(group_id)
        cur = conn.execute(
            "UPDATE groups SET " + ", ".join(sets) + " WHERE id = ?", values
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="group not found")
        return {"ok": True}
    finally:
        conn.close()


@router.delete("/board/groups/{group_id}")
def board_delete_group(group_id: int):
    if group_id == 1:
        raise HTTPException(status_code=400, detail="cannot delete default group (id=1)")
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE source_group_id = ?", (group_id,)
        ).fetchone()[0]
        if count:
            raise HTTPException(
                status_code=409,
                detail=f"group still has {count} subscription(s); move them first",
            )
        cur = conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="group not found")
        return {"ok": True}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 分组总结配置：提示词（分组覆盖 > 面板全局 > 内置默认）与排期
# 与 airss/digest.py 的口径一致：DigestService 生成总结时按同一优先级取词。
# ---------------------------------------------------------------------------

def _prompt_source(group_persona: str, global_prompt: str) -> str:
    if group_persona:
        return "group"
    if global_prompt:
        return "global"
    return "default"


@router.get("/board/summary")
def board_summary_config():
    conn = _connect()
    if conn is None:
        return {
            "available": False,
            "error": "airss 数据库不存在",
            "global_prompt": "",
            "groups": [],
            "subscriptions": [],
        }
    try:
        global_prompt = (_get_board_config(conn, "summary_prompt") or "").strip()
        groups = []
        for r in conn.execute(
            "SELECT id, name, schedules, persona_prompt FROM groups ORDER BY id"
        ):
            try:
                schedules = json.loads(r["schedules"] or "[]")
            except Exception:
                schedules = []
            persona = (r["persona_prompt"] or "").strip()
            subs = [
                _subscription_dict(s)
                for s in conn.execute(
                    "SELECT " + _SUB_SELECT + " FROM subscriptions "
                    "WHERE source_group_id = ? ORDER BY id",
                    (r["id"],),
                )
            ]
            groups.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "schedules": schedules if isinstance(schedules, list) else [],
                    "persona_prompt": persona,
                    "prompt_source": _prompt_source(persona, global_prompt),
                    "subscriptions": subs,
                }
            )
        all_subs = [
            _subscription_dict(s)
            for s in conn.execute("SELECT " + _SUB_SELECT + " FROM subscriptions ORDER BY id")
        ]
        return {
            "available": True,
            "global_prompt": global_prompt,
            "default_prompt": DEFAULT_SUMMARY_PROMPT,
            "groups": groups,
            "subscriptions": all_subs,
        }
    except sqlite3.Error as exc:
        return {
            "available": False,
            "error": str(exc),
            "global_prompt": "",
            "groups": [],
            "subscriptions": [],
        }
    finally:
        conn.close()


@router.post("/board/summary/global-prompt")
def board_set_global_prompt(payload: dict = Body(...)):
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(prompt, str):
        raise HTTPException(status_code=400, detail="prompt must be a string")
    prompt = prompt.strip()
    if len(prompt) > 8000:
        raise HTTPException(status_code=400, detail="prompt too long (max 8000 chars)")
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        # 空串 = 清除面板全局提示词，回落到分组覆盖 / 内置默认
        _set_board_config(conn, "summary_prompt", prompt or None)
        return {"ok": True, "global_prompt": prompt}
    finally:
        conn.close()


@router.post("/board/summary/group-prompt")
def board_set_group_prompt(payload: dict = Body(...)):
    group_id = payload.get("group_id") if isinstance(payload, dict) else None
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(group_id, int) or not isinstance(prompt, str):
        raise HTTPException(status_code=400, detail="group_id and prompt are required")
    prompt = prompt.strip()
    if len(prompt) > 8000:
        raise HTTPException(status_code=400, detail="prompt too long (max 8000 chars)")
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        cur = conn.execute(
            "UPDATE groups SET persona_prompt = ? WHERE id = ?",
            (prompt or None, group_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="group not found")
        return {"ok": True}
    finally:
        conn.close()


@router.post("/board/summary/group-schedules")
def board_set_group_schedules(payload: dict = Body(...)):
    group_id = payload.get("group_id") if isinstance(payload, dict) else None
    schedules = payload.get("schedules") if isinstance(payload, dict) else None
    if not isinstance(group_id, int) or not isinstance(schedules, list):
        raise HTTPException(status_code=400, detail="group_id and schedules are required")
    cleaned = []
    for s in schedules:
        if not isinstance(s, str):
            raise HTTPException(status_code=400, detail="schedules must be strings")
        s = s.strip()
        if s:
            cleaned.append(s)
    if len(cleaned) > 10:
        raise HTTPException(status_code=400, detail="at most 10 schedules per group")
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        cur = conn.execute(
            "UPDATE groups SET schedules = ? WHERE id = ?",
            (json.dumps(cleaned), group_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="group not found")
        return {"ok": True, "schedules": cleaned}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 阅读器接口（不带 /board 前缀）
#
# 面板用户 = 全局唯一虚拟用户，不在 subscribers 表里建记录；所以这里看到的
# subscribers 都是真实的 Gateway/命令行接收端。面板的已读/收藏仍只写
# board_state，绝不碰 articles.is_sent / article_sent。
# ---------------------------------------------------------------------------

def _reader_counts(conn: sqlite3.Connection, sub_id: int):
    """返回 (未读数, 真实订阅者数)。未读口径与 /board 一致。"""
    unread = conn.execute(
        "SELECT COUNT(*) FROM articles a "
        "LEFT JOIN board_state bs ON bs.article_id = a.id "
        "WHERE a.subscription_id = ? AND bs.read_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM article_sent _sent WHERE _sent.article_id = a.id)",
        (sub_id,),
    ).fetchone()[0]
    subscribers = conn.execute(
        "SELECT COUNT(*) FROM subscribers WHERE subscription_id = ?", (sub_id,)
    ).fetchone()[0]
    return unread, subscribers


def _reader_subscription_dict(row: sqlite3.Row, unread: int, subscribers: int) -> dict:
    """订阅配置 + 阅读器需要的统计字段。

    has_gateway_subscriber 表示是否存在真实推送接收端；面板虚拟用户不计入。
    """
    item = _subscription_dict(row, unread)
    item["unread_count"] = unread
    item["subscriber_count"] = subscribers
    item["has_gateway_subscriber"] = subscribers > 0
    return item


def _normalize_sub_payload(payload: dict) -> dict:
    """接受 group_id 作为 source_group_id 的别名（前端两种叫法都出现过）。"""
    if "group_id" in payload and "source_group_id" not in payload:
        payload = dict(payload)
        payload["source_group_id"] = payload["group_id"]
    return payload


def _parse_personal_config(raw) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _adapter_type(umo: str) -> str:
    """与 airss/models.py 的 Subscriber.get_adapter 保持同一口径。"""
    value = (umo or "").strip()
    if value.startswith("http://") or value.startswith("https://"):
        return "webhook"
    head, sep, _ = value.partition(":")
    return head if sep else "telegram"


@router.get("/subscriptions")
def list_subscriptions(include_hidden: bool = False):
    conn = _connect()
    if conn is None:
        return {"available": False, "subscriptions": []}
    try:
        visible_clause = "" if include_hidden else " WHERE s.visible_in_panel = 1"
        unread_by_sub = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT a.subscription_id, COUNT(*) FROM articles a "
                "JOIN subscriptions s ON s.id = a.subscription_id "
                "LEFT JOIN board_state bs ON bs.article_id = a.id "
                "WHERE bs.read_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM article_sent _sent WHERE _sent.article_id = a.id)"
                + (" AND s.visible_in_panel = 1" if not include_hidden else "")
                + " GROUP BY a.subscription_id"
            )
        }
        subscribers_by_sub = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT subscription_id, COUNT(*) FROM subscribers GROUP BY subscription_id"
            )
        }
        subs = [
            _reader_subscription_dict(
                r,
                unread_by_sub.get(r["id"], 0),
                subscribers_by_sub.get(r["id"], 0),
            )
            for r in conn.execute(
                "SELECT " + _SUB_SELECT + " FROM subscriptions s"
                + visible_clause
                + " ORDER BY s.id"
            )
        ]
        return {"available": True, "subscriptions": subs}
    except sqlite3.Error as exc:
        return {"available": False, "error": str(exc), "subscriptions": []}
    finally:
        conn.close()


@router.post("/subscriptions")
def create_subscription(payload: dict = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    payload = _normalize_sub_payload(payload)
    created = board_create_subscription(payload)
    if not created.get("ok"):
        return created
    sub = created["subscription"]
    sub["unread_count"] = 0
    sub["subscriber_count"] = 0
    sub["has_gateway_subscriber"] = False
    return {"ok": True, "id": sub["id"], "subscription": sub}


@router.put("/subscriptions/{subscription_id}")
def update_subscription(subscription_id: int, payload: dict = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    payload = _normalize_sub_payload(payload)
    # 只认 _SUB_CONFIG_FIELDS：其余字段（is_sent、id、统计字段等）一律忽略，
    # 保证推送语义字段不会被面板改写。
    sets, values = [], []
    for key in _SUB_CONFIG_FIELDS:
        if key not in payload:
            continue
        value = _clean_sub_value(key, payload[key])
        sets.append(key + " = ?")
        values.append(value)
    if not sets:
        raise HTTPException(status_code=400, detail="no configurable fields provided")

    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        if _get_subscription(conn, subscription_id) is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        if "source_group_id" in payload:
            _require_group(conn, int(payload["source_group_id"]))
        if "url" in payload:
            other = conn.execute(
                "SELECT id FROM subscriptions WHERE url = ? AND id != ?",
                (_clean_sub_value("url", payload["url"]), subscription_id),
            ).fetchone()
            if other is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"subscription with this URL already exists (id={other['id']})",
                )
        values.append(subscription_id)
        conn.execute(
            "UPDATE subscriptions SET " + ", ".join(sets) + " WHERE id = ?", values
        )
        conn.commit()
        row = _get_subscription(conn, subscription_id)
        unread, subscribers = _reader_counts(conn, subscription_id)
        return {"ok": True, "subscription": _reader_subscription_dict(row, unread, subscribers)}
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    finally:
        conn.close()


@router.delete("/subscriptions/{subscription_id}")
def delete_subscription(subscription_id: int):
    """从面板删除订阅源：清空订阅者并置 stop=true，保留订阅行与历史文章。

    面板是共享库上的虚拟用户，直接删行会连带毁掉 Gateway 订阅者的历史；
    这里按「退订 + 暂停抓取」处理：subscribers 全删，stop=1，文章留给阅读器。
    """
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        if _get_subscription(conn, subscription_id) is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        subscriber_count = conn.execute(
            "SELECT COUNT(*) FROM subscribers WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE subscriptions SET stop = 1 WHERE id = ?", (subscription_id,)
        )
        conn.execute(
            "DELETE FROM article_sent WHERE subscriber_id IN "
            "(SELECT id FROM subscribers WHERE subscription_id = ?)",
            (subscription_id,),
        )
        conn.execute(
            "DELETE FROM subscribers WHERE subscription_id = ?", (subscription_id,)
        )
        conn.commit()
        return {
            "ok": True,
            "stop": True,
            "deleted_subscribers": subscriber_count,
            "remaining_subscribers": 0,
        }
    finally:
        conn.close()


@router.get("/articles")
def list_articles(
    subscription_id: Optional[int] = None,
    unread_only: bool = False,
    visible_only: bool = False,
    keyword: str = "",
    limit: int = 20,
    offset: int = 0,
):
    conn = _connect()
    if conn is None:
        return {"available": False, "items": [], "total": 0}
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    clauses, params = [], []
    if subscription_id is not None:
        clauses.append("a.subscription_id = ?")
        params.append(subscription_id)
    if visible_only:
        clauses.append("s.visible_in_panel = 1")
    if unread_only:
        clauses.append("bs.read_at IS NULL")
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM article_sent _sent WHERE _sent.article_id = a.id)"
        )
    keyword = (keyword or "").strip()
    if keyword:
        clauses.append("(a.title LIKE ? OR a.author LIKE ?)")
        like = "%" + keyword + "%"
        params.extend([like, like])
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
    except sqlite3.Error as exc:
        return {"available": False, "error": str(exc), "items": [], "total": 0}
    finally:
        conn.close()


@router.post("/articles/{article_id}/mark-read")
def mark_article_read(article_id: int):
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        if conn.execute(
            "SELECT 1 FROM articles WHERE id = ?", (article_id,)
        ).fetchone() is None:
            raise HTTPException(status_code=404, detail="article not found")
        conn.execute(
            """
            INSERT INTO board_state (article_id, read_at, feedback) VALUES (?, ?, 0)
            ON CONFLICT(article_id) DO UPDATE
            SET read_at = COALESCE(board_state.read_at, excluded.read_at)
            """,
            (article_id, _now_iso()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT read_at FROM board_state WHERE article_id = ?", (article_id,)
        ).fetchone()
        return {
            "ok": True,
            "article_id": article_id,
            "read_at": row["read_at"] if row else None,
        }
    finally:
        conn.close()


@router.post("/articles/{article_id}/star")
def star_article(article_id: int, payload: Optional[dict] = Body(default=None)):
    """收藏/取消收藏；不传 value/starred 时按当前状态取反。"""
    desired = None
    if payload is not None:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be an object")
        if "value" in payload:
            raw = payload["value"]
        elif "starred" in payload:
            raw = payload["starred"]
        else:
            raise HTTPException(status_code=400, detail="value or starred is required")
        if isinstance(raw, bool):
            desired = raw
        elif raw in (0, 1):
            desired = bool(raw)
        else:
            raise HTTPException(status_code=400, detail="value must be a boolean")
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        if conn.execute(
            "SELECT 1 FROM articles WHERE id = ?", (article_id,)
        ).fetchone() is None:
            raise HTTPException(status_code=404, detail="article not found")
        row = conn.execute(
            "SELECT starred FROM board_state WHERE article_id = ?", (article_id,)
        ).fetchone()
        current = bool(row["starred"]) if row is not None else False
        starred = (not current) if desired is None else desired
        conn.execute(
            """
            INSERT INTO board_state (article_id, read_at, feedback, starred)
            VALUES (?, NULL, 0, ?)
            ON CONFLICT(article_id) DO UPDATE SET starred = excluded.starred
            """,
            (article_id, 1 if starred else 0),
        )
        conn.commit()
        return {"ok": True, "article_id": article_id, "starred": starred}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 阅读增强：批量已读 / 完整正文 / 翻译 / 注解 / 订阅翻译与可见性
#
# 翻译通过 Hermes 宿主提供的 agent.plugin_llm.PluginLlm（ctx.llm 同款 facade）
# 调用用户当前模型；dashboard 进程没有插件 ctx，所以这里按插件名直接构造，
# 失败时返回 503，绝不影响其它阅读接口。翻译结果写入 article_translations，
# 同一（文章, 语言）只调用一次 LLM。
# ---------------------------------------------------------------------------

_TRANSLATE_MAX_INPUT_CHARS = 12000
_TRANSLATE_MAX_TOKENS = 4096
_LLM_CLIENT = None
_LLM_CLIENT_RESOLVED = False


def set_llm_client(client) -> None:
    """注入 LLM 客户端（测试/宿主定制用）。

    生产环境不调用它：首次翻译时按 ``agent.plugin_llm.PluginLlm`` 懒加载。
    """
    global _LLM_CLIENT, _LLM_CLIENT_RESOLVED
    _LLM_CLIENT = client
    _LLM_CLIENT_RESOLVED = True


def _get_llm_client():
    global _LLM_CLIENT, _LLM_CLIENT_RESOLVED
    if not _LLM_CLIENT_RESOLVED:
        _LLM_CLIENT_RESOLVED = True
        try:
            from agent.plugin_llm import PluginLlm

            _LLM_CLIENT = PluginLlm(plugin_id="airss")
        except Exception:
            _LLM_CLIENT = None
    return _LLM_CLIENT


def _llm_result_text(result) -> str:
    if isinstance(result, str):
        return result
    text = getattr(result, "text", None)
    if text is None:
        text = str(result)
    return str(text)


def _call_llm_sync(messages: list, max_tokens: int = _TRANSLATE_MAX_TOKENS) -> str:
    client = _get_llm_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="翻译不可用：Hermes 未提供 agent.plugin_llm（请在 dashboard 进程中使用）",
        )
    try:
        if hasattr(client, "complete"):
            result = client.complete(messages, max_tokens=max_tokens, timeout=120.0)
        elif callable(client):
            result = client(messages=messages, max_tokens=max_tokens, timeout=120.0)
        else:
            raise TypeError("LLM client has no complete() method")
    except HTTPException:
        raise
    except Exception as exc:  # 网络/鉴权/配额错误统一转成可读的 502
        raise HTTPException(status_code=502, detail=f"翻译调用失败：{exc}")
    return _llm_result_text(result)


def _parse_translation_payload(raw: str):
    """把模型输出解析成 (title, content)；兼容 ```json 围栏与纯文本回退。"""
    text = (raw or "").strip()
    fenced = re.match(r"^\s*```[a-zA-Z]*\s*\n?(.*?)\n?```\s*$", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    data = None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        title = data.get("title") or data.get("translated_title") or ""
        content = data.get("content") or data.get("translated_content") or ""
        return str(title).strip(), str(content)
    # 模型只返回译文正文（没有 JSON）时，标题沿用原文。
    return "", text


def _translation_html(raw) -> str:
    """翻译正文按 HTML 清洗；纯文本则按空行拆段，避免整段挤成一行。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if re.search(r"<[a-zA-Z][^>]*>", text):
        return sanitize_html(text)
    escaped = _html_escape(text, quote=False)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", escaped) if p.strip()]
    return "".join(
        "<p>" + p.replace("\n", "<br/>") + "</p>" for p in paragraphs
    )


def _clean_target_language(value) -> str:
    if value is None:
        return "zh-CN"
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="target_language must be a string")
    value = value.strip()
    if not value:
        return "zh-CN"
    if len(value) > 32 or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", value):
        raise HTTPException(
            status_code=400,
            detail="target_language must be a BCP-47 style tag, e.g. zh-CN",
        )
    return value


def _require_article(conn: sqlite3.Connection, article_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, title, content, subscription_id FROM articles WHERE id = ?",
        (article_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="article not found")
    return row


@router.post("/articles/batch-mark-read")
def batch_mark_read(payload: dict = Body(...)):
    """批量标记已读/未读；只写 board_state.read_at，绝不碰推送语义字段。"""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    ids = payload.get("article_ids")
    read = payload.get("read", True)
    if not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="article_ids must be a list")
    if not isinstance(read, bool):
        raise HTTPException(status_code=400, detail="read must be a boolean")

    cleaned = []
    seen = set()
    for raw in ids:
        if isinstance(raw, bool):
            continue
        try:
            num = int(raw)
        except (TypeError, ValueError):
            continue
        if num > 0 and num not in seen:
            seen.add(num)
            cleaned.append(num)
    cleaned = cleaned[:500]

    conn = _connect()
    if conn is None:
        raise HTTPException(status_code=503, detail="airss 数据库不存在")
    try:
        if not cleaned:
            return {"ok": True, "count": 0, "read": read}
        placeholders = ",".join("?" for _ in cleaned)
        existing = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM articles WHERE id IN (" + placeholders + ")", cleaned
            )
        ]
        if not existing:
            return {"ok": True, "count": 0, "read": read}
        if read:
            now = _now_iso()
            conn.executemany(
                """
                INSERT INTO board_state (article_id, read_at, feedback) VALUES (?, ?, 0)
                ON CONFLICT(article_id) DO UPDATE
                SET read_at = COALESCE(board_state.read_at, excluded.read_at)
                """,
                [(article_id, now) for article_id in existing],
            )
            count = len(existing)
        else:
            cur = conn.execute(
                "UPDATE board_state SET read_at = NULL "
                "WHERE read_at IS NOT NULL AND article_id IN ("
                + placeholders
                + ")",
                existing,
            )
            count = cur.rowcount or 0
        conn.commit()
        return {"ok": True, "count": count, "read": read}
    finally:
        conn.close()


@router.get("/articles/{article_id}/full-content")
def get_full_content(article_id: int):
    """返回库内已有正文；is_fetched 预留给后续按原文链接抓取全文。"""
    conn = _connect()
    if conn is None:
        raise HTTPException(status_code=503, detail="airss 数据库不存在")
    try:
        row = _require_article(conn, article_id)
        content = sanitize_html(row["content"])
        return {
            "id": article_id,
            "content": content,
            "is_fetched": False,
            "has_content": bool(content.strip()),
        }
    finally:
        conn.close()


@router.post("/articles/{article_id}/translate")
def translate_article(article_id: int, payload: Optional[dict] = Body(default=None)):
    payload = payload if isinstance(payload, dict) else {}
    target_language = _clean_target_language(payload.get("target_language", "zh-CN"))
    force = bool(payload.get("force", False))

    conn = _connect()
    if conn is None:
        raise HTTPException(status_code=503, detail="airss 数据库不存在")
    try:
        row = _require_article(conn, article_id)
        if not force:
            cached = conn.execute(
                "SELECT translated_title, translated_content FROM article_translations "
                "WHERE article_id = ? AND target_language = ?",
                (article_id, target_language),
            ).fetchone()
            if cached is not None:
                return {
                    "id": article_id,
                    "target_language": target_language,
                    "translated_title": cached["translated_title"] or "",
                    "translated_content": cached["translated_content"] or "",
                    "cached": True,
                }
        title = row["title"] or ""
        content = row["content"] or ""
    finally:
        conn.close()

    source = str(content)[:_TRANSLATE_MAX_INPUT_CHARS]
    user_prompt = (
        f"请将以下 RSS 文章翻译成 {target_language}，保持 HTML 标记不变"
        "（原有标签、链接与段落结构不要新增或删除）。\n"
        '只返回一个 JSON 对象，格式为 {"title": "翻译后的标题", '
        '"content": "翻译后的正文"}，不要输出解释或 Markdown 围栏。\n\n'
        f"标题：{title}\n\n"
        f"正文：\n{source}"
    )
    raw = _call_llm_sync(
        [
            {
                "role": "system",
                "content": (
                    f"请将以下RSS文章翻译成{target_language}，保持HTML标记不变。"
                    "只输出指定 JSON。"
                ),
            },
            {"role": "user", "content": user_prompt},
        ]
    )
    translated_title, translated_content = _parse_translation_payload(raw)
    if not translated_content.strip():
        raise HTTPException(status_code=502, detail="翻译结果为空")
    translated_content = _translation_html(translated_content)
    translated_title = (translated_title or "").strip()

    conn = _connect()
    if conn is None:
        raise HTTPException(status_code=503, detail="airss 数据库不存在")
    try:
        conn.execute(
            """
            INSERT INTO article_translations
                (article_id, target_language, translated_title,
                 translated_content, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(article_id, target_language) DO UPDATE SET
                translated_title = excluded.translated_title,
                translated_content = excluded.translated_content,
                created_at = excluded.created_at
            """,
            (
                article_id,
                target_language,
                translated_title,
                translated_content,
                _now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "id": article_id,
        "target_language": target_language,
        "translated_title": translated_title,
        "translated_content": translated_content,
        "cached": False,
    }


@router.get("/articles/{article_id}/notes")
def get_article_notes(article_id: int):
    conn = _connect()
    if conn is None:
        raise HTTPException(status_code=503, detail="airss 数据库不存在")
    try:
        _require_article(conn, article_id)
        row = conn.execute(
            "SELECT note_text, created_at FROM article_notes WHERE article_id = ?",
            (article_id,),
        ).fetchone()
        return {
            "id": article_id,
            "note_text": (row["note_text"] if row else "") or "",
            "created_at": row["created_at"] if row else None,
        }
    finally:
        conn.close()


@router.post("/articles/{article_id}/notes")
def save_article_notes(article_id: int, payload: dict = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    note_text = payload.get("note_text")
    if not isinstance(note_text, str):
        raise HTTPException(status_code=400, detail="note_text must be a string")
    if len(note_text) > 20000:
        raise HTTPException(status_code=400, detail="note_text is too long (max 20000 chars)")
    note_text = note_text.strip()

    conn = _connect()
    if conn is None:
        raise HTTPException(status_code=503, detail="airss 数据库不存在")
    try:
        _require_article(conn, article_id)
        if not note_text:
            conn.execute("DELETE FROM article_notes WHERE article_id = ?", (article_id,))
            conn.commit()
            return {
                "ok": True,
                "id": article_id,
                "note_text": "",
                "created_at": None,
                "deleted": True,
            }
        now = _now_iso()
        conn.execute(
            """
            INSERT INTO article_notes (article_id, note_text, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
                note_text = excluded.note_text,
                created_at = excluded.created_at
            """,
            (article_id, note_text, now),
        )
        conn.commit()
        return {
            "ok": True,
            "id": article_id,
            "note_text": note_text,
            "created_at": now,
            "deleted": False,
        }
    finally:
        conn.close()


@router.get("/subscriptions/{subscription_id}/translate-config")
def get_translate_config(subscription_id: int):
    conn = _connect()
    if conn is None:
        raise HTTPException(status_code=503, detail="airss 数据库不存在")
    try:
        if _get_subscription(conn, subscription_id) is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        row = conn.execute(
            "SELECT target_language, auto_translate FROM translate_config "
            "WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()
        return {
            "subscription_id": subscription_id,
            "target_language": (row["target_language"] if row else None) or "zh-CN",
            "auto_translate": bool(row["auto_translate"]) if row else False,
            "configured": row is not None,
        }
    finally:
        conn.close()


@router.put("/subscriptions/{subscription_id}/translate-config")
def update_translate_config(subscription_id: int, payload: dict = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    target_language = _clean_target_language(payload.get("target_language", "zh-CN"))
    auto_translate = payload.get("auto_translate", False)
    if not isinstance(auto_translate, bool):
        raise HTTPException(status_code=400, detail="auto_translate must be a boolean")

    conn = _connect()
    if conn is None:
        raise HTTPException(status_code=503, detail="airss 数据库不存在")
    try:
        if _get_subscription(conn, subscription_id) is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        conn.execute(
            """
            INSERT INTO translate_config
                (subscription_id, target_language, auto_translate, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(subscription_id) DO UPDATE SET
                target_language = excluded.target_language,
                auto_translate = excluded.auto_translate,
                created_at = excluded.created_at
            """,
            (subscription_id, target_language, 1 if auto_translate else 0, _now_iso()),
        )
        conn.commit()
        return {
            "ok": True,
            "subscription_id": subscription_id,
            "target_language": target_language,
            "auto_translate": auto_translate,
        }
    finally:
        conn.close()


@router.put("/subscriptions/{subscription_id}/visibility")
def update_subscription_visibility(subscription_id: int, payload: dict = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    visible = payload.get("visible_in_panel")
    if not isinstance(visible, bool):
        raise HTTPException(status_code=400, detail="visible_in_panel must be a boolean")

    conn = _connect()
    if conn is None:
        raise HTTPException(status_code=503, detail="airss 数据库不存在")
    try:
        cur = conn.execute(
            "UPDATE subscriptions SET visible_in_panel = ? WHERE id = ?",
            (1 if visible else 0, subscription_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="subscription not found")
        return {
            "ok": True,
            "subscription_id": subscription_id,
            "visible_in_panel": visible,
        }
    finally:
        conn.close()


@router.get("/subscriptions/{subscription_id}/subscribers")
def list_subscribers(subscription_id: int):
    conn = _connect()
    if conn is None:
        return {"available": False, "subscribers": []}
    try:
        if _get_subscription(conn, subscription_id) is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        subscribers = [
            {
                "id": r["id"],
                "subscription_id": r["subscription_id"],
                "umo": r["umo"],
                "adapter_type": _adapter_type(r["umo"]),
                "personal_config": _parse_personal_config(r["personal_config"]),
            }
            for r in conn.execute(
                "SELECT id, subscription_id, umo, personal_config FROM subscribers "
                "WHERE subscription_id = ? ORDER BY id",
                (subscription_id,),
            )
        ]
        return {"available": True, "subscribers": subscribers}
    finally:
        conn.close()


@router.delete("/subscriptions/{subscription_id}/subscribers/{subscriber_id}")
def delete_subscriber(subscription_id: int, subscriber_id: int):
    """按 subscribers.id 删除接收端；删到最后一个时自动 stop=true。"""
    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        if _get_subscription(conn, subscription_id) is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        cur = conn.execute(
            "DELETE FROM subscribers WHERE id = ? AND subscription_id = ?",
            (subscriber_id, subscription_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="subscriber not found")
        conn.execute(
            "DELETE FROM article_sent WHERE subscriber_id = ?", (subscriber_id,)
        )
        remaining = conn.execute(
            "SELECT COUNT(*) FROM subscribers WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()[0]
        stopped = remaining == 0
        if stopped:
            conn.execute(
                "UPDATE subscriptions SET stop = 1 WHERE id = ?", (subscription_id,)
            )
        conn.commit()
        message = "subscriber deleted"
        if stopped:
            message += "; subscription stopped (no subscribers left)"
        return {
            "ok": True,
            "message": message,
            "stopped": stopped,
            "remaining_subscribers": remaining,
        }
    finally:
        conn.close()


@router.get("/groups")
def list_groups():
    conn = _connect()
    if conn is None:
        return {"available": False, "groups": []}
    try:
        counts = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT source_group_id, COUNT(*) FROM subscriptions GROUP BY source_group_id"
            )
        }
        groups = []
        for r in conn.execute(
            "SELECT id, name, schedules, persona_prompt FROM groups ORDER BY id"
        ):
            try:
                schedules = json.loads(r["schedules"] or "[]")
            except Exception:
                schedules = []
            groups.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "schedules": schedules if isinstance(schedules, list) else [],
                    "persona_prompt": r["persona_prompt"] or "",
                    "subscription_count": counts.get(r["id"], 0),
                }
            )
        return {"available": True, "groups": groups}
    except sqlite3.Error as exc:
        return {"available": False, "error": str(exc), "groups": []}
    finally:
        conn.close()


@router.put("/groups/{group_id}")
def update_group(group_id: int, payload: dict = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    sets, values = [], []
    if "name" in payload:
        name = payload["name"]
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(status_code=400, detail="name must not be empty")
        name = name.strip()
        if len(name) > 100:
            raise HTTPException(status_code=400, detail="name too long (max 100 chars)")
        sets.append("name = ?")
        values.append(name)
    if "schedules" in payload:
        sets.append("schedules = ?")
        values.append(json.dumps(_clean_schedules(payload["schedules"])))
    if "persona_prompt" in payload:
        prompt = payload["persona_prompt"]
        if not isinstance(prompt, str):
            raise HTTPException(status_code=400, detail="persona_prompt must be a string")
        prompt = prompt.strip()
        if len(prompt) > 8000:
            raise HTTPException(status_code=400, detail="prompt too long (max 8000 chars)")
        sets.append("persona_prompt = ?")
        values.append(prompt or None)
    if not sets:
        raise HTTPException(status_code=400, detail="no editable fields provided")

    conn = _connect()
    if conn is None:
        return {"ok": False, "available": False}
    try:
        values.append(group_id)
        cur = conn.execute(
            "UPDATE groups SET " + ", ".join(sets) + " WHERE id = ?", values
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="group not found")
        row = conn.execute(
            "SELECT id, name, schedules, persona_prompt FROM groups WHERE id = ?",
            (group_id,),
        ).fetchone()
        try:
            schedules = json.loads(row["schedules"] or "[]")
        except Exception:
            schedules = []
        return {
            "ok": True,
            "group": {
                "id": row["id"],
                "name": row["name"],
                "schedules": schedules if isinstance(schedules, list) else [],
                "persona_prompt": row["persona_prompt"] or "",
            },
        }
    finally:
        conn.close()
