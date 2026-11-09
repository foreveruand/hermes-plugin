"""Unit tests for AirssToolHandlers."""

import pytest
from plugins.airss.config import AIRSSConfig
from plugins.airss.database import Database
from plugins.airss.fetcher import RSSFetcher
from plugins.airss.scheduler import RSSScheduler
from plugins.airss.tools import AirssToolHandlers, register_all_tools


@pytest.mark.asyncio
async def test_tools_crud(tmp_path):
    db = Database(tmp_path / "test_tools.db")
    await db.init_db()

    config = AIRSSConfig()
    fetcher = RSSFetcher()
    scheduler = RSSScheduler(None, db, fetcher, config)
    handlers = AirssToolHandlers(db, scheduler, fetcher, config)

    # 1. Add subscription tool
    res_add = await handlers.handle_rss_subscription(
        action="add",
        url="https://test.com/rss.xml",
        name="Test Feed",
        interval=10,
    )
    assert res_add["success"] is True
    sub_id = res_add["subscription_id"]
    assert sub_id > 0

    # 2. List subscriptions tool
    res_list = await handlers.handle_rss_subscription(action="list")
    assert res_list["success"] is True
    assert res_list["count"] == 1

    # 3. Set config tool
    res_cfg = await handlers.handle_rss_subscription(
        action="set_config",
        subscription_id=sub_id,
        key="stop",
        value="true",
    )
    assert res_cfg["success"] is True

    # 4. Group tool
    res_group = await handlers.handle_rss_group(
        action="add",
        name="News Group",
        schedules=["09:00"],
    )
    assert res_group["success"] is True
    group_id = res_group["group_id"]

    res_groups = await handlers.handle_rss_group(action="list")
    assert res_groups["success"] is True
    assert len(res_groups["groups"]) == 2  # default (1) + new

    # 5. Subscriber tool
    res_suber = await handlers.handle_rss_subscriber(
        action="add",
        subscription_id=sub_id,
        umo="telegram:999999",
        config={"only_title": True},
    )
    assert res_suber["success"] is True

    # 6. RSSHub path tool
    res_fetch = await handlers.handle_rss_fetch(rsshub_path="/bilibili/ranking")
    assert res_fetch["success"] is True
    assert "rsshub.app/bilibili/ranking" in res_fetch["rsshub_url"]

    await db.close()
