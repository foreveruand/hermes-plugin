"""HTML rendering for AIRSS digests.

The AI digest is Markdown; sending it as text on a phone screen with Telegram's
restricted HTML subset looks cramped. Instead we render it as a card and
screenshot it with headless Chromium, so the reader gets typography instead of
asterisks.

Rendering is dependency-light on purpose: Chromium is taken from the Playwright
cache (or PATH) and the only Python extras are Pillow/markdown, both optional
with fallbacks.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import math
import os
import re
import shutil
from pathlib import Path
from typing import List, Optional, Sequence

logger = logging.getLogger("airss")

BG_COLOR = "#eef1f6"
BG_RGB = (238, 241, 246)

# Chromium refuses to render surfaces taller than 16384 device pixels, so with
# scale=2 a page may never exceed ~8000 CSS pixels.
MAX_WINDOW_CSS_HEIGHT = 8000
# Telegram rejects photos whose width+height exceeds 10000px, and tall cards are
# unreadable on a phone anyway, so a page is capped well below that.
MAX_PAGE_CSS_HEIGHT = 2800
PAGE_PAD_X = 40
CARD_PAD_X = 38
CONTENT_FONT_PX = 17
LINE_HEIGHT_PX = 30.0
LINE_BUDGET_UNITS = 40.0  # conservative: CJK chars per line at 900px width

CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: %(bg)s; }
body {
  width: %(width)dpx;
  color: #1b2330;
  font-family: "Noto Sans CJK SC", "Noto Sans SC", "PingFang SC",
               "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.page { width: %(width)dpx; padding: 34px %(pad)dpx 26px; background: %(bg)s; }
.card {
  background: #ffffff;
  border-radius: 22px;
  overflow: hidden;
  box-shadow: 0 12px 30px rgba(23, 40, 77, 0.09);
}
.head {
  padding: 30px %(cardpad)dpx 26px;
  background: linear-gradient(135deg, #2f6df6 0%%, #6a4bf0 100%%);
  color: #ffffff;
}
.kicker {
  font-size: 13px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  opacity: 0.82;
}
.head h1 {
  font-size: 31px;
  line-height: 1.3;
  margin: 12px 0 14px;
  font-weight: 700;
  word-break: break-word;
}
.meta { font-size: 14px; opacity: 0.9; display: flex; gap: 16px; flex-wrap: wrap; }
.body { padding: 28px %(cardpad)dpx 32px; font-size: %(font)dpx; line-height: 1.78; }
.body > *:first-child { margin-top: 0; }
.body h1 { font-size: 25px; margin: 0 0 16px; line-height: 1.4; }
.body h2 {
  font-size: 21px;
  margin: 30px 0 14px;
  padding-left: 12px;
  line-height: 1.45;
  border-left: 4px solid #2f6df6;
  color: #14203a;
}
.body h3 { font-size: 18px; margin: 22px 0 10px; color: #1d2b47; }
.body h4, .body h5, .body h6 { font-size: 17px; margin: 18px 0 8px; }
.body p { margin: 0 0 13px; }
.body ul, .body ol { margin: 0 0 16px; padding-left: 1.45em; }
.body li { margin: 6px 0; }
.body li > p { margin: 0; }
.body a { color: #2f6df6; text-decoration: none; word-break: break-all; }
.body strong { color: #0f1a2c; font-weight: 700; }
.body em { font-style: italic; }
.body blockquote {
  margin: 0 0 16px;
  padding: 4px 0 4px 16px;
  border-left: 3px solid #ccd7e8;
  color: #5c6980;
}
.body code {
  background: #f1f5fb;
  padding: 2px 6px;
  border-radius: 6px;
  font-size: 0.9em;
  font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
  color: #2b3a55;
}
.body pre {
  background: #0f172a;
  color: #e6edf7;
  padding: 16px 18px;
  border-radius: 12px;
  font-size: 13px;
  line-height: 1.6;
  overflow: hidden;
  white-space: pre-wrap;
  word-break: break-word;
}
.body pre code { background: none; color: inherit; padding: 0; }
.body hr { border: none; border-top: 1px solid #e4e9f2; margin: 24px 0; }
.body table { border-collapse: collapse; width: 100%%; margin: 0 0 16px; font-size: 15px; }
.body th, .body td { border: 1px solid #e4e9f2; padding: 8px 10px; text-align: left; }
.body th { background: #f5f8fd; }
.foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px %(cardpad)dpx 20px;
  border-top: 1px solid #eef1f7;
  font-size: 12.5px;
  color: #7b8699;
}
"""


def _escape(text: str) -> str:
    return html_lib.escape(text or "", quote=False)


def _strip_dangerous(html_text: str) -> str:
    """Remove script/style blocks and inline event handlers from LLM HTML."""
    html_text = re.sub(r"<script\b.*?</script>", "", html_text, flags=re.S | re.I)
    html_text = re.sub(r"<style\b.*?</style>", "", html_text, flags=re.S | re.I)
    html_text = re.sub(r"\son\w+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html_text, flags=re.I)
    return html_text


def _fallback_markdown(md: str) -> str:
    """Minimal Markdown renderer used when the markdown package is absent."""
    out: List[str] = []
    lines = md.splitlines()
    i = 0
    list_tag: Optional[str] = None

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def inline(text: str) -> str:
        text = _escape(text)
        text = re.sub(r"\x60([^\x60]+)\x60", r"<code>\1</code>", text)
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
        text = re.sub(
            r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text
        )
        return text

    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            close_list()
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            close_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if re.match(r"^\s*([-*_])\s*(\1\s*){2,}$", line):
            close_list()
            out.append("<hr>")
            i += 1
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if bullet or numbered:
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                close_list()
                out.append(f"<{tag}>")
                list_tag = tag
            out.append(f"<li>{inline((bullet or numbered).group(1))}</li>")
            i += 1
            continue
        if line.lstrip().startswith(">"):
            close_list()
            quote: List[str] = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(lines[i].lstrip()[1:].strip())
                i += 1
            out.append(f"<blockquote><p>{inline(' '.join(quote))}</p></blockquote>")
            continue
        close_list()
        para: List[str] = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
            r"^(#{1,6}\s|\s*[-*+]\s|\s*\d+[.)]\s|>)", lines[i]
        ):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{inline(' '.join(para))}</p>")
    close_list()
    return "\n".join(out)


def markdown_to_html(md: str) -> str:
    """Convert digest Markdown to HTML, escaping anything the model did not mark up."""
    if not md:
        return ""
    try:
        import markdown as markdown_lib

        rendered = markdown_lib.markdown(
            md,
            extensions=["extra", "sane_lists", "nl2br"],
            output_format="html5",
        )
    except Exception:
        rendered = _fallback_markdown(md)
    return _strip_dangerous(rendered)


def _visual_len(text: str) -> float:
    """Rough display width in CJK-character units."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text or "")
    text = re.sub(r"[*_\x60#>]", "", text)
    units = 0.0
    for ch in text:
        units += 1.0 if ord(ch) > 0x2E7F else 0.55
    return units


def _lines_for(text: str, budget: float = LINE_BUDGET_UNITS) -> int:
    return max(1, math.ceil(_visual_len(text) / budget))


_HEADING_HEIGHT = {1: 56.0, 2: 76.0, 3: 54.0, 4: 46.0}
_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _block_height(block: str) -> float:
    """Estimate rendered height of a block.

    A block is whatever sits between blank lines, so it may mix a heading, a
    paragraph and a list; each run is measured separately instead of assuming
    the first line speaks for the whole block.
    """
    stripped = block.strip()
    if not stripped:
        return 0.0
    if re.match(r"^\s*([-*_])\s*(\1\s*){2,}$", stripped):
        return 26.0

    total = 0.0
    lines = stripped.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+", line)
        if heading:
            total += _HEADING_HEIGHT.get(len(heading.group(1)), 42.0)
            i += 1
            continue
        if _ITEM_RE.match(line):
            run = 0.0
            while i < len(lines) and _ITEM_RE.match(lines[i]):
                item = _ITEM_RE.sub("", lines[i])
                run += _lines_for(item) * LINE_HEIGHT_PX + 12
                i += 1
            total += run + 10
            continue
        if line.lstrip().startswith(">"):
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                i += 1
            total += LINE_HEIGHT_PX + 16
            continue
        para: List[str] = []
        while (
            i < len(lines)
            and lines[i].strip()
            and not re.match(r"^(#{1,6}\s|\s*(?:[-*+]|\d+[.)])\s|>)", lines[i])
        ):
            para.append(lines[i].strip())
            i += 1
        total += _lines_for(" ".join(para)) * LINE_HEIGHT_PX + 13
    return total or 13.0


def _split_blocks(md: str) -> List[str]:
    blocks = [b for b in re.split(r"\n\s*\n", md.strip()) if b.strip()]
    return blocks or [md]


def _pack_pages(blocks: Sequence[str], body_budget: float) -> List[List[str]]:
    pages: List[List[str]] = []
    current: List[str] = []
    used = 0.0
    for block in blocks:
        height = _block_height(block)
        if current and used + height > body_budget:
            pages.append(current)
            current, used = [], 0.0
        current.append(block)
        used += height
    if current:
        pages.append(current)
    return pages or [[]]


def build_digest_html(
    markdown_text: str,
    *,
    group_name: str,
    subtitle: str = "",
    footer_left: str = "Hermes AIRSS · 智能订阅",
    page_label: str = "",
    width: int = 900,
) -> str:
    """Wrap digest Markdown in a styled card document."""
    body = markdown_to_html(markdown_text)
    title_block = ""
    # The model is asked to start with an H1; lift it into the card header so the
    # body starts directly with sections instead of repeating the title.
    match = re.match(r"\s*<h1>(.*?)</h1>\s*", body, flags=re.S)
    if match:
        title_block = match.group(1).strip()
        body = body[match.end():]
    title_html = title_block or _escape(group_name)

    css = CSS % {
        "bg": BG_COLOR,
        "width": width,
        "pad": PAGE_PAD_X,
        "cardpad": CARD_PAD_X,
        "font": CONTENT_FONT_PX,
    }
    meta_bits = [f"<span>{_escape(subtitle)}</span>"] if subtitle else []
    meta_html = f'<div class="meta">{"".join(meta_bits)}</div>' if meta_bits else ""
    page_html = f"<span>{_escape(page_label)}</span>" if page_label else "<span></span>"

    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{_escape(group_name)}</title><style>{css}</style></head>
<body>
  <div class="page">
    <div class="card">
      <div class="head">
        <div class="kicker">AI Digest</div>
        <h1>{title_html}</h1>
        {meta_html}
      </div>
      <div class="body">{body}</div>
      <div class="foot">
        <span>{_escape(footer_left)}</span>
        {page_html}
      </div>
    </div>
  </div>
</body>
</html>"""


def _find_chrome() -> Optional[str]:
    """Locate a Chromium binary: explicit env var, Playwright cache, then PATH."""
    explicit = os.environ.get("AIRSS_CHROME_PATH", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit

    cache = Path.home() / ".cache" / "ms-playwright"
    if cache.is_dir():
        for candidate in sorted(cache.glob("chromium-*/chrome-linux*/chrome"), reverse=True):
            if candidate.is_file():
                return str(candidate)

    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _trim_bottom(image, background=BG_RGB, tolerance: int = 8, margin: int = 24):
    """Crop the uniform page background Chromium painted below the card."""
    width, height = image.size
    pixels = image.load()
    step = max(1, width // 64)

    def row_is_background(y: int) -> bool:
        for x in range(0, width, step):
            r, g, b = pixels[x, y][:3]
            if (
                abs(r - background[0]) > tolerance
                or abs(g - background[1]) > tolerance
                or abs(b - background[2]) > tolerance
            ):
                return False
        return True

    bottom = height - 1
    while bottom > 0 and row_is_background(bottom):
        bottom -= 1
    return image.crop((0, 0, width, min(height, bottom + 1 + margin)))


async def _screenshot(
    chrome: str, html_path: Path, png_path: Path, width: int, height: int, scale: int
) -> bool:
    # No --user-data-dir on purpose: a fresh profile in this environment makes
    # Chromium attempt GCM registration and never exit.
    args = [
        chrome,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--disable-extensions",
        "--no-first-run",
        "--no-default-browser-check",
        f"--force-device-scale-factor={scale}",
        f"--window-size={width},{height}",
        f"--screenshot={png_path}",
        html_path.as_uri(),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=90)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("Chromium screenshot timed out after 90s")
        return False
    return png_path.is_file()


def _estimate_height(md: str, title_chars: int = 24) -> float:
    header = 120 + _lines_for("#" * title_chars) * 41 + 40
    footer = 74
    body = sum(_block_height(b) for b in _split_blocks(md))
    padding = 34 + 26
    return header + footer + body + padding


async def render_digest_images(
    markdown_text: str,
    out_dir: Path,
    *,
    group_name: str,
    subtitle: str = "",
    width: int = 900,
    scale: int = 2,
    footer_left: str = "Hermes AIRSS · 智能订阅",
) -> List[Path]:
    """Render the digest as one or more PNG cards. Returns [] when Chromium is missing."""
    chrome = _find_chrome()
    if not chrome:
        logger.warning("Chromium not found; falling back to text digest delivery")
        return []
    if not markdown_text.strip():
        return []

    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Height estimates deliberately run ~15-20% rich, so divide the page target
    # before packing to keep the real card inside MAX_PAGE_CSS_HEIGHT.
    body_budget = max(600.0, (MAX_PAGE_CSS_HEIGHT - 200) / 1.15)
    pages = _pack_pages(_split_blocks(markdown_text), body_budget)

    paths: List[Path] = []
    total = len(pages)
    for index, blocks in enumerate(pages, 1):
        page_md = "\n\n".join(blocks)
        label = f"{index} / {total}" if total > 1 else ""
        doc = build_digest_html(
            page_md,
            group_name=group_name,
            subtitle=subtitle,
            footer_left=footer_left,
            page_label=label,
            width=width,
        )
        html_path = out_dir / f"digest-{index}.html"
        html_path.write_text(doc, encoding="utf-8")

        estimate = _estimate_height(page_md)
        window_height = int(min(MAX_WINDOW_CSS_HEIGHT, max(420.0, estimate * 1.15 + 120)))
        png_path = out_dir / f"digest-{index}.png"

        if not await _screenshot(chrome, html_path, png_path, width, window_height, scale):
            logger.warning("Chromium failed to render digest page %s", index)
            # All pages or none: a half-rendered card set would lose content while
            # still marking the articles as delivered.
            for rendered in paths:
                rendered.unlink(missing_ok=True)
            return []

        try:
            from PIL import Image
        except Exception:
            logger.warning("Pillow not available; sending uncropped screenshots")
            paths.append(png_path)
            continue

        def _finalize() -> int:
            with Image.open(png_path) as image:
                img = _trim_bottom(image.convert("RGB"), BG_RGB)
            img = _fit_telegram(img)
            img.save(png_path, format="PNG", optimize=True)
            return img.size[1]

        try:
            trimmed_height = await asyncio.to_thread(_finalize)
            # Content reached the bottom edge: the window was too short, so retry
            # once with generous headroom rather than shipping a clipped card.
            if (
                trimmed_height >= window_height * scale - 6
                and window_height < MAX_WINDOW_CSS_HEIGHT
            ):
                taller = int(min(MAX_WINDOW_CSS_HEIGHT, window_height * 1.5))
                if taller > window_height and await _screenshot(
                    chrome, html_path, png_path, width, taller, scale
                ):
                    await asyncio.to_thread(_finalize)
        except Exception as exc:
            logger.warning("Failed to finalize digest image: %s", exc)
            for rendered in paths:
                rendered.unlink(missing_ok=True)
            return []

        paths.append(png_path)

    return paths


def _fit_telegram(image, max_sum: int = 9800):
    """Keep the card inside Telegram's photo limits (width + height <= 10000)."""
    from PIL import Image

    width, height = image.size
    if width + height <= max_sum:
        return image
    factor = max_sum / float(width + height)
    return image.resize(
        (max(1, int(width * factor)), max(1, int(height * factor))),
        Image.LANCZOS,
    )
