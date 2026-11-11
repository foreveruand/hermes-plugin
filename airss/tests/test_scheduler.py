"""Unit tests for RSSScheduler."""

import pytest
from datetime import datetime
from plugins.airss.config import AIRSSConfig
from plugins.airss.database import Database
from plugins.airss.delivery import MessageDelivery
from plugins.airss.digest import DigestService
from plugins.airss.fetcher import FetchResult, RSSFetcher
from plugins.airss.models import RSSArticle, RSSGroup, RSSSubscription, Subscriber
from plugins.airss.scheduler import RSSScheduler, is_schedule_due, match_cron_field


def test_cron_matching():
    assert match_cron_field("*", 15, 0, 59) is True
    assert match_cron_field("*/5", 15, 0, 59) is True
    assert match_cron_field("*/5", 14, 0, 59) is False
    assert match_cron_field("10-20", 15, 0, 59) is True
    assert match_cron_field("10-20", 25, 0, 59) is False
    assert match_cron_field("1,5,15", 15, 0, 59) is True

    dt = datetime(2026, 10, 5, 9, 30)
    assert is_schedule_due("09:30", dt) is True
    assert is_schedule_due("09:31", dt) is False
    assert is_schedule_due("30 9 * * *", dt) is True
    assert is_schedule_due("31 9 * * *", dt) is False


@pytest.mark.asyncio
async def test_scheduler_fetch_and_delivery(tmp_path):
    db = Database(tmp_path / "test_sched.db")
    await db.init_db()

    config = AIRSSConfig()

    class MockFetcher(RSSFetcher):
        async def fetch_feed(self, url, **kwargs):
            return FetchResult(
                success=True,
                articles=[
                    RSSArticle(
                        title="Important News",
                        link="https://example.com/news1",
                        guid="g1",
                        content="Some news body",
                    )
                ],
            )

    fetcher = MockFetcher()
    delivered_messages = []

    async def mock_sender(umo, text):
        delivered_messages.append((umo, text))
        return True

    delivery = MessageDelivery(platform_sender=mock_sender)
    scheduler = RSSScheduler(None, db, fetcher, config, delivery=delivery)

    # Add a subscription
    sub = RSSSubscription(
        name="DailyFeed",
        url="https://example.com/rss.xml",
        interval=5,
        source_group_id=1,
    )
    sub_id = await db.add_subscription(sub)
    sub.id = sub_id

    # Add a subscriber
    subscriber = Subscriber(
        subscription_id=sub_id,
        umo="mock_platform:user_1",
        personal_config={"only_title": True},
    )
    await db.add_subscriber(subscriber)

    # Trigger fetch
    new_count = await scheduler.fetch_subscription(sub)
    assert new_count == 1
    assert len(delivered_messages) == 1
    umo, text = delivered_messages[0]
    assert umo == "mock_platform:user_1"
    assert "Important News" in text

    # Fetching again without new articles returns 0 and does not re-deliver
    new_count_2 = await scheduler.fetch_subscription(sub)
    assert new_count_2 == 0
    assert len(delivered_messages) == 1

    await db.close()


def test_gateway_process_detection(tmp_path):
    import json
    import os

    from plugins.airss import _is_gateway_process

    assert _is_gateway_process(tmp_path) is False

    lock = tmp_path / "gateway.lock"
    lock.write_text("not json", encoding="utf-8")
    assert _is_gateway_process(tmp_path) is False

    lock.write_text(json.dumps({"pid": os.getpid(), "kind": "hermes-gateway"}), encoding="utf-8")
    assert _is_gateway_process(tmp_path) is True

    lock.write_text(json.dumps({"pid": os.getppid(), "kind": "hermes-gateway"}), encoding="utf-8")
    assert _is_gateway_process(tmp_path) is True

    lock.write_text(json.dumps({"pid": 2147483647, "kind": "hermes-gateway"}), encoding="utf-8")
    assert _is_gateway_process(tmp_path) is False

    lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    assert _is_gateway_process(tmp_path) is False
