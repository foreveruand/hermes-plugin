"""Tests for the per-subscription summary switch and prompt resolution.

Semantics under test (展板「订阅源 / 总结」面板与定时总结共用同一口径):
- subscriptions.ai_summary_enabled=True  -> articles are held for the group
  digest and included in _collect_digest_targets.
- ai_summary_enabled=False -> articles are delivered individually even when
  the group has digest schedules, and never enter the group digest.
- Digest prompt resolution: groups.persona_prompt (分组覆盖)
  > board_config 'summary_prompt' (面板全局提示词) > DEFAULT_SYSTEM_PROMPT.
"""

import pytest

from plugins.airss.config import AIRSSConfig
from plugins.airss.database import Database
from plugins.airss.delivery import MessageDelivery
from plugins.airss.digest import DEFAULT_SYSTEM_PROMPT, DigestService
from plugins.airss.fetcher import RSSFetcher
from plugins.airss.models import RSSArticle, RSSGroup, RSSSubscription, Subscriber
from plugins.airss.scheduler import RSSScheduler

COMPLETE_DIGEST = (
    "# 今日综述标题在这里\n\n"
    "## 要点\n"
    "- **主体** 完成了第一件重要事项，相关数字与结论都已经确认无误。\n"
    "- **另一主体** 发布了第二项进展，影响范围覆盖多个相关行业领域。\n"
    "## 观察\n"
    "- 整体趋势保持稳定，后续仍需继续关注关键变量的边际变化情况。\n"
)


async def _build(tmp_path, *, summary_enabled: bool, schedules=("08:00",)):
    db = Database(tmp_path / "gating.db")
    await db.init_db()
    group = RSSGroup(name="新闻组", schedules=list(schedules))
    group_id = await db.add_group(group)
    sub = RSSSubscription(
        name="Feed",
        url="https://example.com/rss.xml",
        source_group_id=group_id,
        ai_summary_enabled=summary_enabled,
    )
    sub_id = await db.add_subscription(sub)
    sub.id = sub_id
    subscriber = Subscriber(subscription_id=sub_id, umo="mock_platform:user_1")
    await db.add_subscriber(subscriber)
    await db.add_article(
        RSSArticle(
            subscription_id=sub_id,
            title="重要新闻",
            link="https://example.com/a1",
            guid="g1",
            content="Some news body.",
        )
    )
    return db, group_id, sub


@pytest.mark.asyncio
async def test_digest_targets_include_enabled_subscription(tmp_path):
    db, group_id, sub = await _build(tmp_path, summary_enabled=True)
    scheduler = RSSScheduler(None, db, RSSFetcher(), AIRSSConfig())
    subs = await db.get_subscriptions_by_group(group_id)
    targets = await scheduler._collect_digest_targets(subs, recent_days=0)
    assert len(targets) == 1
    bucket = next(iter(targets.values()))
    assert [a.title for a in bucket["articles"]] == ["重要新闻"]
    await db.close()


@pytest.mark.asyncio
async def test_digest_targets_skip_disabled_subscription(tmp_path):
    db, group_id, sub = await _build(tmp_path, summary_enabled=False)
    scheduler = RSSScheduler(None, db, RSSFetcher(), AIRSSConfig())
    subs = await db.get_subscriptions_by_group(group_id)
    targets = await scheduler._collect_digest_targets(subs, recent_days=0)
    assert targets == {}
    # 定时总结入口同样无事可做
    assert await scheduler.run_group_digest(group_id) == 0
    await db.close()


@pytest.mark.asyncio
async def test_disabled_subscription_delivers_individually_in_scheduled_group(tmp_path):
    db, group_id, sub = await _build(tmp_path, summary_enabled=False)
    delivered = []

    async def mock_sender(umo, text):
        delivered.append((umo, text))
        return True

    delivery = MessageDelivery(platform_sender=mock_sender)
    scheduler = RSSScheduler(None, db, RSSFetcher(), AIRSSConfig(), delivery=delivery)
    count = await scheduler.deliver_subscription_articles(sub, new_article_ids=set())
    assert count == 1
    assert delivered and "重要新闻" in delivered[0][1]
    await db.close()


@pytest.mark.asyncio
async def test_enabled_subscription_is_held_for_digest(tmp_path):
    db, group_id, sub = await _build(tmp_path, summary_enabled=True)
    delivered = []

    async def mock_sender(umo, text):
        delivered.append((umo, text))
        return True

    delivery = MessageDelivery(platform_sender=mock_sender)
    scheduler = RSSScheduler(None, db, RSSFetcher(), AIRSSConfig(), delivery=delivery)
    count = await scheduler.deliver_subscription_articles(sub, new_article_ids=set())
    assert count == 0
    assert delivered == []
    await db.close()


@pytest.mark.asyncio
async def test_prompt_resolution_chain(tmp_path):
    db = Database(tmp_path / "prompt.db")
    await db.init_db()
    group = RSSGroup(name="组", schedules=["08:00"], persona_prompt=None)
    group_id = await db.add_group(group)
    service = DigestService(db, AIRSSConfig(), llm_caller=None)

    # 都没设置 -> 内置默认
    assert await service._get_group_system_prompt(group_id) == DEFAULT_SYSTEM_PROMPT

    # 面板全局提示词生效（且被追加纯归纳约束）
    await db.set_board_config("summary_prompt", "你是面板全局编辑")
    prompt = await service._get_group_system_prompt(group_id)
    assert prompt.startswith("你是面板全局编辑")
    assert "纯归纳" in prompt

    # 分组覆盖优先于面板全局
    group.id = group_id
    group.persona_prompt = "你是分组专属编辑"
    await db.update_group(group)
    prompt = await service._get_group_system_prompt(group_id)
    assert prompt.startswith("你是分组专属编辑")

    # 清掉面板全局后，分组覆盖仍在；再清分组覆盖 -> 回到默认
    await db.set_board_config("summary_prompt", None)
    group.persona_prompt = None
    await db.update_group(group)
    assert await service._get_group_system_prompt(group_id) == DEFAULT_SYSTEM_PROMPT
    await db.close()
