"""ASMR.ONE download pipeline for the Hermes Media Downloader plugin.

Handles works identified by their DLSite work number (``RJ123456``). The download is
staged under the plugin's shared staging directory, tagged with ``dvtag`` after the
audio is on disk, and only then archived through the normal local / rclone path so
ASMR works land in the media library like any other download.

The pipeline deliberately reuses the plugin's shared configuration (proxy, staging,
``destinations.asmr``, ``config_dir``) instead of carrying a parallel set of paths.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from html import escape as xml_escape
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

try:
    from .config import DEFAULT_CONFIG
    from .core import (
        archive_staged_files,
        find_binary,
        get_subprocess_env,
        needs_proxy,
    )
except ImportError:
    from config import DEFAULT_CONFIG
    from core import (
        archive_staged_files,
        find_binary,
        get_subprocess_env,
        needs_proxy,
    )

logger = logging.getLogger("media-downloader")

# DLSite work numbers: "RJ123456", "rj123456", or a bare numeric id.
RJ_RE = re.compile(r"^(?:RJ)?(\d{4,8})$", re.IGNORECASE)
_TEXT_SUFFIXES = {".txt", ".md", ".srt", ".vtt", ".lrc", ".ass", ".ssa"}
_HQ_SUFFIXES = (".flac", ".wav")
# Control characters, path separators, and dotdot segments never survive into a path.
_UNSAFE_NAME_RE = re.compile(r"[\x00-\x1f\x7f/\\]")

_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
        "Gecko/20100101 Firefox/128.0"
    ),
    "Origin": "https://asmr.one",
    "Referer": "https://asmr.one/",
    "Accept": "application/json",
}


class AsmrError(RuntimeError):
    """Raised when an ASMR work cannot be downloaded, tagged, or archived."""


@dataclass
class WorkTrack:
    """A single downloadable file inside an ASMR work."""

    filename: str
    url: str
    type: str
    save_path: Path
    size: Optional[float] = None
    folder_path: str = ""
    status: str = "Pending"
    downloaded_bytes: int = 0
    total_bytes: int = 0

    def is_hq(self) -> bool:
        """Whether the track is a lossless audio file."""
        return self.save_path.suffix.lower() in _HQ_SUFFIXES


def parse_work_id(target: str) -> str:
    """Extract a DLSite work id from an RJ code, or return an empty string."""
    match = RJ_RE.match((target or "").strip())
    return match.group(1) if match else ""


def sanitize_path_component(raw: str, fallback: str) -> str:
    """Make an API-provided string safe to use as a single path component."""
    raw = str(raw or "")
    if "/" in raw or "\\" in raw:
        return fallback

    cleaned = _UNSAFE_NAME_RE.sub(" ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".").strip()
    if not cleaned or cleaned in (".", ".."):
        return fallback
    return cleaned[:120]


def resolve_asmr_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Merge the ASMR section over the shipped defaults."""
    defaults = DEFAULT_CONFIG["asmr"]
    resolved = defaults.copy()
    overrides = cfg.get("asmr") or {}
    if isinstance(overrides, dict):
        resolved.update({k: v for k, v in overrides.items() if v is not None})
    return resolved


def _cache_dir(cfg: dict[str, Any]) -> Path:
    """Track-tree cache lives under the plugin's shared config directory."""
    return Path(cfg.get("config_dir", DEFAULT_CONFIG["config_dir"])).expanduser() / "asmr-cache"


def _request_timeout(acfg: dict[str, Any]) -> float:
    """Timeout used for metadata, track, and cover requests."""
    try:
        return max(5.0, float(acfg.get("request_timeout_seconds", 30)))
    except (TypeError, ValueError):
        return 30.0


def _download_attempts(acfg: dict[str, Any]) -> int:
    try:
        return max(1, int(acfg.get("download_attempts", 3)))
    except (TypeError, ValueError):
        return 3


def _file_type_set(acfg: dict[str, Any]) -> set[str]:
    raw = acfg.get("file_types") or []
    return {str(t).strip().lower() for t in raw if str(t).strip()}


def _blacklist(acfg: dict[str, Any]) -> list[str]:
    raw = acfg.get("blacklist") or []
    return [str(word) for word in raw if str(word)]


def _safe_child_path(base: Path, raw: str) -> Optional[Path]:
    """Resolve an API-provided relative path inside base, rejecting escapes."""
    candidate = (base / raw).resolve()
    if candidate == base.resolve() or base.resolve() in candidate.parents:
        return candidate
    return None


def transform_work_data(data: list[dict], base_folder: Path, parent_folder: str = "") -> list[WorkTrack]:
    """Flatten the ASMR.ONE track tree into a download list."""
    tracks: list[WorkTrack] = []
    if not isinstance(data, list):
        return tracks

    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        item_type = str(item.get("type") or "").strip().lower()
        if not title:
            continue

        if item_type == "folder":
            child_base = base_folder / sanitize_path_component(title, "folder")
            child_folder = f"{parent_folder}/{title}" if parent_folder else title
            tracks.extend(transform_work_data(item.get("children") or [], child_base, child_folder))
            continue

        if item_type not in ("text", "image", "audio"):
            continue

        url = str(item.get("mediaDownloadUrl") or "").strip()
        if not url:
            continue

        safe_path = _safe_child_path(base_folder, title)
        if safe_path is None:
            logger.warning("Skipping track with unsafe path: %s", title)
            continue

        try:
            size_val = float(item.get("size")) if item.get("size") is not None else None
        except (TypeError, ValueError):
            size_val = None

        tracks.append(
            WorkTrack(
                filename=title,
                url=url,
                type=item_type,
                save_path=safe_path,
                size=size_val,
                folder_path=parent_folder,
            )
        )
    return tracks


async def _get_json(client: httpx.AsyncClient, url: str, acfg: dict[str, Any], label: str) -> Any:
    """GET JSON with retries, raising on final failure."""
    attempts = _download_attempts(acfg)
    timeout = _request_timeout(acfg)
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_error = str(exc)
            if attempt < attempts:
                logger.warning("ASMR %s request failed (%s), retry %d/%d", label, exc, attempt + 1, attempts)
                await asyncio.sleep(2)
    raise AsmrError(f"ASMR {label} 请求失败: {last_error}")


async def fetch_work_metadata(client: httpx.AsyncClient, work_id: str, acfg: dict[str, Any]) -> dict:
    """Fetch work metadata from the ASMR.ONE API."""
    data = await _get_json(client, f"{acfg['api_base']}/api/workInfo/{work_id}", acfg, "metadata")
    if not isinstance(data, dict) or not data:
        raise AsmrError(f"RJ{work_id} 元数据为空")
    return data


async def fetch_work_tracks(client: httpx.AsyncClient, work_id: str, work_dir: Path,
                            cfg: dict[str, Any], acfg: dict[str, Any]) -> list[WorkTrack]:
    """Fetch the track tree, caching it under the plugin's shared config directory."""
    cache_file = _cache_dir(cfg) / f"RJ{work_id}_tracks.json"
    data: Any = None
    if cache_file.is_file():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            logger.debug("Using cached track tree for RJ%s", work_id)
        except Exception as exc:
            logger.warning("Ignoring unreadable track cache %s: %s", cache_file, exc)

    if data is None:
        data = await _get_json(client, f"{acfg['api_base']}/api/tracks/{work_id}?v=2", acfg, "tracks")
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to write track cache %s: %s", cache_file, exc)

    return transform_work_data(data, work_dir)


def select_tracks(tracks: list[WorkTrack], work_id: str, acfg: dict[str, Any]) -> list[WorkTrack]:
    """Filter tracks by file type, lossless preference, and the title blacklist."""
    allowed = _file_type_set(acfg)
    banned = _blacklist(acfg)
    hq_only = bool(acfg.get("hq_only", True))

    selected: list[WorkTrack] = []
    for track in tracks:
        if any(word in track.folder_path for word in banned) or any(word in track.filename for word in banned):
            continue
        if allowed and track.type not in allowed:
            continue
        if track.type == "audio" and hq_only and not track.is_hq():
            continue
        selected.append(track)

    if not selected:
        raise AsmrError(f"RJ{work_id} 没有匹配到可下载的文件")
    return selected


def _expected_bytes(track: WorkTrack) -> Optional[int]:
    """Expected file size in bytes, falling back to the API's megabyte value."""
    if track.total_bytes > 0:
        return int(track.total_bytes)
    if track.size:
        return int(float(track.size) * 1024 * 1024)
    return None


def check_file_integrity(track: WorkTrack) -> bool:
    """Whether a downloaded file exists and matches its expected size when known."""
    if not track.save_path.is_file():
        return False
    expected = _expected_bytes(track)
    if expected is None:
        return True
    try:
        return track.save_path.stat().st_size == expected
    except OSError:
        return False


async def _download_track(client: httpx.AsyncClient, track: WorkTrack, chunk_size: int,
                          attempts: int) -> bool:
    """Download one track, resuming when the server honors a Range request."""
    if check_file_integrity(track):
        track.status = "Downloaded"
        return True

    track.save_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, attempts + 1):
        existing_size = track.save_path.stat().st_size if track.save_path.exists() else 0
        headers = {"Range": f"bytes={existing_size}-"} if existing_size else {}
        try:
            async with client.stream("GET", track.url, headers=headers,
                                     follow_redirects=True) as resp:
                if resp.status_code == 416 and existing_size:
                    # Server rejects the resume: restart this file from scratch.
                    track.save_path.unlink(missing_ok=True)
                    track.downloaded_bytes = 0
                    track.status = "Retrying"
                    await asyncio.sleep(1)
                    continue

                resp.raise_for_status()

                total = int(resp.headers.get("Content-Length") or 0)
                content_range = resp.headers.get("Content-Range")
                if content_range and "/" in content_range:
                    total = int(content_range.rsplit("/", 1)[1])
                track.total_bytes = total

                resuming = existing_size > 0 and resp.status_code == 206
                if not resuming and existing_size:
                    track.save_path.unlink(missing_ok=True)
                    existing_size = 0
                track.downloaded_bytes = existing_size if resuming else 0

                with open(track.save_path, "ab" if resuming else "wb") as handle:
                    async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        track.downloaded_bytes += len(chunk)

            if check_file_integrity(track):
                track.status = "Completed"
                return True

            track.status = "Retrying"
            logger.warning("Size mismatch for %s, retrying (%d/%d)", track.filename, attempt, attempts)
        except Exception as exc:
            track.status = "Retrying"
            logger.warning("Failed to download %s (%s), retry %d/%d", track.filename, exc, attempt, attempts)
            await asyncio.sleep(2)

    track.status = "Error"
    return False


async def download_tracks(tracks: list[WorkTrack], cfg: dict[str, Any], acfg: dict[str, Any],
                          proxy: Optional[str]) -> None:
    """Download every selected track with bounded concurrency and retry."""
    try:
        concurrency = max(1, int(acfg.get("max_concurrent_downloads", 3)))
    except (TypeError, ValueError):
        concurrency = 3
    attempts = _download_attempts(acfg)
    chunk_size = 2 * 1024 * 1024

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=None, limits=limits, headers=_API_HEADERS,
                                 proxy=proxy or None) as client:
        semaphore = asyncio.Semaphore(concurrency)

        async def run(track: WorkTrack) -> None:
            async with semaphore:
                await _download_track(client, track, chunk_size, attempts)

        await asyncio.gather(*(run(track) for track in tracks))


async def download_cover(client: httpx.AsyncClient, cover_url: str, work_dir: Path,
                         acfg: dict[str, Any]) -> Optional[Path]:
    """Download the work cover so it is archived (and taggable) with the audio."""
    if not cover_url:
        return None
    target = work_dir / "cover.jpg"
    try:
        resp = await client.get(cover_url, timeout=_request_timeout(acfg))
        resp.raise_for_status()
        target.write_bytes(resp.content)
        return target
    except Exception as exc:
        logger.warning("ASMR cover download failed: %s", exc)
        return None


def vtt_timestamp_to_lrc(timestamp: str) -> str:
    """Convert a VTT timestamp to an LRC timestamp, dropping milliseconds."""
    parts = timestamp.replace(",", ".").split(":")
    if len(parts) != 3:
        return "[00:00]"
    try:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), int(float(parts[2]))
    except ValueError:
        return "[00:00]"
    return f"[{hours * 60 + minutes:02d}:{seconds:02d}]"


def convert_vtt_to_lrc(vtt_path: Path, lrc_path: Path) -> None:
    """Convert a VTT subtitle file to LRC, tolerating non-UTF-8 bytes."""
    with open(vtt_path, encoding="utf-8-sig", errors="replace") as source, \
            open(lrc_path, "w", encoding="utf-8") as target:
        pending: Optional[str] = None
        text_lines_remaining = 0
        for raw_line in source:
            line = raw_line.strip()
            if "-->" in line:
                pending = vtt_timestamp_to_lrc(line.split("-->")[0].strip())
                text_lines_remaining = 2
                continue
            if pending is None or text_lines_remaining <= 0:
                continue
            text_lines_remaining -= 1
            if not line or line.isdigit():
                continue
            target.write(f"{pending}{line}\n")
            pending = None


def convert_subtitles(work_dir: Path) -> int:
    """Convert every VTT file under the work directory to LRC."""
    converted = 0
    for vtt_path in work_dir.rglob("*.vtt"):
        if vtt_path.name.endswith(".wav.vtt"):
            lrc_path = vtt_path.with_name(vtt_path.name.removesuffix(".wav.vtt") + ".lrc")
        else:
            lrc_path = vtt_path.with_suffix(".lrc")
        try:
            convert_vtt_to_lrc(vtt_path, lrc_path)
            converted += 1
        except OSError as exc:
            logger.warning("Failed to convert subtitle %s: %s", vtt_path, exc)
    return converted


def generate_nfo(work_dir: Path, work_id: str, meta: dict) -> Path:
    """Write an album NFO so media servers ingest the work with its metadata."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = str(meta.get("title") or f"RJ{work_id}")
    year = str(meta.get("release") or now)[:4]
    try:
        runtime = int(meta.get("duration") or 0) // 60
    except (TypeError, ValueError):
        runtime = 0
    maker = str((meta.get("circle") or {}).get("name") or "")

    def names(key: str) -> list[str]:
        values = meta.get(key) or []
        if not isinstance(values, list):
            return []
        return [str(item.get("name") or "") for item in values if isinstance(item, dict) and item.get("name")]

    def block(tag: str, values: list[str]) -> str:
        return "".join(f"  <{tag}>{xml_escape(value)}</{tag}>\n" for value in values)

    content = (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
        "<album>\n"
        "  <review />\n"
        "  <outline />\n"
        "  <lockdata>false</lockdata>\n"
        f"  <dateadded>{now}</dateadded>\n"
        f"  <title>{xml_escape(title)}</title>\n"
        f"  <year>{xml_escape(year)}</year>\n"
        f"  <sorttitle>{xml_escape(title)}</sorttitle>\n"
        f"  <runtime>{runtime}</runtime>\n"
        f"{block('genre', names('tags'))}"
        f"{block('artist', names('vas') or ['Unknown'])}"
        f"{block('albumartist', names('vas') or ['Unknown'])}"
        f"  <maker>{xml_escape(maker)}</maker>\n"
        f"  <thumb>{xml_escape(str(meta.get('mainCoverUrl') or ''))}</thumb>\n"
        "</album>\n"
    )
    nfo_path = work_dir / "album.nfo"
    nfo_path.write_text(content, encoding="utf-8")
    return nfo_path


def run_dvtag(work_dir: Path, work_id: str, cfg: dict[str, Any], acfg: dict[str, Any],
              proxy: Optional[str]) -> str:
    """Tag the downloaded work with `dvtag` and transcode WAV to FLAC."""
    dvtag_bin = find_binary("dvtag", cfg)
    try:
        timeout = max(60, int(acfg.get("dvtag_timeout_seconds", 3600)))
    except (TypeError, ValueError):
        timeout = 3600

    cmd = [dvtag_bin, "-w2f", str(work_dir)]
    try:
        result = subprocess.run(cmd, env=get_subprocess_env(proxy), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace",
                                timeout=timeout)
    except FileNotFoundError as exc:
        raise AsmrError(f"未找到 dvtag 可执行文件，无法为 RJ{work_id} 写入标签: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AsmrError(f"dvtag 处理 RJ{work_id} 超时（{timeout}s）") from exc

    tail = "\n".join(line for line in (result.stdout or "").splitlines() if line.strip())[-2000:]
    if result.returncode != 0:
        raise AsmrError(
            f"dvtag 处理 RJ{work_id} 失败（退出码 {result.returncode}），已保留暂存目录 {work_dir}"
            + (f"\n{tail}" if tail else "")
        )
    return tail


def resolve_asmr_destination(meta: dict, work_id: str, cfg: dict[str, Any]) -> tuple[str, str]:
    """Resolve the archive destination for a work: <asmr root>/<circle>/<source id>.

    Works flagged as R18 by ASMR.ONE are archived under ``destinations.asmr_r`` so
    adult titles stay in a separate library; everything else uses ``destinations.asmr``.
    Both may be a local path or an rclone spec; the returned value stays a plain
    string so remote targets are never coerced into filesystem paths.
    """
    destinations = cfg.get("destinations") or DEFAULT_CONFIG["destinations"]
    r18 = bool(meta.get("nsfw"))
    key = "asmr_r" if r18 else "asmr"
    root = str(destinations.get(key) or "").strip().rstrip("/")
    if not root:
        raise AsmrError(f"未配置 destinations.{key}，无法归档 ASMR 作品")

    circle = sanitize_path_component(str((meta.get("circle") or {}).get("name") or ""), "Unknown")
    source_id = sanitize_path_component(str(meta.get("source_id") or f"RJ{work_id}"), f"RJ{work_id}")
    return f"{root}/{circle}/{source_id}", "asmr"


def _progress_message(tracks: list[WorkTrack]) -> str:
    done = sum(1 for t in tracks if t.status in ("Downloaded", "Completed"))
    total_bytes = sum(_expected_bytes(t) or 0 for t in tracks)
    done_bytes = sum((_expected_bytes(t) or 0) if t.status in ("Downloaded", "Completed")
                     else t.downloaded_bytes for t in tracks)
    percent = (done_bytes * 100 / total_bytes) if total_bytes else 0.0
    return f"ASMR 下载进度: {percent:.1f}% ({done}/{len(tracks)} 个文件)"


def preview_destination(work_id: str, cfg: dict[str, Any], out_dir: str = "") -> tuple[str, str]:
    """Best-effort archive destination for a work, used when a task is submitted.

    Falls back to a metadata-free layout when the API is unreachable so command
    submission never blocks on ASMR.ONE availability.
    """
    if out_dir.strip():
        return out_dir.strip(), "asmr"

    try:
        # Metadata is unavailable here, so the R18 split cannot be resolved yet.
        destination, label = resolve_asmr_destination({}, work_id, cfg)
    except AsmrError:
        destinations = cfg.get("destinations") or DEFAULT_CONFIG["destinations"]
        root = str(destinations.get("asmr") or "").strip().rstrip("/")
        return (f"{root}/{f'RJ{work_id}'}" if root else ""), "asmr"
    return destination, label


async def fetch_preview_destination(target: str, cfg: dict[str, Any], out_dir: str = "") -> tuple[str, str]:
    """Resolve the real <circle>/<source id> destination by reading work metadata."""
    work_id = parse_work_id(target)
    if not work_id:
        raise AsmrError(f"无效的 ASMR 作品编号: {target}")

    acfg = resolve_asmr_config(cfg)
    proxy = ""
    if needs_proxy(str(acfg.get("api_base") or ""), cfg):
        proxy = cfg.get("default_proxy", DEFAULT_CONFIG["default_proxy"])

    try:
        async with httpx.AsyncClient(timeout=_request_timeout(acfg), headers=_API_HEADERS,
                                     proxy=proxy or None) as client:
            meta = await fetch_work_metadata(client, work_id, acfg)
    except Exception:
        logger.info("ASMR metadata unavailable for RJ%s, using fallback destination", work_id)
        return preview_destination(work_id, cfg, out_dir=out_dir)

    destination, label = resolve_asmr_destination(meta, work_id, cfg)
    return (out_dir.strip() or destination), label


def _make_staging_dir(cfg: dict[str, Any], work_id: str) -> Path:
    """Create the work's staging directory, running under the shared staging base.

    Staged contents (audio, cover, subtitles, NFO) are archived as-is, so the staging
    directory is the work directory; a numeric suffix keeps repeated runs from
    reusing a leftover directory.
    """
    base = Path(cfg.get("staging_base", DEFAULT_CONFIG["staging_base"]))
    if not base.parent.exists():
        base = Path(cfg.get("staging_fallback", DEFAULT_CONFIG["staging_fallback"]))

    candidate = base / f"RJ{work_id}"
    attempt = 0
    while True:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            attempt += 1
            candidate = base / f"RJ{work_id}_{attempt}"


async def download_asmr_work(work_id: str, cfg: dict[str, Any], proxy: str = "",
                             force_proxy: bool = False, no_proxy: bool = False,
                             out_dir: str = "",
                             progress_cb: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """Download, tag, and archive one ASMR.ONE work."""
    acfg = resolve_asmr_config(cfg)
    api_base = str(acfg.get("api_base") or "").strip()
    if not api_base:
        raise AsmrError("未配置 asmr.api_base")

    if not proxy and needs_proxy(api_base, cfg, force_proxy=force_proxy, no_proxy=no_proxy):
        proxy = cfg.get("default_proxy", DEFAULT_CONFIG["default_proxy"])

    work_dir = _make_staging_dir(cfg, work_id)
    log_parts = [
        f"Work: RJ{work_id}",
        f"API: {api_base}",
        f"Proxy: {proxy or 'None (Direct)'}",
        f"Staging: {work_dir}",
    ]

    try:
        async with httpx.AsyncClient(timeout=_request_timeout(acfg), headers=_API_HEADERS,
                                     proxy=proxy or None) as api_client:
            meta = await fetch_work_metadata(api_client, work_id, acfg)
            tracks = await fetch_work_tracks(api_client, work_id, work_dir, cfg, acfg)
            if not tracks:
                raise AsmrError(f"RJ{work_id} 没有解析到任何文件")

            selected = select_tracks(tracks, work_id, acfg)
            log_parts.append(f"Tracks: {len(selected)}/{len(tracks)} selected")
            log_parts.append(f"Title: {meta.get('title') or 'unknown'}")

            await download_tracks(selected, cfg, acfg, proxy)

            failed = [t for t in selected if not check_file_integrity(t)]
            blocking = [t for t in failed if not (t.type == "text" or t.save_path.suffix.lower() in _TEXT_SUFFIXES)]
            if blocking:
                raise AsmrError(
                    f"RJ{work_id} 以下文件下载失败: " + ", ".join(t.filename for t in blocking[:10])
                )
            if failed:
                log_parts.append("Warning: " + ", ".join(t.filename for t in failed[:10]) + " 校验未通过（文本类，忽略）")

            await download_cover(api_client, str(meta.get("mainCoverUrl") or ""), work_dir, acfg)

        log_parts.append(_progress_message(selected))

        dvtag_log = run_dvtag(work_dir, work_id, cfg, acfg, proxy)
        if dvtag_log:
            log_parts.append(f"dvtag:\n{dvtag_log}")

        converted = convert_subtitles(work_dir)
        if converted:
            log_parts.append(f"Subtitles: converted {converted} VTT file(s) to LRC")

        generate_nfo(work_dir, work_id, meta)

        destination = out_dir.strip() or resolve_asmr_destination(meta, work_id, cfg)[0]
        log_parts.append(f"Destination: {destination}")

        circle = sanitize_path_component(str((meta.get("circle") or {}).get("name") or ""), "Unknown")
        source_id = sanitize_path_component(str(meta.get("source_id") or f"RJ{work_id}"), f"RJ{work_id}")
        archived, archive_log, transfer = archive_staged_files(
            work_dir, destination, cfg, progress_cb=progress_cb,
            internal_prefix=(circle, source_id),
            slug_hint=str(meta.get("title") or f"RJ{work_id}"))
        if archive_log:
            log_parts.append(archive_log)
        if transfer.get("engine") == "bdpan":
            destination = transfer.get("saved_path") or transfer.get("remote_path") or destination

        return {
            "url": f"RJ{work_id}",
            "category": "asmr",
            "destination": destination,
            "files": archived,
            "count": len(archived),
            "proxy": proxy,
            "success": True,
            "transfer": transfer,
            "log": "\n".join(log_parts),
        }
    except Exception:
        # Keep the staged work on disk so a failed dvtag/tagging run can be inspected.
        logger.exception("ASMR download failed for RJ%s, staging kept at %s", work_id, work_dir)
        raise
    finally:
        try:
            if work_dir.exists() and not any(work_dir.iterdir()):
                work_dir.rmdir()
        except OSError:
            pass
