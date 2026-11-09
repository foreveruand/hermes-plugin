"""Unit tests for MessageDelivery."""

import re

import pytest
from plugins.airss.delivery import (
    MAX_TG_CAPTION,
    MAX_TG_TEXT,
    TG_ALLOWED_TAGS,
    MessageDelivery,
    _tg_plain_text,
    _truncate_html,
    clean_html_text,
    markdown_to_telegram_html,
)
from plugins.airss.models import RSSArticle, Subscriber


def _tags(html_text):
    return {m.lower() for m in re.findall(r"</?([a-zA-Z][a-zA-Z0-9-]*)", html_text)}


def _is_balanced(html_text):
    stack = []
    for m in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)", html_text):
        if m.group(1):
            if not stack or stack[-1] != m.group(2):
                return False
            stack.pop()
        else:
            stack.append(m.group(2))
    return not stack


def test_clean_html_text():
    raw = "<p>Hello <b>World</b>!</p><br/><div>Line 2</div>"
    cleaned = clean_html_text(raw)
    assert "Hello World!" in cleaned
    assert "Line 2" in cleaned
    assert "<p>" not in cleaned


def test_format_article_variations():
    delivery = MessageDelivery()
    art = RSSArticle(
        id=1,
        subscription_id=1,
        title="Test News",
        content="<p>Important details here.</p>",
        link="https://news.test/1",
        author="Editor",
        image_urls=["https://img.test/pic.png"],
    )

    # Default format
    text = delivery.format_article(art, "TechFeed")
    assert "📰 **Test News**" in text
    assert "Important details here." in text
    assert "[Editor](https://news.test/1)" in text

    # Only title
    text_title = delivery.format_article(art, "TechFeed", {"only_title": True})
    assert "📰 **Test News**" in text_title
    assert "Important details" not in text_title

    # Only pic
    text_pic = delivery.format_article(art, "TechFeed", {"only_pic": True})
    assert "![图片](https://img.test/pic.png)" in text_pic

    # Spoiler
    text_sp = delivery.format_article(art, "TechFeed", {"enable_spoiler": True})
    assert "||Important details here.||" in text_sp


@pytest.mark.asyncio
async def test_custom_platform_sender():
    delivered = []

    async def mock_sender(umo: str, text: str):
        delivered.append((umo, text))
        return True

    delivery = MessageDelivery(platform_sender=mock_sender)
    sub = Subscriber(subscription_id=1, umo="custom:channel_123")
    ok = await delivery.send_to_subscriber(sub, "Hello Channel")
    assert ok is True
    assert len(delivered) == 1
    assert delivered[0] == ("custom:channel_123", "Hello Channel")


def test_markdown_bold_and_link_render():
    out = markdown_to_telegram_html(
        "📰 **Test News**\n\n🔗 来源: [Editor](https://news.test/1)"
    )
    assert "<b>Test News</b>" in out
    assert '<a href="https://news.test/1">Editor</a>' in out
    # The literal markdown the user reported seeing must be gone.
    assert "**" not in out


def test_markdown_escapes_html_specials_but_keeps_snake_case():
    out = markdown_to_telegram_html("A & B < C > D and some_var_name here")
    assert "A &amp; B &lt; C &gt; D" in out
    assert "some_var_name" in out  # not mangled into <i> tags
    assert _tags(out) <= TG_ALLOWED_TAGS


def test_markdown_blocks_map_to_allowed_tags():
    out = markdown_to_telegram_html("## 简报\n- 第一条\n> 引用\n---")
    assert "<b>简报</b>" in out
    assert "• 第一条" in out
    assert "<blockquote>引用</blockquote>" in out
    # Telegram rejects these outright.
    for forbidden in ("<h1", "<ul", "<li", "<hr", "<br"):
        assert forbidden not in out


def test_markdown_rejects_script_urls():
    out = markdown_to_telegram_html("[click](javascript:alert(1)) tail")
    assert "<a" not in out
    assert "javascript:" not in out


def test_markdown_inline_spans():
    out = markdown_to_telegram_html("~~del~~ ||spoil|| *ital* `code`")
    assert "<s>del</s>" in out
    assert "<tg-spoiler>spoil</tg-spoiler>" in out
    assert "<i>ital</i>" in out
    assert "<code>code</code>" in out


def test_markdown_degrades_malformed_input():
    for src in ("未闭合 <b>x <h1>y</h1>", "```unclosed fence", "a [unclosed paren"):
        out = markdown_to_telegram_html(src)
        assert _tags(out) <= TG_ALLOWED_TAGS
        assert _is_balanced(out)
        assert "\x00" not in out


def test_truncate_html_respects_limit_without_splitting_tags():
    src = "📰 **标题标题标题**\n\n" + ("正文 with **bold** and [l](https://a.test/x) " * 60)
    for limit in (200, MAX_TG_CAPTION, MAX_TG_TEXT):
        out = _truncate_html(src, limit)
        assert len(out) <= limit
        assert _is_balanced(out)
        assert _tags(out) <= TG_ALLOWED_TAGS


def test_truncate_html_handles_mid_markdown_cut():
    for src in ("**" + "x" * 3000, "[a](https://t.test)" * 200, "```\n" + "c" * 2000):
        out = _truncate_html(src, MAX_TG_CAPTION)
        assert len(out) <= MAX_TG_CAPTION
        assert _is_balanced(out)


def test_tg_plain_text_strips_tags():
    assert _tg_plain_text("<b>Hi</b> &amp; bye") == "Hi & bye"


@pytest.mark.asyncio
async def test_send_to_subscriber_uses_album_for_multiple_images(monkeypatch):
    delivery = MessageDelivery(telegram_bot_token="tok")
    calls = {"group": [], "photo": [], "text": []}

    async def fake_group(chat_id, image_urls, **kw):
        calls["group"].append(list(image_urls))
        return True

    async def fake_photo(chat_id, photo_url, caption, **kw):
        calls["photo"].append((photo_url, caption))
        return True

    async def fake_text(chat_id, text, **kw):
        calls["text"].append(text)
        return True

    monkeypatch.setattr(delivery, "send_telegram_media_group", fake_group)
    monkeypatch.setattr(delivery, "send_telegram_photo", fake_photo)
    monkeypatch.setattr(delivery, "send_telegram", fake_text)

    sub = Subscriber(subscription_id=1, umo="telegram:12345")
    images = ["https://i.test/1.jpg", "https://i.test/2.jpg", "https://i.test/3.jpg"]
    ok = await delivery.send_to_subscriber(sub, "📰 **标题**", images=images)

    assert ok is True
    assert calls["group"] == [images]
    assert calls["photo"] == []
    # The album cannot carry the article text, so it follows as its own message.
    assert calls["text"] == ["📰 **标题**"]


@pytest.mark.asyncio
async def test_send_to_subscriber_falls_back_to_photos_when_album_fails(monkeypatch):
    delivery = MessageDelivery(telegram_bot_token="tok")
    calls = {"group": [], "photo": [], "text": []}

    async def fake_group(chat_id, image_urls, **kw):
        calls["group"].append(list(image_urls))
        return False  # e.g. bilibili rejecting the header-less request

    async def fake_photo(chat_id, photo_url, caption, **kw):
        calls["photo"].append((photo_url, caption))
        return True

    async def fake_text(chat_id, text, **kw):
        calls["text"].append(text)
        return True

    monkeypatch.setattr(delivery, "send_telegram_media_group", fake_group)
    monkeypatch.setattr(delivery, "send_telegram_photo", fake_photo)
    monkeypatch.setattr(delivery, "send_telegram", fake_text)

    sub = Subscriber(subscription_id=1, umo="telegram:12345")
    images = ["https://i.test/1.jpg", "https://i.test/2.jpg"]
    ok = await delivery.send_to_subscriber(sub, "正文", images=images)

    assert ok is True
    assert len(calls["photo"]) == 2  # no image silently dropped
    # Text rides on the first photo only, rather than repeating under each one.
    assert calls["photo"][0][1] == "正文"
    assert calls["photo"][1][1] == ""
    assert calls["text"] == []


@pytest.mark.asyncio
async def test_send_to_subscriber_dedupes_image_urls(monkeypatch):
    """Duplicate URLs make Telegram reject the whole album, so they must be dropped."""
    delivery = MessageDelivery(telegram_bot_token="tok")
    seen = {}

    async def fake_group(chat_id, image_urls, **kw):
        seen["urls"] = list(image_urls)
        return True

    async def fake_photo(chat_id, photo_url, caption, **kw):
        seen.setdefault("photos", []).append(photo_url)
        return True

    async def fake_text(chat_id, text, **kw):
        return True

    monkeypatch.setattr(delivery, "send_telegram_media_group", fake_group)
    monkeypatch.setattr(delivery, "send_telegram_photo", fake_photo)
    monkeypatch.setattr(delivery, "send_telegram", fake_text)

    sub = Subscriber(subscription_id=1, umo="telegram:12345")
    await delivery.send_to_subscriber(
        sub,
        "t",
        images=[
            "https://i.test/1.jpg",
            "https://i.test/2.jpg",
            "https://i.test/1.jpg",
        ],
    )
    assert seen["urls"] == ["https://i.test/1.jpg", "https://i.test/2.jpg"]
def test_format_digest_caption():
    delivery = MessageDelivery()
    caption = delivery.format_digest_caption("科技", 3)
    assert "科技" in caption
    assert "3 篇" in caption
    assert delivery.format_digest_caption("科技") == "📋 **【科技】AI 资讯摘要**"


@pytest.mark.asyncio
async def test_send_digest_images_rejects_non_telegram_targets(tmp_path):
    delivery = MessageDelivery(telegram_bot_token="token")
    image = tmp_path / "digest.png"
    image.write_bytes(b"png")

    webhook = Subscriber(subscription_id=1, umo="https://open.feishu.cn/hook")
    assert await delivery.send_digest_images(webhook, "cap", [image]) is False

    weixin = Subscriber(subscription_id=1, umo="weixin:abc")
    assert await delivery.send_digest_images(weixin, "cap", [image]) is False

    telegram_without_token = MessageDelivery(telegram_bot_token="")
    telegram = Subscriber(subscription_id=1, umo="telegram:12345")
    assert await telegram_without_token.send_digest_images(telegram, "cap", [image]) is False


@pytest.mark.asyncio
async def test_send_telegram_photo_file_missing_file(tmp_path):
    delivery = MessageDelivery(telegram_bot_token="token")
    assert await delivery.send_telegram_photo_file("12345", str(tmp_path / "nope.png")) is False

