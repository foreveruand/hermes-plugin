"""Database operations for the RSS plugin using async SQLite."""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

import aiosqlite

from .models import (
    GLOBAL_CONFIGURABLE_FIELDS,
    RSSArticle,
    RSSGroup,
    RSSSubscription,
    Subscriber,
)

logger = logging.getLogger("airss")


class Database:
    """Async SQLite database for RSS plugin."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conns: dict[int, aiosqlite.Connection] = {}
        self._initialized = False

    async def _get_conn(self) -> aiosqlite.Connection:
        import asyncio
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        conn = self._conns.get(loop_id)
        if conn is None:
            conn = await aiosqlite.connect(self.db_path, timeout=60.0, isolation_level=None)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA busy_timeout=60000;")
            self._conns[loop_id] = conn
        return conn

    async def init_db(self) -> None:
        """Initialize database tables and schema."""
        if self._initialized:
            return
        conn = await self._get_conn()
        # Board-owned tables (展板专用)：board_state 存已读/收藏/反馈，
        # board_config 存展板面板设置（如全局总结提示词）。两个进程
        # （airss 本体与 dashboard 展板）都会懒建，互不依赖对方先跑。
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS board_state (
                article_id INTEGER PRIMARY KEY,
                read_at TEXT,
                feedback INTEGER NOT NULL DEFAULT 0,
                starred INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS board_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await conn.commit()
        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='subscriptions'")
        if await cursor.fetchone():
            # 老库迁移：board_state 早期版本没有 starred 列
            cursor = await conn.execute(
                "SELECT name FROM pragma_table_info('board_state') WHERE name = 'starred'"
            )
            if not await cursor.fetchone():
                await conn.execute(
                    "ALTER TABLE board_state ADD COLUMN starred INTEGER NOT NULL DEFAULT 0"
                )
                await conn.commit()
            self._initialized = True
            return
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                interval INTEGER DEFAULT 5,
                source_group_id INTEGER DEFAULT 1,
                cookies TEXT DEFAULT NULL,
                black_keyword TEXT DEFAULT NULL,
                content_to_remove TEXT DEFAULT NULL,
                max_image_number INTEGER DEFAULT 0,
                ai_summary_enabled INTEGER DEFAULT 0,
                enable_proxy INTEGER DEFAULT 0,
                stop INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                last_fetch TEXT DEFAULT NULL,
                etag TEXT DEFAULT NULL,
                last_modified TEXT DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS articles (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                title TEXT,
                content TEXT,
                link TEXT,
                guid TEXT,
                author TEXT DEFAULT '',
                published_at TEXT,
                fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
                is_sent INTEGER DEFAULT 0,
                image_urls TEXT,
                content_hash TEXT,
                ai_filter_result INTEGER DEFAULT NULL,
                ai_filter_checked_at TEXT DEFAULT NULL,
                UNIQUE(subscription_id, content_hash)
            );

            CREATE TABLE IF NOT EXISTS groups (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                schedules TEXT DEFAULT '[]',
                persona_id TEXT DEFAULT NULL,
                persona_prompt TEXT DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS subscribers (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                umo TEXT NOT NULL,
                personal_config TEXT DEFAULT '{}',
                UNIQUE(subscription_id, umo)
            );

            CREATE TABLE IF NOT EXISTS article_sent (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL,
                subscriber_id INTEGER NOT NULL,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(article_id, subscriber_id)
            );

            CREATE INDEX IF NOT EXISTS idx_articles_subscription ON articles(subscription_id);
            CREATE INDEX IF NOT EXISTS idx_articles_sent ON articles(is_sent);
            CREATE INDEX IF NOT EXISTS idx_subscribers_subscription ON subscribers(subscription_id);
            CREATE INDEX IF NOT EXISTS idx_article_sent_article ON article_sent(article_id);
            CREATE INDEX IF NOT EXISTS idx_article_sent_subscriber ON article_sent(subscriber_id);
            """
        )
        await conn.commit()

        # Run migrations on this conn
        cursor = await conn.execute(
            "SELECT name FROM pragma_table_info('groups') WHERE name = 'persona_prompt'"
        )
        if not await cursor.fetchone():
            await conn.execute("ALTER TABLE groups ADD COLUMN persona_prompt TEXT DEFAULT NULL")
            await conn.commit()

        cursor = await conn.execute(
            "SELECT name FROM pragma_table_info('articles') WHERE name = 'author'"
        )
        if not await cursor.fetchone():
            await conn.execute("ALTER TABLE articles ADD COLUMN author TEXT DEFAULT ''")
            await conn.commit()

        cursor = await conn.execute(
            "SELECT name FROM pragma_table_info('subscriptions') WHERE name = 'stop'"
        )
        if not await cursor.fetchone():
            await conn.execute("ALTER TABLE subscriptions ADD COLUMN stop INTEGER DEFAULT 0")
            await conn.commit()

        cursor = await conn.execute(
            "SELECT name FROM pragma_table_info('subscriptions') WHERE name = 'error_count'"
        )
        if not await cursor.fetchone():
            await conn.execute("ALTER TABLE subscriptions ADD COLUMN error_count INTEGER DEFAULT 0")
            await conn.commit()

        for col, col_def in [
            ("ai_filter_result", "INTEGER DEFAULT NULL"),
            ("ai_filter_checked_at", "TEXT DEFAULT NULL"),
        ]:
            cursor = await conn.execute(
                f"SELECT name FROM pragma_table_info('articles') WHERE name = '{col}'"
            )
            if not await cursor.fetchone():
                await conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {col_def}")
                await conn.commit()

        cursor = await conn.execute("SELECT id FROM groups WHERE id = 1")
        if not await cursor.fetchone():
            await conn.execute(
                "INSERT INTO groups (id, name, schedules, persona_id, persona_prompt) VALUES (1, '默认分组', '[]', NULL, NULL)"
            )
            await conn.commit()

        self._initialized = True
        logger.info(f"AIRSS database initialized at {self.db_path}")

    async def close(self) -> None:
        """Close connection."""
        for loop_id, conn in list(self._conns.items()):
            try:
                await conn.close()
            except Exception:
                pass
        self._conns.clear()
        self._initialized = False

    @asynccontextmanager
    async def _acquire(self):
        """Yield active connection."""
        conn = await self._get_conn()
        if not self._initialized:
            await self.init_db()
        yield conn

    @staticmethod
    def _hash_content(guid: str, link: str) -> str:
        return hashlib.md5(f"{guid}:{link}".encode()).hexdigest()

    @staticmethod
    def _retention_cutoff(retention_days: int) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=retention_days)

    @staticmethod
    def _normalize_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    # ==================== Subscription Operations ====================

    async def add_subscription(self, sub: RSSSubscription) -> int:
        async with self._acquire() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO subscriptions (
                    name, url, interval, source_group_id, cookies, black_keyword,
                    content_to_remove, max_image_number, ai_summary_enabled, enable_proxy, stop
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sub.name,
                    sub.url,
                    sub.interval,
                    sub.source_group_id,
                    sub.cookies,
                    sub.black_keyword,
                    sub.content_to_remove,
                    sub.max_image_number,
                    1 if sub.ai_summary_enabled else 0,
                    1 if sub.enable_proxy else 0,
                    1 if sub.stop else 0,
                ),
            )
            await conn.commit()
            return cursor.lastrowid or 0

    def _row_to_sub(self, row: Any) -> RSSSubscription:
        return RSSSubscription(
            id=row["id"],
            name=row["name"],
            url=row["url"],
            interval=row["interval"],
            source_group_id=row["source_group_id"],
            cookies=row["cookies"],
            black_keyword=row["black_keyword"],
            content_to_remove=row["content_to_remove"],
            max_image_number=row["max_image_number"] or 0,
            ai_summary_enabled=bool(row["ai_summary_enabled"]),
            enable_proxy=bool(row["enable_proxy"]),
            stop=bool(row["stop"]),
            error_count=row["error_count"] or 0,
            last_fetch=datetime.fromisoformat(row["last_fetch"])
            if row["last_fetch"]
            else None,
            etag=row["etag"],
            last_modified=row["last_modified"],
        )

    async def get_subscription(self, sub_id: int) -> Optional[RSSSubscription]:
        async with self._acquire() as conn:
            cursor = await conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
            row = await cursor.fetchone()
            return self._row_to_sub(row) if row else None

    async def get_subscription_by_url(self, url: str) -> Optional[RSSSubscription]:
        async with self._acquire() as conn:
            cursor = await conn.execute("SELECT * FROM subscriptions WHERE url = ?", (url,))
            row = await cursor.fetchone()
            return self._row_to_sub(row) if row else None

    async def get_all_subscriptions(self) -> List[RSSSubscription]:
        async with self._acquire() as conn:
            cursor = await conn.execute("SELECT * FROM subscriptions ORDER BY id ASC")
            rows = await cursor.fetchall()
            return [self._row_to_sub(row) for row in rows]

    async def update_subscription(self, sub: RSSSubscription) -> None:
        async with self._acquire() as conn:
            await conn.execute(
                """
                UPDATE subscriptions SET
                    name = ?, url = ?, interval = ?, source_group_id = ?,
                    cookies = ?, black_keyword = ?, content_to_remove = ?,
                    max_image_number = ?, ai_summary_enabled = ?, enable_proxy = ?, stop = ?,
                    error_count = ?, last_fetch = ?, etag = ?, last_modified = ?
                WHERE id = ?
                """,
                (
                    sub.name,
                    sub.url,
                    sub.interval,
                    sub.source_group_id,
                    sub.cookies,
                    sub.black_keyword,
                    sub.content_to_remove,
                    sub.max_image_number,
                    1 if sub.ai_summary_enabled else 0,
                    1 if sub.enable_proxy else 0,
                    1 if sub.stop else 0,
                    sub.error_count,
                    sub.last_fetch.isoformat() if sub.last_fetch else None,
                    sub.etag,
                    sub.last_modified,
                    sub.id,
                ),
            )
            await conn.commit()

    async def delete_subscription(self, sub_id: int) -> None:
        async with self._acquire() as conn:
            await conn.execute("DELETE FROM subscribers WHERE subscription_id = ?", (sub_id,))
            await conn.execute("DELETE FROM articles WHERE subscription_id = ?", (sub_id,))
            await conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
            await conn.commit()

    async def update_subscription_global_config(
        self, subscription_id: int, key: str, value: Any
    ) -> None:
        if key not in GLOBAL_CONFIGURABLE_FIELDS:
            raise ValueError(f"Field '{key}' is not configurable")
        async with self._acquire() as conn:
            if isinstance(value, bool):
                value = 1 if value else 0
            await conn.execute(
                f"UPDATE subscriptions SET {key} = ? WHERE id = ?",
                (value, subscription_id),
            )
            await conn.commit()

    # ==================== Article Operations ====================

    def _row_to_article(self, row: Any) -> RSSArticle:
        return RSSArticle(
            id=row["id"],
            subscription_id=row["subscription_id"],
            title=row["title"] or "",
            content=row["content"] or "",
            link=row["link"] or "",
            guid=row["guid"] or "",
            author=row["author"] or "",
            published_at=datetime.fromisoformat(row["published_at"])
            if row["published_at"]
            else None,
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            is_sent=bool(row["is_sent"]),
            image_urls=row["image_urls"].split("|||") if row["image_urls"] else [],
            ai_filter_result=bool(row["ai_filter_result"])
            if row["ai_filter_result"] is not None
            else None,
            ai_filter_checked_at=datetime.fromisoformat(row["ai_filter_checked_at"])
            if row["ai_filter_checked_at"]
            else None,
        )

    async def add_article(self, article: RSSArticle) -> Optional[int]:
        content_hash = self._hash_content(article.guid, article.link)
        async with self._acquire() as conn:
            try:
                cursor = await conn.execute(
                    """
                    INSERT INTO articles (
                        subscription_id, title, content, link, guid, author,
                        published_at, fetched_at, is_sent, image_urls, content_hash
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        article.subscription_id,
                        article.title,
                        article.content,
                        article.link,
                        article.guid,
                        article.author,
                        article.published_at.isoformat() if article.published_at else None,
                        article.fetched_at.isoformat(),
                        1 if article.is_sent else 0,
                        "|||".join(article.image_urls),
                        content_hash,
                    ),
                )
                await conn.commit()
                return cursor.lastrowid
            except aiosqlite.IntegrityError:
                return None

    async def article_exists(self, subscription_id: int, guid: str, link: str) -> bool:
        content_hash = self._hash_content(guid, link)
        async with self._acquire() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM articles WHERE subscription_id = ? AND content_hash = ?",
                (subscription_id, content_hash),
            )
            return await cursor.fetchone() is not None

    async def get_unsent_articles(self, subscription_id: int) -> List[RSSArticle]:
        async with self._acquire() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM articles
                WHERE subscription_id = ? AND is_sent = 0
                ORDER BY published_at DESC, fetched_at DESC
                """,
                (subscription_id,),
            )
            rows = await cursor.fetchall()
            return [self._row_to_article(row) for row in rows]

    async def mark_articles_sent(self, article_ids: List[int]) -> None:
        if not article_ids:
            return
        async with self._acquire() as conn:
            placeholders = ",".join("?" * len(article_ids))
            await conn.execute(
                f"UPDATE articles SET is_sent = 1 WHERE id IN ({placeholders})",
                article_ids,
            )
            await conn.commit()

    async def get_recent_articles(
        self, subscription_id: Optional[int] = None, limit: int = 20
    ) -> List[RSSArticle]:
        async with self._acquire() as conn:
            if subscription_id:
                cursor = await conn.execute(
                    "SELECT * FROM articles WHERE subscription_id = ? ORDER BY fetched_at DESC LIMIT ?",
                    (subscription_id, limit),
                )
            else:
                cursor = await conn.execute(
                    "SELECT * FROM articles ORDER BY fetched_at DESC LIMIT ?",
                    (limit,),
                )
            rows = await cursor.fetchall()
            return [self._row_to_article(row) for row in rows]

    # ==================== Subscriber Operations ====================

    async def add_subscriber(self, subscriber: Subscriber) -> Optional[int]:
        async with self._acquire() as conn:
            try:
                cursor = await conn.execute(
                    "INSERT INTO subscribers (subscription_id, umo, personal_config) VALUES (?, ?, ?)",
                    (
                        subscriber.subscription_id,
                        subscriber.umo,
                        json.dumps(subscriber.personal_config),
                    ),
                )
                await conn.commit()
                subscriber_id = cursor.lastrowid
                if subscriber_id:
                    # Mark all existing articles as sent to new subscriber
                    await conn.execute(
                        """
                        INSERT OR IGNORE INTO article_sent (article_id, subscriber_id)
                        SELECT id, ? FROM articles WHERE subscription_id = ?
                        """,
                        (subscriber_id, subscriber.subscription_id),
                    )
                    await conn.commit()
                return subscriber_id
            except aiosqlite.IntegrityError:
                return None

    async def get_subscribers(self, subscription_id: int) -> List[Subscriber]:
        async with self._acquire() as conn:
            cursor = await conn.execute(
                "SELECT * FROM subscribers WHERE subscription_id = ?",
                (subscription_id,),
            )
            rows = await cursor.fetchall()
            return [
                Subscriber(
                    id=row["id"],
                    subscription_id=row["subscription_id"],
                    umo=row["umo"],
                    personal_config=json.loads(row["personal_config"] or "{}"),
                )
                for row in rows
            ]

    async def get_subscriber(self, subscription_id: int, umo: str) -> Optional[Subscriber]:
        async with self._acquire() as conn:
            cursor = await conn.execute(
                "SELECT * FROM subscribers WHERE subscription_id = ? AND umo = ?",
                (subscription_id, umo),
            )
            row = await cursor.fetchone()
            if row:
                return Subscriber(
                    id=row["id"],
                    subscription_id=row["subscription_id"],
                    umo=row["umo"],
                    personal_config=json.loads(row["personal_config"] or "{}"),
                )
            return None

    async def update_subscriber(self, subscriber: Subscriber) -> None:
        async with self._acquire() as conn:
            cursor = await conn.execute(
                "SELECT personal_config FROM subscribers WHERE id = ?",
                (subscriber.id,),
            )
            row = await cursor.fetchone()
            old_config = json.loads(row["personal_config"] or "{}") if row else {}
            new_config = subscriber.personal_config or {}

            await conn.execute(
                "UPDATE subscribers SET personal_config = ? WHERE id = ?",
                (json.dumps(subscriber.personal_config), subscriber.id),
            )

            # If unpausing (stop: True -> False), mark backlog as sent to prevent barrage
            if subscriber.id and old_config.get("stop", False) and not new_config.get("stop", False):
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO article_sent (article_id, subscriber_id)
                    SELECT id, ? FROM articles WHERE subscription_id = ?
                    """,
                    (subscriber.id, subscriber.subscription_id),
                )
            await conn.commit()

    async def delete_subscriber(self, subscription_id: int, umo: str) -> None:
        async with self._acquire() as conn:
            await conn.execute(
                "DELETE FROM subscribers WHERE subscription_id = ? AND umo = ?",
                (subscription_id, umo),
            )
            await conn.commit()

    async def get_unsent_articles_for_subscriber(
        self, subscription_id: int, subscriber_id: int, recent_days: int = 0
    ) -> List[RSSArticle]:
        async with self._acquire() as conn:
            cursor = await conn.execute(
                """
                SELECT a.* FROM articles a
                WHERE a.subscription_id = ?
                AND a.id NOT IN (
                    SELECT article_id FROM article_sent WHERE subscriber_id = ?
                )
                ORDER BY a.published_at DESC, a.fetched_at DESC
                """,
                (subscription_id, subscriber_id),
            )
            rows = await cursor.fetchall()
            articles = [self._row_to_article(r) for r in rows]

            if recent_days > 0:
                cutoff = self._retention_cutoff(recent_days)
                articles = [
                    a for a in articles
                    if self._normalize_datetime(a.published_at or a.fetched_at) >= cutoff
                ]
            return articles

    async def mark_articles_sent_to_subscriber(
        self, subscriber_id: int, article_ids: List[int]
    ) -> None:
        if not article_ids:
            return
        async with self._acquire() as conn:
            await conn.executemany(
                "INSERT OR IGNORE INTO article_sent (article_id, subscriber_id) VALUES (?, ?)",
                [(aid, subscriber_id) for aid in article_ids],
            )
            await conn.commit()

    # ==================== Group Operations ====================

    async def add_group(self, group: RSSGroup) -> int:
        async with self._acquire() as conn:
            cursor = await conn.execute(
                "INSERT INTO groups (name, schedules, persona_id, persona_prompt) VALUES (?, ?, ?, ?)",
                (
                    group.name,
                    json.dumps(group.schedules),
                    group.persona_id,
                    group.persona_prompt,
                ),
            )
            await conn.commit()
            return cursor.lastrowid or 0

    async def get_group(self, group_id: int) -> Optional[RSSGroup]:
        async with self._acquire() as conn:
            cursor = await conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,))
            row = await cursor.fetchone()
            if row:
                return RSSGroup(
                    id=row["id"],
                    name=row["name"],
                    schedules=json.loads(row["schedules"] or "[]"),
                    persona_id=row["persona_id"],
                    persona_prompt=row["persona_prompt"],
                )
            return None

    async def get_all_groups(self) -> List[RSSGroup]:
        async with self._acquire() as conn:
            cursor = await conn.execute("SELECT * FROM groups ORDER BY id ASC")
            rows = await cursor.fetchall()
            return [
                RSSGroup(
                    id=row["id"],
                    name=row["name"],
                    schedules=json.loads(row["schedules"] or "[]"),
                    persona_id=row["persona_id"],
                    persona_prompt=row["persona_prompt"],
                )
                for row in rows
            ]

    async def update_group(self, group: RSSGroup) -> None:
        async with self._acquire() as conn:
            await conn.execute(
                "UPDATE groups SET name = ?, schedules = ?, persona_id = ?, persona_prompt = ? WHERE id = ?",
                (
                    group.name,
                    json.dumps(group.schedules),
                    group.persona_id,
                    group.persona_prompt,
                    group.id,
                ),
            )
            await conn.commit()

    async def delete_group(self, group_id: int) -> None:
        async with self._acquire() as conn:
            await conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
            await conn.commit()

    async def get_subscriptions_by_group(self, group_id: int) -> List[RSSSubscription]:
        async with self._acquire() as conn:
            cursor = await conn.execute(
                "SELECT * FROM subscriptions WHERE source_group_id = ?",
                (group_id,),
            )
            rows = await cursor.fetchall()
            return [self._row_to_sub(row) for row in rows]

    # ==================== Board Config Operations ====================
    # 展板面板设置（key/value）。总结提示词的全局值存 key='summary_prompt'，
    # 由 dashboard 展板写入、DigestService 读取；分组级覆盖仍用 groups.persona_prompt。

    async def get_board_config(self, key: str) -> Optional[str]:
        async with self._acquire() as conn:
            try:
                cursor = await conn.execute(
                    "SELECT value FROM board_config WHERE key = ?", (key,)
                )
                row = await cursor.fetchone()
            except Exception:
                return None
            return row["value"] if row else None

    async def set_board_config(self, key: str, value: Optional[str]) -> None:
        async with self._acquire() as conn:
            if value is None:
                await conn.execute("DELETE FROM board_config WHERE key = ?", (key,))
            else:
                await conn.execute(
                    "INSERT INTO board_config (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            await conn.commit()

    # ==================== AI Filter Cache Operations ====================

    async def get_article_ai_filter_results(
        self, article_ids: List[int]
    ) -> dict[int, bool]:
        if not article_ids:
            return {}
        async with self._acquire() as conn:
            placeholders = ",".join("?" * len(article_ids))
            cursor = await conn.execute(
                f"""
                SELECT id, ai_filter_result FROM articles
                WHERE id IN ({placeholders}) AND ai_filter_result IS NOT NULL
                """,
                article_ids,
            )
            rows = await cursor.fetchall()
            return {int(row["id"]): bool(row["ai_filter_result"]) for row in rows}

    async def set_article_ai_filter_results(self, results: dict[int, bool]) -> None:
        if not results:
            return
        checked_at = datetime.now(timezone.utc).isoformat()
        async with self._acquire() as conn:
            await conn.executemany(
                "UPDATE articles SET ai_filter_result = ?, ai_filter_checked_at = ? WHERE id = ?",
                [
                    (1 if is_dup else 0, checked_at, aid)
                    for aid, is_dup in results.items()
                ],
            )
            await conn.commit()

    async def get_recent_ai_filter_candidates(
        self, recent_minutes: int, exclude_article_ids: List[int], limit: int = 50
    ) -> List[Tuple[int, str]]:
        if recent_minutes <= 0 or limit <= 0:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=recent_minutes)
        async with self._acquire() as conn:
            params: List[Any] = [cutoff.isoformat()]
            exclude_clause = ""
            if exclude_article_ids:
                placeholders = ",".join("?" * len(exclude_article_ids))
                exclude_clause = f"AND id NOT IN ({placeholders})"
                params.extend(exclude_article_ids)
            params.append(limit)

            cursor = await conn.execute(
                f"""
                SELECT id, title FROM articles
                WHERE fetched_at >= ?
                AND title IS NOT NULL AND title != ''
                AND (ai_filter_result IS NULL OR ai_filter_result = 0)
                {exclude_clause}
                ORDER BY fetched_at DESC
                LIMIT ?
                """,
                params,
            )
            rows = await cursor.fetchall()
            return [(int(r["id"]), str(r["title"])) for r in rows]

    # ==================== Cleanup Operations ====================

    async def cleanup_old_articles(self, retention_days: int = 30) -> int:
        cutoff = self._retention_cutoff(retention_days).isoformat()
        async with self._acquire() as conn:
            await conn.execute(
                """
                DELETE FROM article_sent
                WHERE article_id IN (
                    SELECT id FROM articles
                    WHERE COALESCE(published_at, fetched_at) < ?
                )
                """,
                (cutoff,),
            )
            cursor = await conn.execute(
                "DELETE FROM articles WHERE COALESCE(published_at, fetched_at) < ?",
                (cutoff,),
            )
            await conn.commit()
            return cursor.rowcount
