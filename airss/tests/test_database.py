"""Unit tests for Database."""

import pytest
from datetime import datetime, timezone, timedelta
from plugins.airss.database import Database
from plugins.airss.models import RSSSubscription, RSSArticle, RSSGroup, Subscriber


@pytest.mark.asyncio
async def test_database_init_and_crud(tmp_path):
    db_file = tmp_path / "test_rss.db"
    db = Database(db_file)
    await db.init_db()

    # 1. Default group exists
    group = await db.get_group(1)
    assert group is not None
    assert group.name == "默认分组"

    # 2. Add Subscription
    sub = RSSSubscription(
        name="TechNews",
        url="https://example.com/feed.xml",
        interval=10,
        source_group_id=1,
    )
    sub_id = await db.add_subscription(sub)
    assert sub_id > 0

    fetched_sub = await db.get_subscription(sub_id)
    assert fetched_sub is not None
    assert fetched_sub.name == "TechNews"
    assert fetched_sub.interval == 10

    # 3. Add Article
    art = RSSArticle(
        subscription_id=sub_id,
        title="AI Breakthrough",
        content="New model announced today.",
        link="https://example.com/art1",
        guid="art1",
        published_at=datetime.now(timezone.utc),
    )
    art_id = await db.add_article(art)
    assert art_id is not None
    assert art_id > 0

    # Duplicate article returns None
    dup_id = await db.add_article(art)
    assert dup_id is None

    # Check unsent articles
    unsent = await db.get_unsent_articles(sub_id)
    assert len(unsent) == 1
    assert unsent[0].title == "AI Breakthrough"

    # 4. Subscriber operations
    subscriber = Subscriber(
        subscription_id=sub_id,
        umo="telegram:123456",
        personal_config={"only_title": True},
    )
    sub_rec_id = await db.add_subscriber(subscriber)
    assert sub_rec_id is not None

    # New subscriber has existing articles marked as sent
    sub_unsent = await db.get_unsent_articles_for_subscriber(sub_id, sub_rec_id)
    assert len(sub_unsent) == 0

    # Add a new article after subscriber joined
    art2 = RSSArticle(
        subscription_id=sub_id,
        title="Second Article",
        content="Details...",
        link="https://example.com/art2",
        guid="art2",
    )
    art2_id = await db.add_article(art2)
    assert art2_id is not None

    sub_unsent2 = await db.get_unsent_articles_for_subscriber(sub_id, sub_rec_id)
    assert len(sub_unsent2) == 1
    assert sub_unsent2[0].id == art2_id

    # Mark sent to subscriber
    await db.mark_articles_sent_to_subscriber(sub_rec_id, [art2_id])
    sub_unsent3 = await db.get_unsent_articles_for_subscriber(sub_id, sub_rec_id)
    assert len(sub_unsent3) == 0

    # 5. Cleanup old articles
    deleted = await db.cleanup_old_articles(retention_days=10)
    assert deleted == 0

    await db.close()
