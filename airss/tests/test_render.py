"""Unit tests for digest image rendering."""

from pathlib import Path

import pytest
from PIL import Image

from plugins.airss import render
from plugins.airss.render import (
    BG_RGB,
    _pack_pages,
    _split_blocks,
    _trim_bottom,
    build_digest_html,
    markdown_to_html,
    render_digest_images,
)


def test_markdown_to_html_renders_common_blocks():
    html = markdown_to_html(
        "# 标题\n\n## 小节\n- 要点 **重点** 内容\n\n> 引用\n\n[链接](https://example.com)"
    )
    assert "<h1>标题</h1>" in html
    assert "<h2>小节</h2>" in html
    assert "<strong>重点</strong>" in html
    assert "<li>" in html
    assert "<blockquote>" in html
    assert 'href="https://example.com"' in html


def test_markdown_to_html_strips_scripts():
    html = markdown_to_html('正文\n\n<script>alert("x")</script>')
    assert "<script" not in html.lower()
    assert "正文" in html


def test_build_digest_html_lifts_title_into_header():
    doc = build_digest_html(
        "# 今日看点\n\n## 小节\n- 一条要点",
        group_name="科技",
        subtitle="2026-10-17 · 3 篇资讯",
        page_label="1 / 2",
    )
    assert '<h1>今日看点</h1>' in doc
    # Title must appear in the card header only, not duplicated in the body.
    assert doc.count("今日看点") == 1
    assert "2026-10-17 · 3 篇资讯" in doc
    assert "1 / 2" in doc
    assert "AI Digest" in doc


def test_build_digest_html_falls_back_to_group_name():
    doc = build_digest_html("- 只有列表，没有标题", group_name="财经")
    assert "<h1>财经</h1>" in doc


def test_pack_pages_splits_on_budget():
    md = "\n\n".join(f"## 第 {i} 节\n- 一条要点内容" for i in range(40))
    pages = _pack_pages(_split_blocks(md), 300.0)
    assert len(pages) > 1
    assert all(pages), "no page may be empty"
    # Every block must survive pagination exactly once.
    assert sum(len(page) for page in pages) == len(_split_blocks(md))


def test_pack_pages_keeps_single_oversized_block():
    pages = _pack_pages(["## 超长小节\n" + "- 内容" * 5000], 100.0)
    assert len(pages) == 1


def test_trim_bottom_crops_uniform_background():
    image = Image.new("RGB", (200, 400), BG_RGB)
    for x in range(0, 200):
        for y in range(0, 100):
            image.putpixel((x, y), (255, 255, 255))
    trimmed = _trim_bottom(image, BG_RGB)
    assert trimmed.size == (200, 124)


@pytest.mark.asyncio
async def test_render_returns_empty_without_chromium(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "_find_chrome", lambda: None)
    paths = await render_digest_images("# 标题\n\n- 内容", tmp_path, group_name="测试")
    assert paths == []


@pytest.mark.asyncio
async def test_render_produces_png_pages(tmp_path):
    if not render._find_chrome():
        pytest.skip("Chromium not available")
    md = "\n\n".join(
        f"## 第 {i} 节\n- **主体** 发布重要更新，涉及模型能力与生态合作。" for i in range(3)
    )
    paths = await render_digest_images(f"# 今日速览\n\n{md}", tmp_path, group_name="科技")
    assert paths
    with Image.open(paths[0]) as image:
        assert image.size[0] == 1800
        assert image.size[1] > 200
