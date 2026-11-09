"""Unit tests for RSSFetcher."""

import pytest
from plugins.airss.fetcher import RSSFetcher


def test_rsshub_url_building():
    fetcher = RSSFetcher(rsshub_url="https://my-rsshub.test", rsshub_key="secret123")
    url = fetcher.build_rsshub_url("/bilibili/user/video/12345")
    assert url.startswith("https://my-rsshub.test/bilibili/user/video/12345?code=")
    # code should be md5 of "/bilibili/user/video/12345secret123"
    import hashlib

    expected_code = hashlib.md5("/bilibili/user/video/12345secret123".encode()).hexdigest()
    assert f"code={expected_code}" in url


def test_rsshub_url_without_key():
    fetcher = RSSFetcher(rsshub_url="https://rsshub.app")
    url = fetcher.build_rsshub_url("/github/repos/NousResearch")
    assert url == "https://rsshub.app/github/repos/NousResearch"


def test_parse_entries_and_images():
    fetcher = RSSFetcher()
    entries = [
        {
            "id": "item-1",
            "title": "Test Title",
            "content": [{"value": '<p>Hello world <img src="https://img.test/pic.png"/></p>'}],
            "link": "https://example.com/item1",
            "author": "Alice",
        }
    ]
    articles = fetcher._parse_entries(entries)
    assert len(articles) == 1
    art = articles[0]
    assert art.title == "Test Title"
    assert art.author == "Alice"
    assert art.image_urls == ["https://img.test/pic.png"]
