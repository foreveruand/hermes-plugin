"""AI topic duplicate filter for AIRSS."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Set

import aiohttp

from .config import AIRSSConfig
from .database import Database
from .models import RSSArticle

logger = logging.getLogger("airss")


class AIFilterService:
    """Detect duplicate news topics using Generative LLM or Decisions API."""

    def __init__(self, db: Database, config: AIRSSConfig, llm_caller: Optional[Callable] = None):
        self.db = db
        self.config = config
        self.llm_caller = llm_caller

    @staticmethod
    def parse_generative_duplicate_results(
        text: str, article_ids: Set[int]
    ) -> Optional[Dict[int, bool]]:
        normalized = (text or "").strip()
        if normalized.startswith("```"):
            normalized = normalized.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            return None

        if isinstance(payload, dict):
            payload = payload.get("results")
        if not isinstance(payload, list):
            return None

        results: Dict[int, bool] = {}
        for item in payload:
            if not isinstance(item, dict):
                return None
            article_id = item.get("id")
            duplicate = item.get("duplicate")
            if (
                not isinstance(article_id, int)
                or article_id not in article_ids
                or not isinstance(duplicate, bool)
            ):
                return None
            results[article_id] = duplicate
        return results

    async def get_decision_model_duplicate_results(
        self,
        current_articles: List[dict],
        candidate_articles: List[dict],
    ) -> Dict[int, bool]:
        ai_config = self.config.ai_config
        url = str(ai_config.get("ai_filter_decision_url", "") or "").strip()
        api_key = str(ai_config.get("ai_filter_decision_key", "") or "").strip()
        model = str(ai_config.get("ai_filter_decision_model", "") or "").strip()
        threshold = float(ai_config.get("ai_filter_decision_threshold", 0.5) or 0.5)

        if not url or not api_key or not model:
            raise ValueError("Decisions API url/key/model must be configured for decision model type")

        questions = {
            f"q_{article['id']}": {
                "type": "noul",
                "instructions": (
                    f"当前文章《{article['title']}》是否与任一已有文章表达同一事件、同一新闻或高度重复主题？"
                    "同一主体但不同事件，以及同一事件的实质性新进展，都算不重复。"
                ),
                "criteria": {
                    "true": "与任一已有文章重复",
                    "false": "不与任何已有文章重复",
                },
            }
            for article in current_articles
        }
        state = json.dumps(
            {"当前文章": current_articles, "已有文章": candidate_articles},
            ensure_ascii=False,
        )
        payload = {"model": model, "state": state, "questions": questions}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status != 200:
                    raise ValueError(f"Decision model API returned status {response.status}")
                data = await response.json()

        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("Decision model response is missing answers")

        results: Dict[int, bool] = {}
        for article in current_articles:
            answer = answers.get(f"q_{article['id']}")
            noul = answer.get("noul") if isinstance(answer, dict) else None
            if not isinstance(noul, (int, float)) or isinstance(noul, bool):
                raise ValueError("Decision model response is missing valid per-article answers")
            results[article["id"]] = float(noul) >= threshold
        return results

    async def filter_duplicate_articles(
        self, articles: List[RSSArticle]
    ) -> Set[int]:
        articles_by_id = {
            article.id: article
            for article in articles
            if article.id is not None and (article.title or "").strip()
        }
        if not articles_by_id:
            return set()

        cached_results = await self.db.get_article_ai_filter_results(list(articles_by_id))
        pending_articles = [
            article for aid, article in articles_by_id.items() if aid not in cached_results
        ]
        if not pending_articles:
            return {aid for aid, is_dup in cached_results.items() if is_dup}

        ai_config = self.config.ai_config
        recent_minutes = int(ai_config.get("ai_filter_recent_minutes", 30) or 30)
        model_type = str(ai_config.get("ai_filter_model_type", "generative") or "generative").strip()

        pending_ids = set(articles_by_id) - set(cached_results)
        candidates = await self.db.get_recent_ai_filter_candidates(
            recent_minutes, exclude_article_ids=list(articles_by_id)
        )
        if not candidates:
            await self.db.set_article_ai_filter_results(dict.fromkeys(pending_ids, False))
            return {aid for aid, is_dup in cached_results.items() if is_dup}

        current_payload = [{"id": a.id, "title": (a.title or "").strip()} for a in pending_articles]
        candidate_payload = [{"id": cid, "title": ctitle} for cid, ctitle in candidates]

        results: Dict[int, bool] = {}
        try:
            if model_type == "decision":
                results = await self.get_decision_model_duplicate_results(
                    current_payload, candidate_payload
                )
            else:
                if not self.llm_caller:
                    raise RuntimeError("No LLM caller provided for generative filter")

                prompt = (
                    "判断当前 RSS 文章是否与已有文章标题表达同一事件、同一新闻或高度重复主题。\n"
                    "同一主体但不同事件，以及同一事件的实质性新进展，都不算重复。\n"
                    "逐项判断每篇当前文章是否与任一已有文章重复。返回项的 id 必须是当前文章的 id，"
                    "绝不能使用已有文章的 id。每篇当前文章恰好返回一项。\n"
                    '只返回 JSON 数组，每项必须是 {"id": 当前文章 ID, "duplicate": 布尔值}，不要解释。\n\n'
                    f"当前文章：{json.dumps(current_payload, ensure_ascii=False)}\n"
                    f"已有文章：{json.dumps(candidate_payload, ensure_ascii=False)}"
                )
                system_prompt = "你是 RSS 新闻主题去重分类器。只输出指定 JSON。"
                raw_response = await self.llm_caller(prompt=prompt, system_prompt=system_prompt)
                parsed = self.parse_generative_duplicate_results(raw_response, pending_ids)
                if parsed is None:
                    raise ValueError("AI duplicate filter returned invalid output")
                results = parsed
        except Exception as exc:
            logger.warning(f"AI duplicate filter failed for articles {pending_ids}: {exc}")
            # Fail open: default to False
            results = {aid: False for aid in pending_ids}

        # Ensure all pending articles have an entry
        results = {aid: results.get(aid, False) for aid in pending_ids}
        await self.db.set_article_ai_filter_results(results)
        cached_results.update(results)

        return {aid for aid, is_dup in cached_results.items() if is_dup}
