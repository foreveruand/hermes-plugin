"""Background scheduler for AIRSS feed fetching and digest delivery."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import AIRSSConfig
from .database import Database
from .delivery import MessageDelivery
from .digest import DigestService
from .fetcher import RSSFetcher
from .filter import AIFilterService
from .models import RSSArticle, RSSGroup, RSSSubscription, Subscriber
from .render import render_digest_images

logger = logging.getLogger("airss")


def match_cron_field(field_str: str, val: int, min_val: int, max_val: int) -> bool:
    """Matches a single cron field against an integer value."""
    if field_str == "*":
        return True
    for part in field_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            range_part, step_str = part.split("/", 1)
            step = int(step_str)
            if range_part == "*":
                start, end = min_val, max_val
            elif "-" in range_part:
                s, e = range_part.split("-", 1)
                start, end = int(s), int(e)
            else:
                start, end = int(range_part), max_val
            if start <= val <= end and (val - start) % step == 0:
                return True
        elif "-" in part:
            s, e = part.split("-", 1)
            if int(s) <= val <= int(e):
                return True
        elif part.isdigit():
            if int(part) == val:
                return True
    return False


def is_schedule_due(schedule: str, dt: datetime) -> bool:
    """Check if HH:MM or 5-part cron expression matches given datetime."""
    schedule = schedule.strip()
    if not schedule:
        return False

    # HH:MM format
    if re.match(r"^\d{1,2}:\d{2}$", schedule):
        h, m = schedule.split(":")
        return dt.hour == int(h) and dt.minute == int(m)

    # 5-part cron: minute hour day month day_of_week
    parts = schedule.split()
    if len(parts) == 5:
        m, h, dom, mon, dow = parts
        # Python weekday: Monday is 0, Sunday is 6. Cron typically Sunday=0 or 7.
        cron_dow = (dt.weekday() + 1) % 7
        return (
            match_cron_field(m, dt.minute, 0, 59)
            and match_cron_field(h, dt.hour, 0, 23)
            and match_cron_field(dom, dt.day, 1, 31)
            and match_cron_field(mon, dt.month, 1, 12)
            and match_cron_field(dow, cron_dow, 0, 7)
        )

    return False


_SCHEDULER_THREAD: Optional[threading.Thread] = None
_SCHEDULER_STOP_EVENT = threading.Event()
_SCHEDULER_GENERATION = 0
_SCHEDULER_LOCK = threading.Lock()


class RSSScheduler:
    """Manages periodic feed fetches and scheduled digest distributions."""

    def __init__(
        self,
        ctx: Any,
        db: Database,
        fetcher: RSSFetcher,
        config: AIRSSConfig,
        delivery: Optional[MessageDelivery] = None,
        filter_service: Optional[AIFilterService] = None,
        digest_service: Optional[DigestService] = None,
    ):
        self.ctx = ctx
        self.db = db
        self.fetcher = fetcher
        self.config = config
        self.delivery = delivery or MessageDelivery(
            default_webhook=config.delivery_config.get("default_webhook", ""),
            telegram_bot_token=config.delivery_config.get("telegram_bot_token", ""),
        )
        self.filter_service = filter_service or AIFilterService(db, config)
        self.digest_service = digest_service or DigestService(
            db, config, llm_facade=getattr(ctx, "llm", None) if ctx else None
        )

        self.running: bool = False
        self._last_digest_runs: Dict[str, str] = {}  # group_id:schedule -> YYYY-MM-DD-HH:MM

    def start(self) -> None:
        """Start the background scheduler task in a dedicated daemon thread."""
        global _SCHEDULER_THREAD, _SCHEDULER_STOP_EVENT, _SCHEDULER_GENERATION
        self.stop()
        self.running = True
        _SCHEDULER_STOP_EVENT.clear()
        with _SCHEDULER_LOCK:
            _SCHEDULER_GENERATION += 1
            gen = _SCHEDULER_GENERATION
            t = threading.Thread(
                target=self._worker_thread_main,
                args=(gen, _SCHEDULER_STOP_EVENT),
                name=f"airss-scheduler-{gen}",
                daemon=True,
            )
            t.start()
            _SCHEDULER_THREAD = t
            logger.info("AIRSS scheduler worker thread started (gen=%d)", gen)

    def _worker_thread_main(self, gen: int, stop_event: threading.Event) -> None:
        try:
            from hermes_constants import get_hermes_home, set_hermes_home_override
            from agent.secret_scope import build_profile_secret_scope, set_secret_scope
            phome = getattr(getattr(self.ctx, "_manager", None), "home_path", None)
            if not phome:
                phome = get_hermes_home()
            set_hermes_home_override(str(phome))
            set_secret_scope(build_profile_secret_scope(phome), profile_home=str(phome))
        except Exception as exc:
            logger.warning("Failed to initialize secret scope in scheduler thread: %s", exc)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_loop(gen, stop_event))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    def stop(self) -> None:
        """Stop background scheduler."""
        global _SCHEDULER_THREAD, _SCHEDULER_STOP_EVENT
        self.running = False
        _SCHEDULER_STOP_EVENT.set()
        with _SCHEDULER_LOCK:
            if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
                _SCHEDULER_THREAD.join(timeout=2.0)
            _SCHEDULER_THREAD = None

    async def _run_loop(self, gen: int, stop_event: threading.Event) -> None:
        """Periodic loop checking fetches and digest cron schedules."""
        logger.info("AIRSS scheduler background worker running (gen=%d)", gen)
        # On startup: perform an immediate backlog recovery check for unpushed articles (<24h)
        try:
            await self.recover_unpushed_backlogs()
        except Exception as exc:
            logger.error("Error during initial backlog recovery: %s", exc, exc_info=True)

        last_backlog_check = datetime.now(timezone.utc)

        while self.running and not stop_event.is_set():
            with _SCHEDULER_LOCK:
                if _SCHEDULER_GENERATION != gen:
                    logger.info("AIRSS scheduler gen=%d superseded by %d, stopping", gen, _SCHEDULER_GENERATION)
                    break
            try:
                now = datetime.now()
                now_utc = datetime.now(timezone.utc)

                # 1. Check subscriptions due for fetch
                await self._check_and_fetch_due_subscriptions(now)

                # 2. Check periodic backlog recovery (default 5 minutes)
                backlog_interval_mins = max(1, int(self.config.fetch_config.get("backlog_interval", 5) or 5))
                if (now_utc - last_backlog_check).total_seconds() >= backlog_interval_mins * 60:
                    last_backlog_check = now_utc
                    try:
                        await self.recover_unpushed_backlogs()
                    except Exception as exc:
                        logger.error(f"Error during periodic backlog recovery: {exc}", exc_info=True)

                # 3. Check group digest schedules
                await self._check_group_digests(now)

                # 3. Daily cleanup at 03:00
                if now.hour == 3 and now.minute == 0:
                    retention_days = int(
                        self.config.storage_config.get("article_retention_days", 30)
                    )
                    await self.db.cleanup_old_articles(retention_days)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Error in AIRSS scheduler loop: {exc}", exc_info=True)

            # Sleep 30 seconds before next check, checking stop_event every second
            for _ in range(30):
                if not self.running or stop_event.is_set():
                    break
                await asyncio.sleep(1)

    async def recover_unpushed_backlogs(self) -> None:
        """Scan active subscriptions and push any unpushed articles within the last 24 hours."""
        try:
            subscriptions = await self.db.get_all_subscriptions()
            for sub in subscriptions:
                if sub.stop:
                    continue
                await self.deliver_subscription_articles(sub, new_article_ids=set())
        except Exception as exc:
            logger.error("Error recovering unpushed backlogs: %s", exc, exc_info=True)

    async def _check_and_fetch_due_subscriptions(self, now: datetime) -> None:
        subscriptions = await self.db.get_all_subscriptions()
        for sub in subscriptions:
            if sub.stop:
                continue

            # Check interval
            interval_mins = max(1, sub.interval)
            is_due = False
            if sub.last_fetch is None:
                is_due = True
            else:
                elapsed = (now.replace(tzinfo=timezone.utc) - sub.last_fetch.replace(tzinfo=timezone.utc)).total_seconds()
                if elapsed >= interval_mins * 60:
                    is_due = True

            if is_due:
                try:
                    await self.fetch_subscription(sub)
                except Exception as exc:
                    logger.error(f"Error fetching subscription {sub.id} ({sub.name}): {exc}")

    async def fetch_subscription(self, sub: RSSSubscription) -> int:
        """Fetch a single subscription, store articles, and deliver unsent updates."""
        logger.info(f"Fetching RSS feed for subscription {sub.id}: {sub.name} ({sub.url})")
        res = await self.fetcher.fetch_feed(
            url=sub.url,
            etag=sub.etag,
            last_modified=sub.last_modified,
            cookies=sub.cookies,
            enable_proxy=sub.enable_proxy,
        )

        sub.last_fetch = datetime.now(timezone.utc)

        if not res.success:
            sub.error_count += 1
            max_errors = int(self.config.fetch_config.get("max_error_count", 100))
            if sub.error_count >= max_errors:
                sub.stop = True
                logger.warning(
                    f"Subscription {sub.id} ({sub.name}) exceeded max errors ({max_errors}), stopping"
                )
            await self.db.update_subscription(sub)
            return 0

        # Success - reset error count
        sub.error_count = 0
        if res.etag:
            sub.etag = res.etag
        if res.last_modified:
            sub.last_modified = res.last_modified
        await self.db.update_subscription(sub)

        # Store newly discovered articles
        new_article_ids: Set[int] = set()
        if res.articles:
            # Filter content_to_remove regex & black_keyword
            filtered_articles = []
            regex = None
            if sub.content_to_remove:
                try:
                    regex = re.compile(sub.content_to_remove)
                except Exception as e:
                    logger.warning(f"Invalid content_to_remove regex for {sub.name}: {e}")

            black_keywords = [
                k.strip() for k in (sub.black_keyword or "").split(",") if k.strip()
            ]

            for art in res.articles:
                if regex and art.content:
                    art.content = regex.sub("", art.content)

                if black_keywords:
                    matched = any(
                        (kw in (art.title or "")) or (kw in (art.content or ""))
                        for kw in black_keywords
                    )
                    if matched:
                        continue

                art.subscription_id = sub.id or 0
                filtered_articles.append(art)

            for art in filtered_articles:
                art_id = await self.db.add_article(art)
                if art_id is not None:
                    new_article_ids.add(art_id)

        # Deliver newly discovered articles + catch up on unpushed backlogs (<24h)
        await self.deliver_subscription_articles(sub, new_article_ids)
        return len(new_article_ids)

    async def deliver_subscription_articles(
        self, sub: RSSSubscription, new_article_ids: Set[int]
    ) -> int:
        """Deliver new articles and catch up on unpushed backlogs (<24h). Stale (>24h) marked sent."""
        group = await self.db.get_group(sub.source_group_id)
        is_digest_group = group and len(group.schedules) > 0

        if not sub.id:
            return 0
        # 分组总结的订阅级开关（ai_summary_enabled，展板「订阅源/总结」面板可改）：
        # 开 = 文章留给分组定时总结、不逐条推送；关 = 即使所在分组有总结
        # 排期，也照常逐条推送（这正是该字段原本的语义，此前未被执行）。
        if is_digest_group and sub.ai_summary_enabled:
            return 0

        subscribers = await self.db.get_subscribers(sub.id)
        if not subscribers:
            return 0

        now = datetime.now(timezone.utc)
        cutoff_24h = now - timedelta(hours=24)
        total_delivered = 0

        for subscriber in subscribers:
            if subscriber.id is None:
                continue
            cfg = subscriber.personal_config or {}
            if cfg.get("stop", False):
                continue

            # Retrieve all articles for this subscription not yet sent to this subscriber
            unsent_articles = await self.db.get_unsent_articles_for_subscriber(
                sub.id, subscriber.id
            )
            if not unsent_articles:
                continue

            to_send = []
            stale_ids = []

            for art in unsent_articles:
                if not art.id:
                    continue
                # 1. Newly fetched articles in this round:
                # Per requirements, new RSS items fetched after a long offline period
                # or first run must ALWAYS be delivered; 24h limit does NOT apply to them!
                if art.id in new_article_ids:
                    to_send.append(art)
                    continue

                # 2. Existing backlog from database:
                # Determine article timestamp (fetched_at or published_at)
                ts = None
                if art.fetched_at:
                    ts = art.fetched_at if art.fetched_at.tzinfo else art.fetched_at.replace(tzinfo=timezone.utc)
                elif art.published_at:
                    ts = art.published_at if art.published_at.tzinfo else art.published_at.replace(tzinfo=timezone.utc)

                if ts and ts < cutoff_24h:
                    # Stale backlog older than 24 hours: mark as sent without sending to prevent spam
                    stale_ids.append(art.id)
                else:
                    # Unsent backlog within 24 hours: eligible for catch-up push (补推)
                    to_send.append(art)

            # Mark stale backlog as sent in database
            if stale_ids:
                await self.db.mark_articles_sent_to_subscriber(subscriber.id, stale_ids)

            if not to_send:
                continue

            # Sort ascending (oldest first) so user receives them in chronological order
            to_send.sort(key=lambda a: a.id or 0)

            # Filter articles for subscriber (keywords, personal filters)
            kept, skipped_ids = await self.filter_articles_for_subscriber(
                to_send, subscriber, sub
            )
            if skipped_ids:
                await self.db.mark_articles_sent_to_subscriber(
                    subscriber.id, skipped_ids
                )

            for art in kept:
                delivered = await self.delivery.send_article(subscriber, art, sub.name, cfg)
                if delivered and art.id:
                    # Immediately mark this article as sent in the database!
                    await self.db.mark_articles_sent_to_subscriber(
                        subscriber.id, [art.id]
                    )
                    total_delivered += 1
                    # Polite throttle between messages
                    await asyncio.sleep(1.0)

        return total_delivered

    async def filter_articles_for_subscriber(
        self,
        articles: List[RSSArticle],
        subscriber: Subscriber,
        sub: RSSSubscription,
    ) -> Tuple[List[RSSArticle], List[int]]:
        """Filter articles based on personal settings, keywords, and AI topic filter."""
        cfg = subscriber.personal_config or {}
        kept: List[RSSArticle] = []
        skipped_ids: List[int] = []

        black_kws = [
            k.strip() for k in cfg.get("black_keyword", "").split(",") if k.strip()
        ]
        white_kws = [
            k.strip() for k in cfg.get("white_keyword", "").split(",") if k.strip()
        ]

        for art in articles:
            title = art.title or ""
            content = art.content or ""
            text = f"{title} {content}"

            # Black keyword
            if black_kws and any(kw in text for kw in black_kws):
                if art.id:
                    skipped_ids.append(art.id)
                continue

            # White keyword
            if white_kws and not any(kw in text for kw in white_kws):
                if art.id:
                    skipped_ids.append(art.id)
                continue

            # Only has pic
            if cfg.get("only_has_pic", False) and not art.image_urls:
                if art.id:
                    skipped_ids.append(art.id)
                continue

            kept.append(art)

        # AI duplicate topic filter
        if cfg.get("ai_filter_enabled", False) and kept:
            duplicate_ids = await self.filter_service.filter_duplicate_articles(kept)
            final_kept = []
            for art in kept:
                if art.id in duplicate_ids:
                    skipped_ids.append(art.id)
                else:
                    final_kept.append(art)
            kept = final_kept

        return kept, skipped_ids

    async def _check_group_digests(self, now: datetime) -> None:
        groups = await self.db.get_all_groups()

        tz_name = self.config.ai_config.get("ai_summary_timezone", "Asia/Shanghai") or "Asia/Shanghai"
        try:
            import zoneinfo
            target_tz = zoneinfo.ZoneInfo(tz_name)
            sched_now = datetime.now(timezone.utc).astimezone(target_tz)
        except Exception:
            sched_now = now

        time_key = sched_now.strftime("%Y-%m-%d-%H:%M")

        for group in groups:
            if not group.schedules:
                continue
            for schedule in group.schedules:
                run_key = f"{group.id}:{schedule}"
                if self._last_digest_runs.get(run_key) == time_key:
                    continue

                if is_schedule_due(schedule, sched_now) and group.id:
                    self._last_digest_runs[run_key] = time_key
                    try:
                        await self.run_group_digest(group.id)
                    except Exception as e:
                        logger.error(f"Failed to generate digest for group {group.id}: {e}")

    async def run_group_digest(self, group_id: int) -> int:
        """Collect unsent articles for all group subscribers, generate digests, and deliver."""
        group = await self.db.get_group(group_id)
        if not group:
            return 0

        subscriptions = await self.db.get_subscriptions_by_group(group_id)
        if not subscriptions:
            return 0

        # Collect targets bucketed by visible articles
        recent_days = int(self.config.ai_config.get("ai_digest_recent_days", 0) or 0)
        digest_targets = await self._collect_digest_targets(subscriptions, recent_days)
        if not digest_targets:
            return 0

        total_recipients = 0
        for signature, target in digest_targets.items():
            articles = target["articles"]
            digest_content, _ = await self.digest_service.generate_digest(
                articles=articles,
                group_id=group_id,
                article_signature=signature,
            )

            formatted_msg = self.delivery.format_digest(digest_content, group.name)
            caption = self.delivery.format_digest_caption(group.name, len(articles))
            # Rendering costs a few seconds per page, so skip it when every
            # recipient is a webhook or another text-only platform.
            wants_images = any(
                recipient["subscribers"]
                and recipient["subscribers"][0].umo.strip().startswith("telegram:")
                for recipient in target["recipients"]
            )
            image_paths = (
                await self._render_digest_images(digest_content, group.name, len(articles))
                if wants_images
                else []
            )

            try:
                for recipient in target["recipients"]:
                    subscribers = recipient["subscribers"]
                    if not subscribers:
                        continue

                    # Each recipient represents a distinct UMO (destination chat / webhook).
                    # All sub_records in subscribers share the same UMO.
                    # Send the group digest once to this recipient.
                    primary_sub = subscribers[0]
                    ok = False
                    if image_paths:
                        ok = await self.delivery.send_digest_images(
                            primary_sub, caption, image_paths
                        )
                    if not ok:
                        # Webhooks and non-Telegram targets keep the text digest,
                        # as do failed image sends.
                        ok = await self.delivery.send_to_subscriber(
                            primary_sub, formatted_msg
                        )
                    if ok:
                        # Mark articles sent to all subscriber records for this recipient
                        for sub_record in subscribers:
                            await self.db.mark_articles_sent_to_subscriber(
                                sub_record.id, target["article_ids"]
                            )
                        total_recipients += 1
            finally:
                if image_paths:
                    shutil.rmtree(image_paths[0].parent, ignore_errors=True)

        return total_recipients

    async def _render_digest_images(
        self, digest_content: str, group_name: str, article_count: int
    ) -> List[Path]:
        """Render the digest as image cards when enabled. Returns [] to mean 'send text'."""
        ai_cfg = self.config.ai_config
        if not bool(ai_cfg.get("ai_digest_image_enabled", True)):
            return []

        try:
            width = max(600, int(ai_cfg.get("ai_digest_image_width", 900) or 900))
            scale = max(1, int(ai_cfg.get("ai_digest_image_scale", 2) or 2))
        except (TypeError, ValueError):
            width, scale = 900, 2

        out_dir = Path(tempfile.mkdtemp(prefix="airss-digest-"))
        try:
            paths = await render_digest_images(
                digest_content,
                out_dir,
                group_name=group_name,
                subtitle=f"{self._digest_date()} · {article_count} 篇资讯",
                width=width,
                scale=scale,
            )
        except Exception as exc:
            logger.warning("Digest image rendering failed: %s", exc)
            paths = []

        if not paths:
            shutil.rmtree(out_dir, ignore_errors=True)
        return paths

    def _digest_date(self) -> str:
        tz_name = self.config.ai_config.get("ai_summary_timezone", "Asia/Shanghai") or "Asia/Shanghai"
        try:
            import zoneinfo

            return datetime.now(timezone.utc).astimezone(zoneinfo.ZoneInfo(tz_name)).strftime("%Y-%m-%d")
        except Exception:
            return datetime.now().strftime("%Y-%m-%d")

    async def _collect_digest_targets(
        self, subscriptions: List[RSSSubscription], recent_days: int
    ) -> Dict[str, dict]:
        sub_by_id = {s.id: s for s in subscriptions if s.id is not None}
        umo_to_subscribers: Dict[str, List[Subscriber]] = {}

        for sub in subscriptions:
            # 只汇总启用了总结的订阅；未启用的订阅走逐条推送路径，
            # 它们的文章不进分组总结（ai_summary_enabled 的既定语义）。
            if sub.id is None or not sub.ai_summary_enabled:
                continue
            for subscriber in await self.db.get_subscribers(sub.id):
                umo_to_subscribers.setdefault(subscriber.umo, []).append(subscriber)

        digest_targets: Dict[str, dict] = {}
        for umo, sub_list in umo_to_subscribers.items():
            active_subs = [
                s for s in sub_list if not (s.personal_config or {}).get("stop", False)
            ]
            if not active_subs:
                continue

            visible_articles: Dict[int, RSSArticle] = {}
            for subscriber in active_subs:
                if subscriber.id is None:
                    continue
                sub = sub_by_id.get(subscriber.subscription_id)
                if not sub:
                    continue
                unsent = await self.db.get_unsent_articles_for_subscriber(
                    subscriber.subscription_id, subscriber.id, recent_days=recent_days
                )
                kept, skipped_ids = await self.filter_articles_for_subscriber(
                    unsent, subscriber, sub
                )
                if skipped_ids:
                    await self.db.mark_articles_sent_to_subscriber(
                        subscriber.id, skipped_ids
                    )
                for art in kept:
                    if art.id:
                        visible_articles[art.id] = art

            if not visible_articles:
                continue

            article_ids = sorted(visible_articles)
            signature = ",".join(str(i) for i in article_ids)
            bucket = digest_targets.setdefault(
                signature,
                {
                    "article_ids": article_ids,
                    "articles": sorted(
                        visible_articles.values(),
                        key=lambda a: (a.published_at or a.fetched_at, a.id or 0),
                        reverse=True,
                    ),
                    "recipients": [],
                },
            )
            bucket["recipients"].append({"umo": umo, "subscribers": active_subs})

        return digest_targets
