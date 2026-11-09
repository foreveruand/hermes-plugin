"""Message delivery and formatting service for AIRSS."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

import aiohttp

from .models import RSSArticle, Subscriber

logger = logging.getLogger("airss")

MAX_WEBHOOK_BYTES = 4090
MAX_TG_CAPTION = 1024
MAX_TG_TEXT = 4096

# Telegram only accepts these HTML tags; anything else makes the API return 400.
TG_ALLOWED_TAGS = frozenset(
    {"b", "i", "u", "s", "code", "pre", "blockquote", "a", "tg-spoiler"}
)
_TG_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")
_TG_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_PROTECT_SPLIT_RE = re.compile(r"(\x00PROTECTED\d+\x00)")
_SCHEME_BLOCKLIST = ("javascript", "data", "vbscript")


def clean_html_text(raw_html: str) -> str:
    """Clean HTML tags and unescape entities for markdown output."""
    if not raw_html:
        return ""
    text = re.sub(r"<(br|p|div|/p|/div)[^>]*>", "\n", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # Collapse multiple consecutive empty lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _tg_sanitize_link_url(url: str) -> str:
    """Reject script-bearing URLs, escape the rest for use in an href."""
    stripped = url.strip()
    if ":" in stripped and stripped.split(":", 1)[0].lower().strip() in _SCHEME_BLOCKLIST:
        return ""
    return html.escape(stripped, quote=True)


def _tg_sanitize_html(text: str) -> str:
    """Drop disallowed tags and balance the rest so Telegram never 400s on markup."""
    out: list[str] = []
    stack: list[str] = []
    pos = 0
    for m in _TG_TAG_RE.finditer(text):
        out.append(text[pos : m.start()])
        pos = m.end()
        tag = m.group(0)
        name = m.group(1).lower()
        if name not in TG_ALLOWED_TAGS:
            continue
        if tag.startswith("</"):
            if name in stack:
                while stack:
                    open_name = stack.pop()
                    out.append(f"</{open_name}>")
                    if open_name == name:
                        break
        else:
            stack.append(name)
            if name == "a":
                # An <a> without href is dropped by Telegram, and the href is the
                # one attribute that carries meaning -- everything else is noise.
                href = _TG_HREF_RE.search(tag)
                # The stored href is already HTML-escaped; unescape before
                # re-validating so the scheme check sees the real scheme and the
                # ampersand is not escaped twice.
                safe = _tg_sanitize_link_url(html.unescape(href.group(1))) if href else ""
                if safe:
                    out.append(f'<a href="{safe}">')
                else:
                    stack.pop()
            else:
                out.append(f"<{name}>")
    out.append(text[pos:])
    out.extend(f"</{name}>" for name in reversed(stack))
    return "".join(out).replace("\x00", "")


def markdown_to_telegram_html(text: str) -> str:
    """Convert Markdown to the restricted HTML subset Telegram's HTML mode accepts.

    Markdown syntax is stashed behind placeholders *before* escaping, because RSS
    titles and bodies routinely contain ``&``, ``<``, ``_`` and ``[`` as literal text
    which must survive as escaped characters, not as syntax.
    """
    if not text:
        return ""
    try:
        stash: list[str] = []

        def protect(fragment: str) -> str:
            stash.append(fragment)
            return f"\x00PROTECTED{len(stash) - 1}\x00"

        # --- pass 1: protect syntax that must survive escaping ---
        result = re.sub(
            r"```(\w*)\n?(.*?)```",
            lambda m: protect(
                f"<pre><code>{html.escape(m.group(2), quote=False)}</code></pre>"
            ),
            text,
            flags=re.DOTALL,
        )
        result = re.sub(
            r"`([^`\n]+)`",
            lambda m: protect(f"<code>{html.escape(m.group(1), quote=False)}</code>"),
            result,
        )
        # Images cannot be embedded via HTML parse_mode; degrade to a labelled link.
        def _link_or_text(text_m: str, url_m: str, label: str) -> str:
            safe = _tg_sanitize_link_url(url_m)
            if not safe:
                # Blocked scheme: emit the label as plain text, never a live link.
                return html.escape(label or url_m, quote=False)
            return f'<a href="{safe}">{html.escape(label, quote=False)}</a>'

        # URLs may contain balanced parens (e.g. wiki links), so match them
        # explicitly rather than stopping at the first ")".
        link_url = r"((?:[^()\s]|\([^()\s]*\))+)"
        result = re.sub(
            rf"!\[([^\]]*)\]\({link_url}\)",
            lambda m: protect(_link_or_text(m.group(1), m.group(2), m.group(1) or m.group(2))),
            result,
        )
        result = re.sub(
            rf"\[([^\]]+)\]\({link_url}\)",
            lambda m: protect(_link_or_text(m.group(1), m.group(2), m.group(1))),
            result,
        )

        # --- pass 2: escape everything that is not a placeholder ---
        result = "".join(
            part
            if part.startswith("\x00PROTECTED")
            else html.escape(part, quote=False)
            for part in _PROTECT_SPLIT_RE.split(result)
        )

        # --- pass 3: line-level blocks (Telegram has no <h1>/<ul>/<hr>/<br>) ---
        lines = result.split("\n")
        out_lines: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if re.match(r"^\s*([-*_])\s*(\1\s*){2,}$", line):
                out_lines.append("———")
                i += 1
                continue
            header = re.match(r"^#{1,6}\s+(.+)$", line)
            if header:
                out_lines.append(f"<b>{header.group(1).strip()}</b>")
                i += 1
                continue
            if line.startswith("&gt; "):
                quote: list[str] = []
                while i < len(lines) and lines[i].startswith("&gt; "):
                    quote.append(lines[i][5:])
                    i += 1
                out_lines.append(f"<blockquote>{chr(10).join(quote)}</blockquote>")
                continue
            bullet = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$", line)
            if bullet:
                out_lines.append(f"• {bullet.group(1)}")
                i += 1
                continue
            out_lines.append(line)
            i += 1
        result = "\n".join(out_lines)

        # --- pass 4: inline spans, applied to non-stashed runs only ---
        def apply_inline(chunk: str) -> str:
            if chunk.startswith("\x00PROTECTED"):
                return chunk
            for pattern, repl in (
                (r"\*\*(.+?)\*\*", "<b>\\1</b>"),
                (r"__(.+?)__", "<b>\\1</b>"),
                (r"~~(.+?)~~", "<s>\\1</s>"),
                (r"\|\|(.+?)\|\|", "<tg-spoiler>\\1</tg-spoiler>"),
                (r"(?<!\*)\*([^*\n]+)\*(?!\*)", "<i>\\1</i>"),
                # Intentionally no `_italic_` rule: RSS bodies are full of
                # snake_case identifiers that it would mangle.
            ):
                chunk = re.sub(pattern, repl, chunk)
            return chunk

        result = "".join(apply_inline(p) for p in _PROTECT_SPLIT_RE.split(result))

        # --- pass 5: restore stashed fragments ---
        for idx, fragment in enumerate(stash):
            result = result.replace(f"\x00PROTECTED{idx}\x00", fragment)

        return _tg_sanitize_html(result)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("markdown_to_telegram_html failed, sending escaped text: %s", exc)
        return html.escape(text, quote=False)


def _tg_plain_text(html_text: str) -> str:
    """Render Telegram HTML back down to readable plain text (for 400 retries)."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text)).strip()


def _truncate_html(markdown_src: str, limit: int) -> str:
    """Truncate Markdown so its converted HTML fits ``limit``, never mid-tag."""
    converted = markdown_to_telegram_html(markdown_src)
    if len(converted) <= limit:
        return converted
    low, high = 0, len(markdown_src)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = markdown_to_telegram_html(markdown_src[:mid])
        if len(candidate) <= limit:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best or markdown_to_telegram_html(markdown_src[:high])


class MessageDelivery:
    """Handles formatting and dispatching RSS messages to subscribers."""

    def __init__(
        self,
        default_webhook: str = "",
        telegram_bot_token: str = "",
        platform_sender: Optional[Callable] = None,
    ):
        self.default_webhook = default_webhook
        self._telegram_bot_token = telegram_bot_token
        self.platform_sender = platform_sender

    @property
    def telegram_bot_token(self) -> str:
        if self._telegram_bot_token:
            return self._telegram_bot_token
        try:
            import os
            token = os.environ.get("TELEGRAM_BOT_TOKEN")
            if token:
                return token
        except Exception:
            pass
        try:
            from hermes_cli.config import get_env_value
            token = get_env_value("TELEGRAM_BOT_TOKEN")
            if token:
                return token
        except Exception:
            pass
        try:
            import os
            from pathlib import Path
            for p in [
                Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")) / ".env",
                Path.home() / ".hermes" / "profiles" / "my-hermes" / ".env",
                Path.home() / ".hermes" / ".env",
            ]:
                if p.is_file():
                    for line in p.read_text(encoding="utf-8").splitlines():
                        if line.startswith("TELEGRAM_BOT_TOKEN="):
                            return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
        return ""

    @telegram_bot_token.setter
    def telegram_bot_token(self, val: str) -> None:
        self._telegram_bot_token = val

    @staticmethod
    def truncate_by_bytes(text: str, max_bytes: int = MAX_WEBHOOK_BYTES) -> str:
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    def format_article(
        self,
        article: RSSArticle,
        sub_name: str,
        personal_config: Optional[Dict[str, Any]] = None,
        for_native_media: bool = False,
    ) -> str:
        """Format an RSS article based on personal preferences."""
        cfg = personal_config or {}
        only_title = bool(cfg.get("only_title", False))
        only_pic = bool(cfg.get("only_pic", False))
        enable_spoiler = bool(cfg.get("enable_spoiler", False))

        title = article.title or "无标题"
        link = article.link or ""
        author = article.author or sub_name or "RSS"
        via_line = f"[{author}]({link})" if link else author

        if only_pic:
            if not for_native_media and article.image_urls:
                imgs = "\n".join(f"![图片]({url})" for url in article.image_urls[:3])
                return f"{imgs}\n\n📰 {title} via {via_line}"
            return f"📰 **{title}**\n🔗 来源: {via_line}"

        if only_title:
            return f"📰 **{title}**\n🔗 来源: {via_line}"

        cleaned_content = clean_html_text(article.content or "")
        if len(cleaned_content) > 800:
            cleaned_content = cleaned_content[:800] + "…"
        if enable_spoiler and cleaned_content:
            cleaned_content = f"||{cleaned_content}||"

        msg_parts = [f"📰 **{title}**"]
        if cleaned_content:
            msg_parts.append(cleaned_content)
        msg_parts.append(f"🔗 来源: {via_line}")

        if not for_native_media and article.image_urls:
            max_img = int(cfg.get("max_image_number", 0) or 0)
            if max_img > 0:
                urls = article.image_urls[:max_img]
                msg_parts.append("\n".join(f"![图片]({u})" for u in urls))

        return "\n\n".join(msg_parts)

    def format_digest(self, digest_text: str, group_name: str) -> str:
        """Format an AI digest report."""
        header = f"📋 **【{group_name}】资讯摘要简报**\n"
        footer = "\n\n— 来自 Hermes AIRSS 智能订阅"
        return f"{header}\n{digest_text.strip()}{footer}"

    async def send_webhook(
        self,
        webhook_url: str,
        text: str,
        timeout: int = 15,
    ) -> bool:
        """Send markdown payload to webhook (WeCom / Feishu / generic)."""
        if not text or not webhook_url:
            return False

        truncated_text = self.truncate_by_bytes(text, MAX_WEBHOOK_BYTES)

        # Detect Feishu vs WeCom vs Generic
        if "open.feishu.cn" in webhook_url:
            payload = {
                "msg_type": "text",
                "content": {"text": truncated_text},
            }
        else:
            # Default to WeCom markdown format
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": truncated_text},
            }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        err = data.get("errcode") or data.get("code")
                        if err is None or err == 0:
                            return True
                        logger.warning(f"Webhook error response: {data}")
                        return False
                    logger.warning(f"Webhook HTTP error: {resp.status}")
                    return False
        except Exception as exc:
            logger.error(f"Failed to post to webhook {webhook_url}: {exc}")
            return False

    async def send_telegram(
        self,
        chat_id: str | int,
        text: str,
        bot_token: Optional[str] = None,
        thread_id: Optional[int] = None,
        timeout: int = 20,
    ) -> bool:
        """Send message via Telegram Bot API as HTML with link preview disabled.

        ``text`` arrives as Markdown; Telegram's legacy ``Markdown`` parse_mode cannot
        render ``**bold**``, so it is converted to HTML here rather than at format time
        (webhook targets still need the original Markdown).
        """
        token = bot_token or self.telegram_bot_token
        if not token:
            logger.warning("Cannot send Telegram message: telegram_bot_token is not configured")
            return False

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        html_text = _truncate_html(text, MAX_TG_TEXT)
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": html_text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "disable_web_page_preview": True,
        }
        if thread_id is not None:
            payload["message_thread_id"] = thread_id

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status == 200:
                        return True
                    # HTML rejected (400): resend as readable plain text, never raw HTML
                    if resp.status == 400:
                        payload.pop("parse_mode", None)
                        payload["text"] = _tg_plain_text(html_text)
                        async with session.post(
                            url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)
                        ) as retry_resp:
                            return retry_resp.status == 200
                    logger.warning(f"Telegram API status: {resp.status}")
                    return False
        except Exception as exc:
            logger.error(f"Failed to send Telegram message to {chat_id}: {exc}")
            return False

    async def send_telegram_photo(
        self,
        chat_id: str | int,
        photo_url: str,
        caption: str,
        thread_id: Optional[int] = None,
        bot_token: Optional[str] = None,
        enable_spoiler: bool = True,
        timeout: int = 30,
    ) -> bool:
        """Send a photo message to Telegram with spoiler support and caption.

        ``caption`` arrives as Markdown and is converted to HTML so ``**bold**``
        renders; Telegram's legacy Markdown parse_mode would show it literally.
        """
        token = bot_token or self.telegram_bot_token
        if not token:
            return False

        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        html_caption = _truncate_html(caption, MAX_TG_CAPTION)
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": html_caption,
            "parse_mode": "HTML",
            "has_spoiler": bool(enable_spoiler),
        }
        if thread_id is not None:
            payload["message_thread_id"] = thread_id

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status == 200:
                        return True
                    if resp.status == 400:
                        payload.pop("parse_mode", None)
                        payload["caption"] = _tg_plain_text(html_caption)
                        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as retry_resp:
                            if retry_resp.status == 200:
                                return True
        except Exception as exc:
            logger.debug(f"Direct photo URL send failed for {photo_url}: {exc}")

        # Fallback: Download image and upload multipart
        # The Referer header matters: bilibili blocks hotlinked images without it.
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.bilibili.com/",
            }
            async with aiohttp.ClientSession(headers=headers) as dl_session:
                async with dl_session.get(photo_url, timeout=aiohttp.ClientTimeout(total=15)) as dl_resp:
                    if dl_resp.status == 200:
                        image_data = await dl_resp.read()
                        if image_data:
                            form = aiohttp.FormData()
                            form.add_field("chat_id", str(chat_id))
                            if thread_id is not None:
                                form.add_field("message_thread_id", str(thread_id))
                            form.add_field("caption", html_caption)
                            form.add_field("parse_mode", "HTML")
                            if enable_spoiler:
                                form.add_field("has_spoiler", "true")
                            form.add_field("photo", image_data, filename="image.jpg", content_type="image/jpeg")
                            async with dl_session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=timeout)) as up_resp:
                                if up_resp.status == 200:
                                    return True
                                if up_resp.status == 400:
                                    form2 = aiohttp.FormData()
                                    form2.add_field("chat_id", str(chat_id))
                                    if thread_id is not None:
                                        form2.add_field("message_thread_id", str(thread_id))
                                    form2.add_field("caption", _tg_plain_text(html_caption))
                                    if enable_spoiler:
                                        form2.add_field("has_spoiler", "true")
                                    form2.add_field("photo", image_data, filename="image.jpg", content_type="image/jpeg")
                                    async with dl_session.post(url, data=form2, timeout=aiohttp.ClientTimeout(total=timeout)) as up2_resp:
                                        if up2_resp.status == 200:
                                            return True
        except Exception as exc:
            logger.warning(f"Multipart photo upload failed for {photo_url}: {exc}")

        return False

    async def send_telegram_media_group(
        self,
        chat_id: str | int,
        image_urls: List[str],
        thread_id: Optional[int] = None,
        bot_token: Optional[str] = None,
        enable_spoiler: bool = True,
        timeout: int = 30,
    ) -> bool:
        """Send 2-10 photos as one album. Telegram rejects albums smaller than 2 items.

        Uses a JSON body (not multipart), so it cannot send the ``Referer`` header
        that bilibili images require — callers must keep a per-photo fallback.
        """
        token = bot_token or self.telegram_bot_token
        if not token or len(image_urls) < 2:
            return False

        url = f"https://api.telegram.org/bot{token}/sendMediaGroup"

        def build_payload(spoiler: bool) -> Dict[str, Any]:
            media = [
                {"type": "photo", "media": u, **({"has_spoiler": True} if spoiler else {})}
                for u in image_urls[:10]
            ]
            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "media": json.dumps(media),
            }
            if thread_id is not None:
                payload["message_thread_id"] = thread_id
            return payload

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=build_payload(enable_spoiler),
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 200:
                        return True
                    # Older API versions / clients can reject has_spoiler on album items.
                    if resp.status == 400 and enable_spoiler:
                        async with session.post(
                            url, json=build_payload(False),
                            timeout=aiohttp.ClientTimeout(total=timeout),
                        ) as retry:
                            return retry.status == 200
                    logger.debug(f"Media group send failed with status {resp.status}")
                    return False
        except Exception as exc:
            logger.warning(f"Failed to send media group to {chat_id}: {exc}")
            return False


    def format_digest_caption(self, group_name: str, article_count: int = 0) -> str:
        """Short caption for a digest image: the card already carries the content."""
        suffix = f" · {article_count} 篇" if article_count else ""
        return f"📋 **【{group_name}】AI 资讯摘要**{suffix}"

    async def send_telegram_photo_file(
        self,
        chat_id: str | int,
        file_path: str,
        caption: str = "",
        thread_id: Optional[int] = None,
        bot_token: Optional[str] = None,
        enable_spoiler: bool = False,
        timeout: int = 90,
    ) -> bool:
        """Upload a locally rendered image as a photo.

        sendPhoto's URL form cannot carry a local file and sendDocument would show
        the card as an attachment, so this always goes through multipart upload.
        """
        token = bot_token or self.telegram_bot_token
        if not token:
            logger.warning("Cannot send Telegram photo: telegram_bot_token is not configured")
            return False

        try:
            with open(file_path, "rb") as handle:
                image_data = handle.read()
        except OSError as exc:
            logger.warning(f"Cannot read digest image {file_path}: {exc}")
            return False
        if not image_data:
            return False

        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        html_caption = _truncate_html(caption, MAX_TG_CAPTION) if caption else ""

        def build_form(with_parse_mode: bool) -> aiohttp.FormData:
            form = aiohttp.FormData()
            form.add_field("chat_id", str(chat_id))
            if thread_id is not None:
                form.add_field("message_thread_id", str(thread_id))
            if html_caption:
                form.add_field("caption", html_caption if with_parse_mode else _tg_plain_text(html_caption))
                if with_parse_mode:
                    form.add_field("parse_mode", "HTML")
            if enable_spoiler:
                form.add_field("has_spoiler", "true")
            form.add_field("photo", image_data, filename="digest.png", content_type="image/png")
            return form

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=build_form(True), timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status == 200:
                        return True
                    if resp.status == 400:
                        async with session.post(
                            url, data=build_form(False),
                            timeout=aiohttp.ClientTimeout(total=timeout),
                        ) as retry:
                            return retry.status == 200
                    logger.warning(f"Telegram photo upload failed with status {resp.status}")
        except Exception as exc:
            logger.error(f"Failed to upload digest photo to {chat_id}: {exc}")
        return False

    async def send_digest_images(
        self,
        subscriber: Subscriber,
        caption: str,
        image_paths: List[Any],
    ) -> bool:
        """Deliver rendered digest cards; False means the caller should send text."""
        umo = (subscriber.umo or "").strip()
        if not umo.startswith("telegram:") or subscriber.is_webhook():
            return False

        token = self.telegram_bot_token
        if not token:
            return False

        parts = umo.split(":")
        chat_id = parts[1]
        thread_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None

        sent = 0
        for index, path in enumerate(image_paths):
            ok = await self.send_telegram_photo_file(
                chat_id=chat_id,
                file_path=str(path),
                caption=caption if index == 0 else "",
                thread_id=thread_id,
                bot_token=token,
            )
            if ok:
                sent += 1
            else:
                logger.warning(f"Digest image {index + 1}/{len(image_paths)} failed to send")
        return sent > 0

    async def send_to_subscriber(
        self,
        subscriber: Subscriber,
        text: str,
        images: Optional[List[str]] = None,
        enable_spoiler: bool = True,
    ) -> bool:
        """Route message according to subscriber UMO."""
        umo = subscriber.umo.strip()
        if not umo:
            return False

        if subscriber.is_webhook():
            return await self.send_webhook(umo, text)

        # Telegram direct Bot API (supports telegram:chat_id or telegram:chat_id:thread_id)
        token = self.telegram_bot_token
        if umo.startswith("telegram:") and token:
            parts = umo.split(":")
            chat_id = parts[1]
            thread_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None

            # Duplicate URLs make Telegram reject the whole album.
            unique_images = list(dict.fromkeys(u for u in (images or []) if u))

            if len(unique_images) >= 2:
                ok = await self.send_telegram_media_group(
                    chat_id=chat_id,
                    image_urls=unique_images,
                    thread_id=thread_id,
                    bot_token=token,
                    enable_spoiler=enable_spoiler,
                )
                if ok:
                    # Album items cannot carry a shared caption, so the article text
                    # follows as its own message.
                    return await self.send_telegram(
                        chat_id=chat_id,
                        text=text,
                        bot_token=token,
                        thread_id=thread_id,
                    )
                # Album failed (e.g. bilibili blocks the JSON request's missing
                # Referer) — fall through to per-photo sends, which can set headers.
                logger.debug("Media group send failed; falling back to individual photos")

            if len(unique_images) >= 1:
                sent_any = False
                for idx, image_url in enumerate(unique_images):
                    # Only the first photo carries the article text; a caption on
                    # every album item would repeat it once per image.
                    ok = await self.send_telegram_photo(
                        chat_id=chat_id,
                        photo_url=image_url,
                        caption=text if idx == 0 else "",
                        thread_id=thread_id,
                        bot_token=token,
                        enable_spoiler=enable_spoiler,
                    )
                    sent_any = sent_any or ok
                if sent_any:
                    return True
                # If sending photo failed, fall back to text message below

            return await self.send_telegram(
                chat_id=chat_id,
                text=text,
                bot_token=token,
                thread_id=thread_id,
            )

        # Fallback to custom platform sender if provided
        if self.platform_sender:
            try:
                res = await self.platform_sender(umo, text)
                return bool(res)
            except Exception as exc:
                logger.error(f"Custom platform sender failed for UMO {umo}: {exc}")

        # Hermes CLI dispatch (native platform support for telegram:chat:thread, weixin, etc.)
        try:
            cmd = ["hermes", "send", "--to", umo, "--no-preview"]
            if enable_spoiler:
                cmd.append("--spoiler")
            cmd.append(text)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            if proc.returncode == 0:
                return True
            logger.warning(f"hermes send failed for {umo}: {stderr.decode(errors='ignore')}")
            return False
        except Exception as exc:
            logger.error(f"Failed to dispatch via hermes send to {umo}: {exc}")
            return False

    async def send_article(
        self,
        subscriber: Subscriber,
        article: RSSArticle,
        sub_name: str,
        personal_config: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Format and deliver an RSS article to a subscriber with photo and spoiler support."""
        cfg = personal_config or subscriber.personal_config or {}
        enable_spoiler = bool(cfg.get("enable_spoiler", True))

        images = []
        if not cfg.get("only_title", False) and article.image_urls:
            max_img = int(cfg.get("max_image_number", 0) or 0)
            if max_img > 0:
                images = article.image_urls[:max_img]
            else:
                images = article.image_urls[:3]

        is_telegram = subscriber.umo.strip().startswith("telegram:")
        text = self.format_article(
            article,
            sub_name,
            cfg,
            for_native_media=bool(is_telegram and images),
        )

        return await self.send_to_subscriber(
            subscriber=subscriber,
            text=text,
            images=images if is_telegram else None,
            enable_spoiler=enable_spoiler,
        )
