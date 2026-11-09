"""Core download engine for the Hermes Media Downloader plugin.

Combines yt-dlp and gallery-dl workflows to download videos, audio, and images from
Bilibili, YouTube, X/Twitter, Pixiv and other supported sites.

Downloads are staged under a scratch directory and only archived once complete. The
archive backend is chosen by ``upload_mode``: ``local`` moves files into a local
directory, ``rclone`` transfers them to a remote spec (``remote:path``), and ``bdpan``
packs them into an encrypted 7z archive and uploads it to Baidu Netdisk.

The plugin never pushes media files to the user: callers receive the archived paths,
and any path rendered into reply text must stay inside a code span so the gateway
does not re-attach it as a file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

try:
    from .config import DEFAULT_CONFIG
except ImportError:
    from config import DEFAULT_CONFIG

logger = logging.getLogger("media-downloader")

_LOG_TAIL_LINES = 40

# DLSite work numbers: "RJ123456", "rj123456", or a bare numeric id.
_RJ_RE = re.compile(r"^(?:RJ)?(\d{4,8})$", re.IGNORECASE)

# rclone --stats-one-line progress, e.g.
# "29.877 MiB / 365.731 MiB, 8%, 9.769 KiB/s, ETA 9h46m46s (xfr#4/13)".
_RCLONE_PROGRESS_RE = re.compile(
    r"[\d.]+\s*[KMGTP]?i?B\s*/\s*[\d.]+\s*[KMGTP]?i?B,\s*\d+%"
    r"(?:,\s*[\d.]+\s*[KMGTP]?i?B/s)?"
    r"(?:,\s*ETA\s*[\dhms.]+)?"
    r"(?:\s*\(xfr#\d+/\d+\))?"
)
# Per-file "-P" progress, e.g. "* track01.flac:  8% /35.731MiB, 9.808KiB/s, 9h44m".
_RCLONE_FILE_PROGRESS_RE = re.compile(r"^\*\s*\S.*?:\s*\d+%\s*/")
# Final summary counts emitted by rclone (distinct from the byte/byte progress line).
_RCLONE_COUNT_RE = re.compile(r"Transferred:\s+(\d+)\s*/\s*(\d+)")
_RCLONE_ERRORS_RE = re.compile(r"Errors:\s+(\d+)")


def _normalize_rclone_progress(segment: str) -> str:
    """Collapse one rclone stats line into a compact "pct · speed · ETA" string."""
    m = _RCLONE_PROGRESS_RE.search(segment)
    if not m:
        if not _RCLONE_FILE_PROGRESS_RE.match(segment):
            return ""
        pct = re.search(r"(\d+)%", segment)
        speed = re.search(r",\s*([\d.]+\s*[KMGTP]?i?B/s)", segment)
        parts = [f"{pct.group(1)}%" if pct else ""]
        if speed:
            parts.append(speed.group(1))
        return " · ".join(p for p in parts if p)
    text = m.group(0)
    pct = re.search(r"(\d+)%", text)
    speed = re.search(r",\s*([\d.]+\s*[KMGTP]?i?B/s)", text)
    eta = re.search(r"ETA\s*([\dhms.]+)", text)
    xfr = re.search(r"\(xfr#(\d+/\d+)\)", text)
    parts: list[str] = []
    if pct:
        parts.append(f"{pct.group(1)}%")
    if speed:
        parts.append(speed.group(1))
    if eta:
        parts.append(f"ETA {eta.group(1)}")
    if xfr:
        parts.append(xfr.group(1))
    return " · ".join(parts)


def _cap_lines(text: str, limit: int) -> str:
    """Keep at most ``limit`` non-empty lines, noting how many were dropped."""
    lines = [ln for ln in str(text).splitlines() if ln.strip()]
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join(lines[:limit]) + f"\n... 另有 {len(lines) - limit} 行"


def find_binary(name: str, cfg: dict[str, Any]) -> str:
    """Find an executable via PATH or the configured fallback locations."""
    found = shutil.which(name)
    if found:
        return found
    for base in cfg.get("bin_fallback_paths", DEFAULT_CONFIG["bin_fallback_paths"]):
        p = Path(base) / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return name


def needs_proxy(url: str, cfg: dict[str, Any], force_proxy: bool = False, no_proxy: bool = False) -> bool:
    """Decide whether a URL must be routed through the proxy."""
    if no_proxy:
        return False
    if force_proxy:
        return True
    try:
        domain = urlparse(url).netloc.lower()
    except Exception:
        return True
    for d in cfg.get("domestic_domains", DEFAULT_CONFIG["domestic_domains"]):
        if d in domain:
            return False
    return True


def get_subprocess_env(proxy: Optional[str]) -> dict[str, str]:
    """Prepare a clean subprocess environment preventing proxy leakage on direct traffic."""
    env = os.environ.copy()
    proxy_vars = ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"]
    if proxy:
        for v in proxy_vars:
            env[v] = proxy
    else:
        for v in proxy_vars:
            env.pop(v, None)
    return env


def classify_destination(url: str, cfg: dict[str, Any], category: str = "auto", author: str = "",
                         out_dir: str = "") -> tuple[str, str]:
    """Resolve the archive destination and category label for a URL.

    The destination may be a local path or an rclone remote spec; both are returned as
    plain strings so callers never coerce a remote spec into a filesystem path.
    """
    if out_dir:
        return out_dir.strip(), "custom"

    destinations = cfg.get("destinations", DEFAULT_CONFIG["destinations"])
    domain = urlparse(url).netloc.lower()

    if category == "asmr":
        return destinations["asmr"], "asmr"
    if "bilibili.com" in domain or "b23.tv" in domain or "douyin.com" in domain:
        return destinations["bilibili"], "bilibili"
    if "pixiv.net" in domain or "x.com" in domain or "twitter.com" in domain or "weibo.com" in domain or "weibo.cn" in domain or category == "image":
        root = destinations["pixiv"].rstrip("/")
        return (f"{root}/{author}", "pixiv") if author else (root, "pixiv")
    return destinations["extract"], "extract"


def _tail(text: str, limit: int = _LOG_TAIL_LINES) -> str:
    lines = [ln for ln in str(text).splitlines() if ln.strip()]
    return "\n".join(lines[-limit:])


def run_yt_dlp(url: str, output_dir: Path, cfg: dict[str, Any], proxy: Optional[str] = None,
               extra_args: Optional[list[str]] = None) -> tuple[bool, str]:
    """Run yt-dlp against a URL, staging output under output_dir."""
    yt_dlp_bin = find_binary("yt-dlp", cfg)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(output_dir / "%(uploader)s/%(title).40s/%(title).100s.%(ext)s")

    cmd = [
        yt_dlp_bin,
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        "--socket-timeout", "60",
        "--retries", "10",
        "--extractor-retries", "5",
        "--fragment-retries", "20",
        "--continue",
        "--print", "after_move:filepath",
        "--newline",
        "--progress",
        "--embed-thumbnail",
        "--no-mtime",
        "-i",
        "--add-metadata",
        "-o", out_tmpl,
    ]

    domain = urlparse(url).netloc.lower()
    chromium_profile_raw = str(cfg.get("chromium_profile_path", "")).strip()
    chromium_profile = Path(chromium_profile_raw) if chromium_profile_raw else None
    bilibili_cookies = Path(cfg.get("cookies_dir", DEFAULT_CONFIG["cookies_dir"])) / "bilibili.txt"

    if chromium_profile is not None and chromium_profile.exists():
        browser_spec = str(cfg.get("chromium_cookie_browser") or "").strip() or f"chromium:{chromium_profile_raw}"
        cmd.extend(["--cookies-from-browser", browser_spec])
    elif ("bilibili.com" in domain or "b23.tv" in domain) and bilibili_cookies.exists():
        cmd.extend(["--cookies", str(bilibili_cookies)])

    archive_file = cfg.get("yt_archive_file")
    if archive_file:
        Path(archive_file).parent.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--download-archive", str(archive_file)])

    cmd.extend(["--proxy", proxy or ""])

    if extra_args:
        cmd.extend(extra_args)
    cmd.append(url)

    res = subprocess.run(cmd, env=get_subprocess_env(proxy), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors="replace")
    return res.returncode == 0, _tail(res.stdout)


def run_gallery_dl(url: str, output_dir: Path, cfg: dict[str, Any], proxy: Optional[str] = None,
                   extra_args: Optional[list[str]] = None) -> tuple[bool, str]:
    """Run gallery-dl against a URL, staging output under output_dir."""
    gdl_bin = find_binary("gallery-dl", cfg)
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [gdl_bin, "--directory", str(output_dir), "--no-colors", "--no-input"]

    gdl_config = Path(cfg.get("gdl_config_file", DEFAULT_CONFIG["gdl_config_file"]))
    if gdl_config.exists():
        cmd.extend(["-c", str(gdl_config)])

    domain = urlparse(url).netloc.lower()
    twitter_cookies = Path(cfg.get("cookies_dir", DEFAULT_CONFIG["cookies_dir"])) / "x.txt"
    if ("twitter.com" in domain or "x.com" in domain) and twitter_cookies.exists():
        cmd.extend(["-C", str(twitter_cookies)])

    archive_file = cfg.get("gdl_archive_file")
    if archive_file:
        Path(archive_file).parent.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--download-archive", str(archive_file)])

    cmd.extend(["--proxy", proxy or ""])

    if extra_args:
        cmd.extend(extra_args)
    cmd.append(url)

    res = subprocess.run(cmd, env=get_subprocess_env(proxy), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors="replace")
    return res.returncode == 0, _tail(res.stdout)


def resolve_staging_dir(cfg: dict[str, Any]) -> Path:
    """Pick the staging root, falling back to scratch cache when the mount is unavailable."""
    base = Path(cfg.get("staging_base", DEFAULT_CONFIG["staging_base"]))
    if not base.parent.exists():
        base = Path(cfg.get("staging_fallback", DEFAULT_CONFIG["staging_fallback"]))
    staging_dir = base / str(uuid.uuid4())[:8]
    staging_dir.mkdir(parents=True, exist_ok=True)
    return staging_dir


def _staged_files(staging_dir: Path) -> list[Path]:
    """List completed staged files, skipping partial downloads and placeholders."""
    found: list[Path] = []
    for root, _, files in os.walk(staging_dir):
        for f in files:
            src = Path(root) / f
            if src.stat().st_size == 0 or src.suffix.lower() in (".part", ".ytdl"):
                continue
            found.append(src)
    return found


def safe_stage_and_move(tmp_dir: Path, dest_dir: Path) -> list[Path]:
    """Move staged files into a local archive, preserving relative directory hierarchy."""
    moved: list[Path] = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in _staged_files(tmp_dir):
        dest = dest_dir / src.relative_to(tmp_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            continue
        shutil.move(str(src), str(dest))
        moved.append(dest)
    return moved


def rclone_transfer(staging_dir: Path, destination: str, cfg: dict[str, Any],
                    progress_cb: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """Transfer staged files to an rclone remote, streaming a normalized progress line.

    Returns ``{"ok", "moved", "total", "errors", "progress", "log"}`` where ``log`` holds only
    the non-progress output (i.e. genuine rclone error lines). ``progress_cb`` receives the
    latest one-line ``"pct · speed · ETA"`` string as the transfer advances.
    """
    rc = dict(DEFAULT_CONFIG["rclone"])
    rc.update(cfg.get("rclone") or {})

    rclone_bin = find_binary(str(rc.get("bin", "rclone")), cfg)
    target = destination.rstrip("/") + "/"

    cmd = [
        rclone_bin,
        str(rc.get("mode", "move")),
        str(staging_dir),
        target,
        "-P",
        "--stats-one-line",
        f"--stats={rc.get('stats_interval', '2s')}",
    ]
    for pattern in rc.get("exclude", []) or []:
        cmd.extend(["--exclude", str(pattern)])
    for flag in rc.get("flags", []) or []:
        cmd.append(str(flag))

    timeout = rc.get("timeout_seconds", 7200)
    result: dict[str, Any] = {"ok": False, "moved": None, "total": None, "errors": None,
                              "progress": "", "log": ""}
    other_lines: list[str] = []

    def _handle(segment: str) -> None:
        segment = segment.strip()
        if not segment:
            return
        normalized = _normalize_rclone_progress(segment)
        if normalized:
            result["progress"] = normalized
            if progress_cb:
                try:
                    progress_cb(normalized)
                except Exception:  # a callback failure must never abort the transfer
                    logger.debug("rclone progress callback failed", exc_info=True)
            return
        counts = _RCLONE_COUNT_RE.search(segment)
        if counts:
            result["moved"], result["total"] = int(counts.group(1)), int(counts.group(2))
            return
        errors = _RCLONE_ERRORS_RE.search(segment)
        if errors:
            result["errors"] = int(errors.group(1))
            return
        other_lines.append(segment)

    proc = subprocess.Popen(cmd, env=get_subprocess_env(None), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    stdout = proc.stdout
    assert stdout is not None  # PIPE guarantees a stream

    def _reader() -> None:
        buf = ""
        while True:
            try:
                chunk = os.read(stdout.fileno(), 4096)
            except OSError:
                break
            if not chunk:
                break
            # rclone separates live progress with "\r" and messages with "\n"; split on both.
            buf += chunk.decode("utf-8", "replace")
            parts = re.split(r"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                _handle(part)
        if buf:
            _handle(buf)

    reader = threading.Thread(target=_reader, daemon=True, name="rclone-reader")
    reader.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    reader.join(timeout=5)

    result["log"] = _tail("\n".join(other_lines))
    if timed_out:
        note = f"rclone 传输超时（>{timeout}s）"
        result["log"] = f"{result['log']}\n{note}" if result["log"] else note
    result["ok"] = proc.returncode == 0 and not timed_out
    return result


def archive_staged_files(staging_dir: Path, destination: str, cfg: dict[str, Any],
                         progress_cb: Optional[Callable[[str], None]] = None,
                         internal_prefix: tuple[str, ...] = (), slug_hint: str = ""
                         ) -> tuple[list[str], str, dict[str, Any]]:
    """Archive staged files according to ``cfg['upload_mode']``.

    ``local`` moves files into a local directory, ``rclone`` transfers them to a remote
    spec, and ``bdpan`` packs them into an encrypted 7z and uploads it to Baidu Netdisk.
    Returns the archived path list, a log line, and a transfer summary
    (``{"engine", "moved", "failed", "total", ...}``).
    """
    mode = str(cfg.get("upload_mode", DEFAULT_CONFIG.get("upload_mode", "local"))).strip().lower()

    if mode == "bdpan":
        try:
            from .bdpan import bdpan_archive_transfer
            from .handoff import attach_manifest
        except ImportError:
            from bdpan import bdpan_archive_transfer
            from handoff import attach_manifest
        files, log, transfer = bdpan_archive_transfer(
            staging_dir, slug_hint or destination, cfg,
            internal_prefix=internal_prefix, progress_cb=progress_cb)
        # The receiver needs the category + relative prefix, never a host path.
        attach_manifest(transfer, destination, cfg, internal_prefix=internal_prefix,
                        title=slug_hint)
        return files, log, transfer

    if mode == "rclone":
        staged = _staged_files(staging_dir)
        res = rclone_transfer(staging_dir, destination, cfg, progress_cb=progress_cb)
        if not res["ok"]:
            # Never lose a completed download: park it locally and surface the path.
            fallback_root = Path(cfg.get("staging_fallback", DEFAULT_CONFIG["staging_fallback"]))
            preserved = safe_stage_and_move(staging_dir, fallback_root / staging_dir.name)
            failed = res["errors"] if res["errors"] is not None else len(preserved)
            total = res["total"] if res["total"] is not None else (len(staged) or len(preserved))
            moved = res["moved"] if res["moved"] is not None else max(0, total - failed)
            reason = _cap_lines(res["log"], 8) or "(无详细错误输出)"
            raise RuntimeError(
                f"rclone 传输失败（成功 {moved}/{total}，失败 {failed}）：\n{reason}\n"
                f"已暂存到本地 {fallback_root / staging_dir.name} ({len(preserved)} 个文件)"
            )
        root = destination.rstrip("/")
        files = [f"{root}/{p.relative_to(staging_dir).as_posix()}" for p in staged]
        moved = res["moved"] if res["moved"] is not None else len(staged)
        total = res["total"] if res["total"] is not None else len(staged)
        failed = res["errors"] if res["errors"] is not None else 0
        transfer = {"engine": "rclone", "moved": moved, "failed": failed, "total": total}
        return files, f"Transferred {moved}/{total}", transfer

    moved = safe_stage_and_move(staging_dir, Path(destination))
    transfer = {"engine": "local", "moved": len(moved), "failed": 0, "total": len(moved)}
    return [str(p) for p in moved], "", transfer


def download_media(url: str, cfg: dict[str, Any], category: str = "auto", author: str = "",
                   out_dir: str = "", proxy: str = "", force_proxy: bool = False,
                   no_proxy: bool = False, extra_args: Optional[list[str]] = None,
                   progress_cb: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """Download a URL, stage it, and archive the result.

    Returns a result dict with the archived file list, destination category and a log tail.
    """
    url = url.strip()
    if not proxy and needs_proxy(url, cfg, force_proxy=force_proxy, no_proxy=no_proxy):
        proxy = cfg.get("default_proxy", DEFAULT_CONFIG["default_proxy"])

    destination, category_label = classify_destination(url, cfg, category=category,
                                                       author=author, out_dir=out_dir)
    staging_dir = resolve_staging_dir(cfg)
    domain = urlparse(url).netloc.lower()
    upload_mode = str(cfg.get("upload_mode", DEFAULT_CONFIG.get("upload_mode", "local"))).strip().lower()

    log_parts = [
        f"URL: {url}",
        f"Proxy: {proxy or 'None (Direct)'}",
        f"Staging: {staging_dir}",
        f"Destination: {destination} ({upload_mode})",
    ]

    is_social_image = (
        ("pixiv.net" in domain)
        or ("weibo.com" in domain or "weibo.cn" in domain)
        or (("x.com" in domain or "twitter.com" in domain) and category == "image")
        or (category == "image")
    )

    try:
        if is_social_image or (("x.com" in domain or "twitter.com" in domain) and category != "video"):
            log_parts.append("Strategy: gallery-dl (social images)")
            success, log = run_gallery_dl(url, staging_dir, cfg, proxy=proxy, extra_args=extra_args)
            log_parts.append(log)
            if not success:
                log_parts.append("gallery-dl failed, falling back to yt-dlp")
                success, log = run_yt_dlp(url, staging_dir, cfg, proxy=proxy, extra_args=extra_args)
                log_parts.append(log)
        else:
            log_parts.append("Strategy: yt-dlp (video/audio)")
            success, log = run_yt_dlp(url, staging_dir, cfg, proxy=proxy, extra_args=extra_args)
            log_parts.append(log)
            if not success:
                log_parts.append("yt-dlp failed, falling back to gallery-dl")
                success, log = run_gallery_dl(url, staging_dir, cfg, proxy=proxy, extra_args=extra_args)
                log_parts.append(log)

        archived, archive_log, transfer = archive_staged_files(
            staging_dir, destination, cfg, progress_cb=progress_cb,
            slug_hint=author or category_label)
        if archive_log:
            log_parts.append(archive_log)
        if transfer.get("engine") == "bdpan":
            destination = transfer.get("saved_path") or transfer.get("remote_path") or destination
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    return {
        "url": url,
        "category": category_label,
        "destination": destination,
        "files": archived,
        "count": len(archived),
        "proxy": proxy,
        "success": success,
        "transfer": transfer,
        "log": "\n".join(log_parts),
    }


def resolve_destination(target: str, cfg: dict[str, Any], category: str = "auto", author: str = "",
                        out_dir: str = "") -> tuple[str, str]:
    """Resolve the archive destination for a URL or an ASMR work number."""
    if is_asmr_target(target):
        try:
            from .asmr import fetch_preview_destination
        except ImportError:
            from asmr import fetch_preview_destination
        return asyncio.run(fetch_preview_destination(target.strip(), cfg, out_dir=out_dir))

    return classify_destination(target, cfg, category=category, author=author, out_dir=out_dir)


def is_asmr_target(target: str) -> bool:
    """Return True when a target is a DLSite work number such as ``RJ123456``."""
    return bool(_RJ_RE.match((target or "").strip()))


def run_download_target(target: str, cfg: dict[str, Any], category: str = "auto", author: str = "",
                        out_dir: str = "", proxy: str = "", force_proxy: bool = False,
                        no_proxy: bool = False, extra_args: Optional[list[str]] = None,
                        progress_cb: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """Download a target with the matching engine.

    DLSite work numbers (``RJ123456``) go through the ASMR.ONE pipeline; anything else
    keeps using yt-dlp / gallery-dl.
    """
    if is_asmr_target(target):
        try:
            from .asmr import download_asmr_work
        except ImportError:
            from asmr import download_asmr_work
        work_id = _RJ_RE.match(target.strip()).group(1)
        return asyncio.run(download_asmr_work(
            work_id=work_id,
            cfg=cfg,
            proxy=proxy,
            force_proxy=force_proxy,
            no_proxy=no_proxy,
            out_dir=out_dir,
            progress_cb=progress_cb,
        ))

    return download_media(
        url=target,
        cfg=cfg,
        category=category,
        author=author,
        out_dir=out_dir,
        proxy=proxy,
        force_proxy=force_proxy,
        no_proxy=no_proxy,
        extra_args=extra_args,
        progress_cb=progress_cb,
    )


def format_archived_paths(files: list[str], limit: int = 20) -> str:
    """Render archived paths inside a fenced code block.

    The gateway's ``extract_local_files`` turns bare absolute paths into attachments;
    keeping them inside a code span suppresses that so downloads stay "archive only".
    """
    shown = files[:limit]
    body = "\n".join(shown)
    if len(files) > limit:
        body += f"\n... 另有 {len(files) - limit} 个文件"
    return f"```\n{body}\n```"


def parse_extra_args(raw: str) -> list[str]:
    """Split a free-form extra argument string into a list."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()
