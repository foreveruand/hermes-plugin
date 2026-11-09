"""Unit tests for AIFilterService."""

import pytest
from plugins.airss.config import AIRSSConfig
from plugins.airss.database import Database
from plugins.airss.filter import AIFilterService
from plugins.airss.models import RSSArticle


def test_parse_generative_duplicate_results():
    text = """```json
    [
        {"id": 10, "duplicate": true},
        {"id": 11, "duplicate": false}
    ]
    ```"""
    res = AIFilterService.parse_generative_duplicate_results(text, {10, 11})
    assert res == {10: True, 11: False}


def test_parse_generative_invalid():
    res = AIFilterService.parse_generative_duplicate_results("invalid json", {1, 2})
    assert res is None

    # Missing ID in set
    res2 = AIFilterService.parse_generative_duplicate_results(
        '[{"id": 99, "duplicate": true}]', {1, 2}
    )
    assert res2 is None


@pytest.mark.asyncio
async def test_filter_duplicate_articles_mock_llm(tmp_path):
    db = Database(tmp_path / "test_filter.db")
    await db.init_db()

    config = AIRSSConfig()

    async def mock_llm_caller(prompt: str, system_prompt: str = "") -> str:
        return '[{"id": 1, "duplicate": true}, {"id": 2, "duplicate": false}]'

    service = AIFilterService(db, config, llm_caller=mock_llm_caller)

    # First add a candidate article in the DB so filter has something to compare against
    art_cand = RSSArticle(
        subscription_id=1,
        title="Existing Breaking News",
        link="https://example.com/c1",
        guid="c1",
    )
    cand_id = await db.add_article(art_cand)
    assert cand_id is not None

    # New articles to test
    art1 = RSSArticle(id=1, subscription_id=1, title="Breaking News Again", guid="a1", link="https://example.com/a1")
    art2 = RSSArticle(id=2, subscription_id=1, title="Something Completely Different", guid="a2", link="https://example.com/a2")
    await db.add_article(art1)
    await db.add_article(art2)

    duplicates = await service.filter_duplicate_articles([art1, art2])
    assert duplicates == {1}

    # Second run should hit cache without calling LLM
    async def failing_llm(prompt: str, system_prompt: str = "") -> str:
        raise RuntimeError("Should not be called")

    service_cached = AIFilterService(db, config, llm_caller=failing_llm)
    duplicates2 = await service_cached.filter_duplicate_articles([art1, art2])
    assert duplicates2 == {1}

    await db.close()
