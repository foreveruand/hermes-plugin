"""AI digest generation for AIRSS in Hermes Agent."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Callable, List, Optional, Tuple

from .config import AIRSSConfig
from .database import Database
from .models import RSSArticle, RSSGroup

logger = logging.getLogger("airss")

# Appended to every persona prompt: a persona that mentions "use web search tools"
# would otherwise make the model answer with a plan or a tool-call preamble, because
# _call_llm is a single round-trip with no tools attached.
PURE_INDUCTION_SUFFIX = (
    "\n\n【重要执行约束】本任务为纯归纳模式，系统未提供外部工具与网络搜索。"
    "请直接输出最终排版好的正文内容，绝不要输出任何思考规划、问候语或开场白"
    "（严禁输出“好的”、“接下来我将”、“先核对”等），禁止使用或提及任何工具调用，"
    "必须且只能基于提供的已有文章内容生成摘要。"
)

DEFAULT_SYSTEM_PROMPT = (
    "你是一位资深资讯编辑，负责把零散文章整理成可以快速扫读的中文日报。\n"
    "本任务为纯归纳模式：严禁尝试调用任何外部工具或网络搜索，禁止提及工具调用，"
    "必须且只能基于下方提供的 RSS 文章内容进行归纳。\n"
    "输出会被直接排版成一张卡片，请严格按以下格式输出 Markdown：\n"
    "1. 第一行用 `# ` 写一个 24 字以内的总标题，概括当天最值得关注的主线；\n"
    "2. 用 3-6 个 `## ` 小节归类内容，小节标题控制在 12 字以内；\n"
    "3. 每个小节写 2-5 条 `- ` 要点，先陈述事实再点出影响或看点，单条不超过 70 字；\n"
    "4. 用 **粗体** 标出关键主体、数字或结论，不要整句加粗；\n"
    "5. 信息足够时，用 `## 观察` 收尾，写 1-2 句编辑判断；\n"
    "6. 只依据提供的文章内容归纳，不编造、不复述原文链接，不使用代码块、表格、图片与多级嵌套；\n"
    "7. 严格禁止开场白与规划语：不要输出「好的」「接下来我将」「先核对」等任何问候或思考过程，"
    "从第一行 `# ` 标题直接开始。"
)


class DigestService:
    """Generate AI digests for RSS articles."""

    def __init__(
        self,
        db: Database,
        config: AIRSSConfig,
        llm_facade: Optional[Any] = None,
        llm_caller: Optional[Callable] = None,
    ):
        self.db = db
        self.config = config
        self.llm_facade = llm_facade
        self.llm_caller = llm_caller

    def _get_ai_config(self) -> dict:
        return self.config.ai_config

    async def _get_custom_prompt(self, group_id: int) -> str:
        """总结提示词的自定义值，优先级：分组覆盖 > 展板面板全局设置。

        分组覆盖是 groups.persona_prompt（原有功能）；面板全局设置是
        board_config 的 'summary_prompt'，由两个展板（Hermes airss 展板 /
        Muse RSS 展板）的「总结」面板写入。都没有时返回空串，用内置默认。
        """
        group = await self.db.get_group(group_id)
        persona = (group.persona_prompt or "").strip() if group else ""
        if persona:
            return persona
        if self.db is not None:
            try:
                board_prompt = await self.db.get_board_config("summary_prompt")
            except Exception:
                board_prompt = None
            if board_prompt and board_prompt.strip():
                return board_prompt.strip()
        return ""

    async def _get_group_system_prompt(self, group_id: int) -> str:
        persona = await self._get_custom_prompt(group_id)
        if not persona:
            return DEFAULT_SYSTEM_PROMPT
        if PURE_INDUCTION_SUFFIX.strip() in persona:
            return persona
        return persona + PURE_INDUCTION_SUFFIX

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        if limit <= 0:
            return ""
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"

    def _trim_candidates(
        self,
        articles: List[RSSArticle],
        title_max_len: int,
        content_max_len: int,
    ) -> List[RSSArticle]:
        trimmed: List[RSSArticle] = []
        for article in articles:
            truncated_title = self._truncate(article.title or "", title_max_len)
            truncated_content = self._truncate(article.content or "", content_max_len)

            if not truncated_title and not truncated_content:
                continue

            trimmed_article = RSSArticle(
                id=article.id,
                subscription_id=article.subscription_id,
                title=truncated_title,
                link=article.link,
                content=truncated_content,
                guid=article.guid,
                author=article.author,
                published_at=article.published_at,
                fetched_at=article.fetched_at,
                is_sent=article.is_sent,
                image_urls=article.image_urls,
            )
            trimmed.append(trimmed_article)
        return trimmed

    @staticmethod
    def _count_tokens(text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 3)

    def _build_prompt(self, articles: List[RSSArticle]) -> str:
        lines = [
            "请根据以下 RSS 文章内容生成新闻摘要与资讯日报：",
            "",
            "文章列表：",
            "",
        ]

        for i, article in enumerate(articles, 1):
            title = article.title or ""
            content = article.content or ""
            link = article.link or ""
            author = f" (作者: {article.author})" if article.author else ""

            lines.append(f"{i}. 标题: {title}{author}")
            if content:
                lines.append(f"   内容摘要: {content}")
            if link:
                lines.append(f"   原文链接: {link}")
            lines.append("")

        lines.append(
            "请直接输出排版好的 Markdown 摘要正文：第一行必须是 `# ` 标题，"
            "不要任何开场白、问候语、思考规划或工具调用说明。"
        )
        return "\n".join(lines)

    _FENCE_WRAP_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", re.S)
    # A digest starts at the first heading line, or at a section emoji line.
    _HEADING_START_RE = re.compile(r"(?m)^(?=#{1,2}\s|[\U0001F4C8\U0001F4CB\U0001F4CC\U0001F4F0\U0001F4A1\U0001F50D\u2728])")
    _HEADING_RE = re.compile(r"(?m)^#{1,6}\s+\S")
    _BULLET_RE = re.compile(r"(?m)^\s*(?:[-*+]\s+|\d+[.)]\s+)")
    # Opening small talk / plan-of-action lines emitted before the real content.
    _CHATTER_RE = re.compile(
        r"^\s*(?:好的|好|当然可以|当然|没问题|明白|了解|收到|嗯+|ok|okay|"
        r"接下来|下面|以下是|这是|我将|我会|我们来|我们先|先核对|首先|"
        r"需要先|让我)"
        r"[^。！？!?\n]{0,60}?[，,：:。!！?？\n]",
        re.IGNORECASE,
    )
    _TRUNCATION_MARKERS = ("····", "……", "...", "…", "等等", "待续", "（未完）", "(未完)")
    _TRUNCATION_TAIL_CHARS = (
        "，", ",", "、", "：", ":", "；", ";", "-", "—", "(", "（", "【", "《", "“",
    )

    @staticmethod
    def _has_content(line: str) -> bool:
        return bool(re.search(r"[0-9A-Za-z\u4e00-\u9fff]", line or ""))

    @classmethod
    def _strip_leading_chatter(cls, text: str) -> str:
        """Drop small talk / planning text sitting before the first heading."""
        heading = cls._HEADING_START_RE.search(text)
        if heading:
            # Only discard the prefix when it is prose: a leading bullet list is
            # real content, not chatter.
            if heading.start() == 0 or not cls._BULLET_RE.search(text[: heading.start()]):
                return text[heading.start():]

        for _ in range(6):
            original = text
            text = text.lstrip()
            match = cls._CHATTER_RE.match(text)
            if match:
                text = text[match.end():]
            lines = text.split("\n")
            while lines and not cls._has_content(lines[0]):
                lines.pop(0)
            text = "\n".join(lines)
            if text == original:
                break
        return text

    @classmethod
    def _clean_digest(cls, text: str) -> str:
        """Unwrap a fenced block, drop chit-chat openers and collapse blank lines."""
        text = (text or "").strip()
        fenced = cls._FENCE_WRAP_RE.match(text)
        if fenced:
            text = fenced.group(1).strip()
        text = cls._strip_leading_chatter(text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    @classmethod
    def _is_truncated(cls, text: str, finish_reason: Optional[str] = None) -> bool:
        """True when the response stopped early or stops mid-sentence."""
        if (finish_reason or "").strip().lower() in ("length", "max_tokens"):
            return True
        stripped = (text or "").rstrip().rstrip("*|`~>").rstrip()
        if not stripped:
            return True
        if stripped.endswith(cls._TRUNCATION_MARKERS):
            return True
        return stripped.endswith(cls._TRUNCATION_TAIL_CHARS)

    def validate_digest(
        self,
        text: str,
        finish_reason: Optional[str] = None,
        min_chars: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """Return ``(ok, reason)``; a rejected digest must never reach the client."""
        if (finish_reason or "").strip().lower() in ("length", "max_tokens"):
            return False, "truncated by max output tokens"

        text = (text or "").strip()
        if not text:
            return False, "empty content"

        if min_chars is None:
            min_chars = int(self._get_ai_config().get("ai_digest_min_chars", 150) or 150)
        if len(re.sub(r"\s+", "", text)) < min_chars:
            return False, "content too short"

        if not (self._HEADING_RE.search(text) or self._BULLET_RE.search(text)):
            return False, "missing digest structure"

        if self._is_truncated(text, finish_reason):
            return False, "content ends mid-sentence"

        return True, ""

    def _generate_fallback(
        self, articles: List[RSSArticle], fallback_message: str = ""
    ) -> str:
        if fallback_message:
            return fallback_message

        lines = ["📌 **RSS 资讯简报（未启用 AI 总结或总结失败回退）**\n"]
        for article in articles:
            title = self._truncate(article.title or "无标题", 60)
            link = article.link or ""
            if link:
                lines.append(f"- [{title}]({link})")
            else:
                lines.append(f"- {title}")

        return "\n".join(lines)

    @classmethod
    def _extract_result(cls, result: Any) -> Tuple[str, Optional[str]]:
        """Normalise whatever the caller/facade returned into ``(text, finish_reason)``."""
        if isinstance(result, str):
            return result, None
        if result is None:
            return "", None
        if isinstance(result, tuple):
            content = result[0] if result else ""
            finish = result[1] if len(result) > 1 else None
            return str(content if content is not None else ""), (
                str(finish).strip().lower() if finish else None
            )

        content = getattr(result, "text", None)
        if content is None:
            content = str(result)

        finish = getattr(result, "finish_reason", None) or getattr(result, "stop_reason", None)
        if not finish:
            metadata = getattr(result, "metadata", None)
            if isinstance(metadata, dict):
                finish = metadata.get("finish_reason") or metadata.get("stop_reason")
        return str(content), (str(finish).strip().lower() if finish else None)

    async def _call_llm(
        self, prompt: str, system_prompt: str, max_tokens: int
    ) -> Tuple[str, Optional[str]]:
        if self.llm_caller:
            result = await self.llm_caller(
                prompt=prompt, system_prompt=system_prompt, max_tokens=max_tokens
            )
            return self._extract_result(result)
        if self.llm_facade and hasattr(self.llm_facade, "acomplete"):
            try:
                from agent.secret_scope import _SECRET_SCOPE, set_secret_scope, build_profile_secret_scope
                if not _SECRET_SCOPE.get():
                    from hermes_constants import get_hermes_home
                    phome = get_hermes_home()
                    set_secret_scope(build_profile_secret_scope(phome), profile_home=str(phome))
            except Exception:
                pass

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            ai_cfg = self._get_ai_config()
            raw_provider = str(ai_cfg.get("ai_provider") or "").strip()
            raw_model = str(ai_cfg.get("ai_model") or "").strip()

            provider = None
            model = None
            if raw_provider:
                if "/" in raw_provider:
                    p_part, m_part = raw_provider.split("/", 1)
                    provider = p_part.strip() or None
                    model = m_part.strip() or None
                else:
                    provider = raw_provider
            if raw_model:
                model = raw_model

            timeout = float(ai_cfg.get("ai_timeout", 240.0) or 240.0)

            call_kwargs = {
                "messages": messages,
                "max_tokens": max_tokens,
                "timeout": timeout,
            }
            if provider:
                call_kwargs["provider"] = provider
            if model:
                call_kwargs["model"] = model

            result = await self.llm_facade.acomplete(**call_kwargs)
            return self._extract_result(result)
        raise RuntimeError("No LLM caller or facade configured.")

    async def generate_digest(
        self,
        articles: List[RSSArticle],
        group_id: int,
        article_signature: Optional[str] = None,
    ) -> Tuple[str, int]:
        """Generate an AI digest from articles."""
        if not articles:
            return "暂无新文章。", 0

        ai_config = self._get_ai_config()
        max_articles = int(ai_config.get("ai_digest_max_articles", 50))
        max_input_tokens = int(ai_config.get("ai_digest_max_input_tokens", 131072))
        max_output_tokens = int(ai_config.get("ai_digest_max_output_tokens", 8192))
        title_max_len = int(ai_config.get("ai_digest_title_max_len", 120))
        content_max_len = int(ai_config.get("ai_digest_content_max_len", 2048))
        fallback_msg = str(ai_config.get("ai_fallback_message", "") or "")

        trimmed = self._trim_candidates(
            articles[:max_articles], title_max_len, content_max_len
        )
        if not trimmed:
            return "暂无新文章。", 0

        prompt = self._build_prompt(trimmed)

        while self._count_tokens(prompt) > max_input_tokens and len(trimmed) > 1:
            trimmed.pop()
            prompt = self._build_prompt(trimmed)

        if self._count_tokens(prompt) > max_input_tokens:
            logger.warning("Digest input exceeds token budget=%s, using fallback", max_input_tokens)
            return self._generate_fallback(trimmed, fallback_msg), len(trimmed)

        system_prompt = await self._get_group_system_prompt(group_id)

        try:
            digest, finish_reason = await self._call_llm(
                prompt=prompt,
                system_prompt=system_prompt,
                max_tokens=max_output_tokens,
            )
            cleaned = self._clean_digest(digest)
            ok, reason = self.validate_digest(cleaned, finish_reason)
            if not ok:
                logger.warning(
                    "Digest rejected (%s, finish_reason=%s, chars=%d); falling back",
                    reason,
                    finish_reason,
                    len(cleaned),
                )
                return self._generate_fallback(trimmed, fallback_msg), len(trimmed)
            return cleaned, len(trimmed)
        except Exception as exc:
            logger.warning("LLM digest generation failed: %s; falling back to link directory", exc)
            return self._generate_fallback(trimmed, fallback_msg), len(trimmed)

    async def generate_single_summary(
        self, article: RSSArticle, group_id: int
    ) -> str:
        """Generate summary for a single article using group's Persona prompt."""
        system_prompt = await self._get_group_system_prompt(group_id)
        prompt = (
            "请用简明扼要的 1-2 句话总结以下文章要点：\n\n"
            f"标题: {article.title}\n\n"
            f"内容: {self._truncate(article.content or '', 1000)}"
        )

        try:
            summary, finish_reason = await self._call_llm(
                prompt=prompt,
                system_prompt=system_prompt,
                max_tokens=500,
            )
            cleaned = self._clean_digest(summary)
            if not cleaned or self._is_truncated(cleaned, finish_reason):
                logger.warning(
                    "Single summary rejected (finish_reason=%s); falling back to title",
                    finish_reason,
                )
                return article.title or "无标题"
            return cleaned
        except Exception as exc:
            logger.warning("Single summary LLM failed: %s", exc)
            return article.title or "无标题"

    @staticmethod
    def build_article_signature(articles: List[RSSArticle]) -> str:
        article_ids = sorted(
            str(article.id) for article in articles if article.id is not None
        )
        if article_ids:
            return ",".join(article_ids)
        return hashlib.sha1(
            "|".join(
                f"{article.subscription_id}:{article.guid}:{article.link}"
                for article in articles
            ).encode("utf-8")
        ).hexdigest()
