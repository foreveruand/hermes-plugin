"""Tests for image-based digest delivery."""

import pytest

from plugins.airss import scheduler as scheduler_mod
from plugins.airss.config import AIRSSConfig
from plugins.airss.database import Database
from plugins.airss.delivery import MessageDelivery
from plugins.airss.digest import DigestService
from plugins.airss.fetcher import RSSFetcher
from plugins.airss.models import RSSArticle, RSSGroup, RSSSubscription, Subscriber
from plugins.airss.scheduler import RSSScheduler


async def _build_group(tmp_path, umo):
    db = Database(tmp_path / "digest_delivery.db")
    await db.init_db()

    group = RSSGroup(name="科技", schedules=["08:00"])
    group_id = await db.add_group(group)

    sub = RSSSubscription(
        name="Feed", url="https://example.com/rss.xml", source_group_id=group_id
    )
    sub_id = await db.add_subscription(sub)

    # Subscribe first: add_subscriber backfills article_sent for existing rows,
    # so the article has to arrive afterwards to count as unsent.
    subscriber = Subscriber(subscription_id=sub_id, umo=umo)
    await db.add_subscriber(subscriber)

    await db.add_article(
        RSSArticle(
            subscription_id=sub_id,
            title="量子计算新进展",
            link="https://example.com/a1",
            guid="g1",
            content="A quantum chip was announced.",
        )
    )

    config = AIRSSConfig()

    async def mock_llm(prompt, system_prompt, max_tokens):
        return (
            "# 今日科技\n\n"
            "## 模型\n"
            "- **厂商** 发布新模型，推理速度提升 **2.3 倍**，上下文窗口扩展至 200 万 tokens。\n"
            "- 开源权重同步放出，社区在一天内完成量化与本地部署适配。\n"
            "## 硬件\n"
            "- 新一代加速卡进入送样阶段，单卡显存带宽较上一代提升约 **40%**。\n"
            "## 观察\n"
            "- 模型与硬件同步迭代，推理成本下降会继续抬高应用层的试错空间。\n"
        )

    digest_service = DigestService(db, config, llm_caller=mock_llm)
    delivery = MessageDelivery(telegram_bot_token="token")
    scheduler = RSSScheduler(None, db, RSSFetcher(), config, delivery=delivery)
    scheduler.digest_service = digest_service
    return db, scheduler, delivery, group_id


@pytest.mark.asyncio
async def test_run_group_digest_sends_images_for_telegram(tmp_path, monkeypatch):
    db, scheduler, delivery, group_id = await _build_group(tmp_path, "telegram:12345")
    captured = {}

    async def fake_render(md, out_dir, **kwargs):
        captured["markdown"] = md
        captured["group_name"] = kwargs.get("group_name")
        image = out_dir / "digest-1.png"
        image.write_bytes(b"png")
        return [image]

    async def fake_send_images(subscriber, caption, image_paths):
        captured["caption"] = caption
        captured["paths"] = list(image_paths)
        captured["dir_alive"] = image_paths[0].parent.exists()
        return True

    async def fake_send_text(subscriber, text, **kwargs):
        captured["text"] = text
        return True

    monkeypatch.setattr(scheduler_mod, "render_digest_images", fake_render)
    monkeypatch.setattr(delivery, "send_digest_images", fake_send_images)
    monkeypatch.setattr(delivery, "send_to_subscriber", fake_send_text)

    recipients = await scheduler.run_group_digest(group_id)

    assert recipients == 1
    assert "今日科技" in captured["markdown"]
    assert captured["group_name"] == "科技"
    assert "科技" in captured["caption"]
    assert captured["dir_alive"] is True
    assert "text" not in captured, "image send must not also send the text digest"
    assert not captured["paths"][0].parent.exists(), "temp render dir should be cleaned up"

    await db.close()


@pytest.mark.asyncio
async def test_run_group_digest_falls_back_to_text_when_render_fails(tmp_path, monkeypatch):
    db, scheduler, delivery, group_id = await _build_group(tmp_path, "telegram:67890")
    captured = {}

    async def fake_render(md, out_dir, **kwargs):
        return []

    async def fake_send_text(subscriber, text, **kwargs):
        captured["text"] = text
        return True

    monkeypatch.setattr(scheduler_mod, "render_digest_images", fake_render)
    monkeypatch.setattr(delivery, "send_to_subscriber", fake_send_text)

    recipients = await scheduler.run_group_digest(group_id)

    assert recipients == 1
    assert "今日科技" in captured["text"]

    await db.close()
