"""Unit tests for DigestService."""

import pytest
from plugins.airss.config import AIRSSConfig
from plugins.airss.database import Database
from plugins.airss.digest import DigestService
from plugins.airss.models import RSSArticle, RSSGroup

COMPLETE_DIGEST = (
    "# 今日财经与科技综述\n\n"
    "## 市场\n"
    "- **沪指** 收涨 1.24%，两市成交额回升至 **1.18 万亿**，北向资金净流入 42 亿元。\n"
    "- **债市** 收益率小幅下行，十年期国债收益率报 2.31%，市场对降准预期升温。\n"
    "## 行业\n"
    "- **半导体** 板块领涨，先进制程设备订单同比翻倍，国产替代节奏快于市场预期。\n"
    "- **新能源车** 月度交付创新高，头部厂商毛利率回升至 **18.6%**，价格战压力缓解。\n"
    "## 观察\n"
    "- 资金面改善叠加产业景气回升，短期风险偏好有望延续，但需留意海外利率的扰动。\n"
)


class _LLMResult:
    """Mimics a facade response object that carries a finish_reason."""

    def __init__(self, text: str, finish_reason: str = "stop"):
        self.text = text
        self.finish_reason = finish_reason


def _articles():
    return [
        RSSArticle(
            id=1,
            subscription_id=1,
            title="Quantum Leap",
            content="A new quantum processor was introduced.",
            link="https://example.com/q1",
        ),
        RSSArticle(
            id=2,
            subscription_id=1,
            title="Robotics Advance",
            content="Humanoid robot walks on rough terrain.",
            link="https://example.com/r1",
        ),
    ]


async def _make_service(tmp_path, llm_caller, group_prompt="你是科技总编"):
    db = Database(tmp_path / "test_digest.db")
    await db.init_db()

    group = RSSGroup(id=2, name="Tech Group", persona_prompt=group_prompt)
    await db.add_group(group)

    return DigestService(db, AIRSSConfig(), llm_caller=llm_caller), db


@pytest.mark.asyncio
async def test_generate_digest_mock_llm(tmp_path):
    called_prompts = []

    async def mock_llm_caller(prompt: str, system_prompt: str, max_tokens: int) -> str:
        called_prompts.append((prompt, system_prompt))
        return COMPLETE_DIGEST

    service, db = await _make_service(tmp_path, mock_llm_caller)

    articles = _articles()

    digest, count = await service.generate_digest(articles, group_id=2)
    assert count == 2
    assert "今日财经与科技综述" in digest
    assert len(called_prompts) == 1
    prompt, system_prompt = called_prompts[0]
    # The persona must be kept, but always followed by the pure-induction constraint.
    assert system_prompt.startswith("你是科技总编")
    assert "纯归纳模式" in system_prompt
    assert "Quantum Leap" in prompt
    assert "Robotics Advance" in prompt
    assert "直接输出" in prompt  # tail reminder

    # Test single summary
    single = await service.generate_single_summary(articles[0], group_id=2)
    assert "今日财经与科技综述" in single

    # Test fallback on LLM failure
    async def failing_llm(prompt: str, system_prompt: str, max_tokens: int) -> str:
        raise RuntimeError("API connection timed out")

    service_fail, _ = await _make_service(tmp_path / "fail", failing_llm)
    fallback_digest, f_count = await service_fail.generate_digest(articles, group_id=2)
    assert f_count == 2
    assert "未启用 AI 总结或总结失败回退" in fallback_digest
    assert "[Quantum Leap](https://example.com/q1)" in fallback_digest

    await db.close()


def test_clean_digest_strips_chatter_before_first_heading():
    raw = (
        "好的，接下来我将为你梳理今日财经要点。\n\n"
        "先核对指数、资金和板块的最新信号，再输出摘要。\n\n"
        "# 财经日报\n\n## 市场\n- **沪指** 收涨 1.2%。"
    )
    cleaned = DigestService._clean_digest(raw)

    assert cleaned.startswith("# 财经日报")
    assert "好的" not in cleaned
    assert "接下来" not in cleaned
    assert "先核对" not in cleaned


def test_clean_digest_strips_chatter_without_heading():
    raw = "好的，以下是今日要点：\n\n- **沪指** 收涨 1.2%。"
    cleaned = DigestService._clean_digest(raw)

    assert cleaned.startswith("- **沪指**")
    assert "好的" not in cleaned


def test_clean_digest_unwraps_fenced_block_and_collapses_blanks():
    raw = "```markdown\n# 标题\n\n\n\n- 第一条\n```"
    assert DigestService._clean_digest(raw) == "# 标题\n\n- 第一条"


@pytest.mark.asyncio
async def test_generate_digest_falls_back_when_finish_reason_is_length(tmp_path):
    async def truncated_llm(prompt: str, system_prompt: str, max_tokens: int):
        # A reasoning model that burned its budget thinking: only a few chars of body.
        return _LLMResult("# 今日财经", finish_reason="length")

    service, db = await _make_service(tmp_path, truncated_llm)
    digest, count = await service.generate_digest(_articles(), group_id=2)

    assert count == 2
    assert "未启用 AI 总结或总结失败回退" in digest
    assert "[Quantum Leap](https://example.com/q1)" in digest
    assert "# 今日财经" not in digest

    await db.close()


@pytest.mark.asyncio
async def test_generate_digest_falls_back_when_content_too_short(tmp_path):
    async def short_llm(prompt: str, system_prompt: str, max_tokens: int) -> str:
        return "# 快讯\n\n- 今日市场小幅上涨。"

    service, db = await _make_service(tmp_path, short_llm)
    digest, count = await service.generate_digest(_articles(), group_id=2)

    assert count == 2
    assert "未启用 AI 总结或总结失败回退" in digest
    assert "今日市场小幅上涨" not in digest

    await db.close()


@pytest.mark.asyncio
async def test_generate_digest_falls_back_when_tail_is_truncated(tmp_path):
    async def mid_sentence_llm(prompt: str, system_prompt: str, max_tokens: int) -> str:
        return COMPLETE_DIGEST.rstrip().rstrip("。") + "，"

    service, db = await _make_service(tmp_path, mid_sentence_llm)
    digest, count = await service.generate_digest(_articles(), group_id=2)

    assert count == 2
    assert "未启用 AI 总结或总结失败回退" in digest
    assert "今日财经与科技综述" not in digest

    await db.close()


@pytest.mark.asyncio
async def test_generate_digest_accepts_complete_digest(tmp_path):
    async def good_llm(prompt: str, system_prompt: str, max_tokens: int) -> str:
        return COMPLETE_DIGEST

    service, db = await _make_service(tmp_path, good_llm)
    digest, count = await service.generate_digest(_articles(), group_id=2)

    assert count == 2
    assert digest == COMPLETE_DIGEST.strip()
    assert "未启用 AI 总结或总结失败回退" not in digest

    await db.close()


def test_validate_digest_rules():
    service = DigestService(db=None, config=AIRSSConfig())

    ok, reason = service.validate_digest(COMPLETE_DIGEST)
    assert ok is True and reason == ""

    ok, reason = service.validate_digest(COMPLETE_DIGEST, finish_reason="length")
    assert ok is False and "max output tokens" in reason

    ok, reason = service.validate_digest(COMPLETE_DIGEST, finish_reason="max_tokens")
    assert ok is False and "max output tokens" in reason

    ok, reason = service.validate_digest("# 标题\n- 太短了。")
    assert ok is False and reason == "content too short"

    ok, reason = service.validate_digest(COMPLETE_DIGEST.rstrip().rstrip("。") + "…")
    assert ok is False and reason == "content ends mid-sentence"

    ok, reason = service.validate_digest("这是一段没有标题也没有列表的长文本" * 20 + "。")
    assert ok is False and reason == "missing digest structure"
