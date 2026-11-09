"""RSS Fetcher - Async RSS feed fetching and parsing."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Set
from urllib.parse import quote, urlsplit

import aiohttp
import feedparser

from .models import RSSArticle

logger = logging.getLogger("airss")

RSSHUB_PATH_SAFE_CHARS = "/:@!$&'()*+,;="
PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")


@dataclass
class FetchResult:
    """Result of fetching an RSS feed."""

    success: bool
    articles: List[RSSArticle] = field(default_factory=list)
    feed_title: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    error: Optional[str] = None


class RSSFetcher:
    """Async RSS feed fetcher."""

    def __init__(
        self,
        proxy: Optional[str] = None,
        proxy_enabled: bool = False,
        timeout: int = 30,
        rsshub_url: Optional[str] = None,
        rsshub_key: Optional[str] = None,
    ):
        self.proxy = proxy
        self.proxy_enabled = proxy_enabled
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.rsshub_url = rsshub_url.rstrip("/") if rsshub_url else None
        self.rsshub_key = rsshub_key.strip() if rsshub_key else None

    @staticmethod
    def _quote_route_path(path: str) -> str:
        escapes: List[str] = []

        def stash_escape(match: re.Match) -> str:
            escapes.append(match.group(0))
            return f"__RSSHUB_PCT_{len(escapes) - 1}__"

        protected_path = PERCENT_ESCAPE_RE.sub(stash_escape, path)
        quoted_path = quote(protected_path, safe=RSSHUB_PATH_SAFE_CHARS)

        for index, escape in enumerate(escapes):
            quoted_path = quoted_path.replace(f"__RSSHUB_PCT_{index}__", escape)

        return quoted_path

    def build_rsshub_url(self, path: Optional[str]) -> str:
        path = (path or "").strip()
        parsed_path = urlsplit(path)
        route = self._quote_route_path(parsed_path.path.lstrip("/"))
        query = parsed_path.query

        if not self.rsshub_url:
            base = "https://rsshub.app"
        else:
            base = self.rsshub_url

        if not route:
            return base

        if self.rsshub_key:
            request_path = f"/{route}"
            code = hashlib.md5(f"{request_path}{self.rsshub_key}".encode()).hexdigest()
            query = f"{query}&code={code}" if query else f"code={code}"

        query_suffix = f"?{query}" if query else ""
        return f"{base}/{route}{query_suffix}"

    async def fetch_feed(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        cookies: Optional[str] = None,
        enable_proxy: Optional[bool] = None,
    ) -> FetchResult:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        if cookies:
            headers["Cookie"] = cookies

        use_proxy = self.proxy_enabled if enable_proxy is None else enable_proxy
        proxy = self.proxy if (self.proxy and use_proxy) else None

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, headers=headers, proxy=proxy) as response:
                    if response.status == 304:
                        return FetchResult(
                            success=True,
                            articles=[],
                            etag=etag,
                            last_modified=last_modified,
                        )

                    if response.status != 200:
                        return FetchResult(
                            success=False,
                            error=f"HTTP {response.status}",
                        )

                    content = await response.text()
                    new_etag = response.headers.get("ETag")
                    new_last_modified = response.headers.get("Last-Modified")

            feed = feedparser.parse(content)

            if feed.bozo and not feed.entries:
                return FetchResult(
                    success=False,
                    error=str(feed.bozo_exception) if feed.bozo_exception else "Parse error",
                )

            articles = self._parse_entries(feed.entries)
            feed_title = None
            if hasattr(feed, "feed") and hasattr(feed.feed, "title"):
                feed_title = feed.feed.title
                if isinstance(feed_title, dict):
                    feed_title = feed_title.get("value")

            return FetchResult(
                success=True,
                articles=articles,
                feed_title=feed_title,
                etag=new_etag,
                last_modified=new_last_modified,
            )

        except aiohttp.ClientError as e:
            logger.error(f"Failed to fetch RSS feed {url}: {e}")
            return FetchResult(success=False, error=str(e))
        except Exception as e:
            logger.error(f"Unexpected error fetching {url}: {e}")
            return FetchResult(success=False, error=str(e))

    def _parse_entries(self, entries: list) -> List[RSSArticle]:
        articles: List[RSSArticle] = []

        for entry in entries:
            try:
                guid = entry.get("id") or entry.get("guid") or entry.get("link", "")
                title = entry.get("title", "Untitled")
                if isinstance(title, dict):
                    title = title.get("value", "Untitled")

                content = ""
                raw_content = entry.get("content")
                if raw_content and isinstance(raw_content, list) and len(raw_content) > 0:
                    first = raw_content[0]
                    content = first.get("value", "") if isinstance(first, dict) else str(first)
                elif entry.get("summary"):
                    content = entry.get("summary")
                elif entry.get("description"):
                    content = entry.get("description")

                link = entry.get("link", "")
                if isinstance(link, dict):
                    link = link.get("href", "")
                elif isinstance(link, list) and link:
                    link = link[0].get("href", "") if isinstance(link[0], dict) else str(link[0])

                published_at = None
                if entry.get("published_parsed"):
                    try:
                        published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        pass
                elif entry.get("updated_parsed"):
                    try:
                        published_at = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        pass

                author = ""
                if entry.get("author"):
                    author = entry.get("author")
                elif entry.get("author_detail"):
                    detail = entry.get("author_detail")
                    if isinstance(detail, dict):
                        author = detail.get("name", "")
                    elif hasattr(detail, "name"):
                        author = detail.name

                image_urls = self._extract_images(content)

                article = RSSArticle(
                    title=title,
                    content=content,
                    link=link,
                    guid=guid,
                    author=author,
                    published_at=published_at,
                    fetched_at=datetime.now(timezone.utc),
                    image_urls=image_urls,
                )
                articles.append(article)

            except Exception as e:
                logger.warning(f"Failed to parse entry: {e}")
                continue

        return articles

    @staticmethod
    def _extract_images(content: str) -> List[str]:
        pattern = r'<img[^>]+src=["\']([^"\']+)["\']'
        matches = re.findall(pattern, content, re.IGNORECASE)
        return matches[:5]


def detect_new_articles(
    articles: List[RSSArticle], existing_guids: Set[str]
) -> List[RSSArticle]:
    return [a for a in articles if a.guid not in existing_guids]
