"""Core processing routines for audio extraction, video clipping, and subtitle sync."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("audio-extract")

# Subtitle/lyric sidecar extensions (mirrors scripts/scan_missing_subs.py). A media
# file that already ships one of these next to it is treated as "has lyrics/subtitles"
# and skipped by the audio-extraction scan -- there is nothing left to transcribe.
SIDECAR_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".smi", ".idx", ".lrc"}

# Language / status tokens stripped when normalizing a subtitle stem.
_LANG_TAGS = {
    "chs", "cht", "zh-cn", "zh-tw", "zh", "cn", "tc", "sc", "zho", "chi",
    "eng", "en", "jp", "ja", "default", "forced", "流出", "破解",
}

# Multi-encode / resolution tags stripped when normalizing a media stem.
_ENCODE_TAG_RE = re.compile(
    r"\s*-\s*(hevc|h265|h264|av1|x264|x265|qsv|nvenc|1080p|720p|4k|2160p)\S*",
    re.IGNORECASE,
)


def _is_junk_name(name: str) -> bool:
    """Ignore hidden files, AppleDouble, Synology @eaDir, and OS junk."""
    if name.startswith(".") or name.startswith("._") or name == "@eaDir":
        return True
    return name.lower() in {"thumbs.db", ".ds_store", "desktop.ini"}


def _clean_sidecar_stem(stem: str) -> str:
    """Strip language/status tags from a subtitle stem (``name.chs`` -> ``name``)."""
    parts = stem.split(".")
    while len(parts) > 1 and parts[-1].lower() in _LANG_TAGS:
        parts.pop()
    stem = ".".join(parts)
    parts_dash = stem.split("-")
    while len(parts_dash) > 1 and parts_dash[-1].lower() in _LANG_TAGS:
        parts_dash.pop()
    return "-".join(parts_dash).strip()


def _clean_media_stem(stem: str) -> str:
    """Strip multi-encode tags from a media stem (``name - hevc-qsv`` -> ``name``)."""
    return _ENCODE_TAG_RE.sub("", stem).strip()


def _norm_stem(stem: str) -> str:
    return re.sub(r"[\s_]+", " ", stem).lower()


def has_subtitle_sidecar(media_file: Path, siblings: list[Path] | None = None) -> bool:
    """True when ``media_file`` has a matching subtitle/lyric sidecar beside it.

    Matches the rules used by ``scripts/scan_missing_subs.py`` but for a single
    directory: exact stem (``name.srt``), language-suffix stem (``name.chs.srt`` /
    ``name-zh-CN.srt``), audio ``<media>.lrc`` style (``track.flac.lrc``), and a
    normalized stem that ignores language and multi-encode tags
    (``name - hevc-qsv.mkv`` vs ``name.chs.srt``). Pass ``siblings`` to reuse an
    existing directory listing instead of re-reading the parent folder.
    """
    if siblings is None:
        try:
            siblings = list(media_file.parent.iterdir())
        except OSError:
            return False

    v_stem = media_file.stem
    v_clean = _clean_media_stem(v_stem)
    v_norm = _norm_stem(v_clean)
    v_lower = v_stem.lower()

    for s in siblings:
        if s.suffix.lower() not in SIDECAR_EXTS or _is_junk_name(s.name):
            continue
        if not s.is_file():
            continue
        s_stem = s.stem
        s_lower = s_stem.lower()
        if s_lower == v_lower:
            return True
        if s_lower.startswith(f"{v_lower}.") or s_lower.startswith(f"{v_lower}-"):
            return True
        s_clean = _clean_sidecar_stem(s_stem)
        s_norm = _norm_stem(s_clean)
        if s_clean and (s_clean.lower() == v_clean.lower() or s_norm == v_norm):
            return True
        if s_clean and (v_clean.lower().startswith(s_clean.lower()) or s_clean.lower().startswith(v_clean.lower())):
            return True
        if s_norm and (v_norm.startswith(s_norm) or s_norm.startswith(v_norm)):
            return True

    return False


# Download clients write into the destination before the payload is complete: a temp
# suffix on the payload itself, a control file beside it, or an "incomplete" folder.
# Such a file must never be handed to ffmpeg -- a partially written container either
# fails outright or, worse, yields a truncated audio track.
_PARTIAL_SUFFIXES = {
    ".part", ".partial", ".!qb", ".!ut", ".tmp", ".temp", ".download",
    ".downloading", ".crdownload", ".opdownload", ".filepart", ".bc!", ".aria2",
}
_PARTIAL_DIR_NAMES = {".incomplete", "incomplete", ".downloading", ".unpack"}
_PARTIAL_CONTROL_SUFFIXES = (".part", ".aria2", ".!qb", ".!ut", ".downloading", ".crdownload")

# Tunables for the periodic scan's auto-extraction. Every value can be overridden by
# an ``auto_extract`` object in the plugin config.
AUTO_EXTRACT_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "format": "mp3",
    # A file must be untouched for this long before it is considered settled.
    "settle_seconds": 900,
    # Second size/mtime sample taken ``sample_wait_seconds`` later, catching writers
    # that keep an old mtime (sparse preallocation, open handles).
    "sample_wait_seconds": 2,
    "batch_size": 20,
    "max_per_scan": 200,
    # Wall-clock cap for one scan run; leftovers are deferred to the next scan so a
    # long batch never trips the cron script timeout mid-extraction.
    "max_seconds_per_scan": 3000,
    # Directory prefixes whose files are indexed but never auto-extracted
    # (e.g. live recordings that are not transcription targets).
    "exclude_paths": [],
    "delivery_target": "",
}


def auto_extract_options(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return the auto-extract tunables merged over the built-in defaults."""
    opts = dict(AUTO_EXTRACT_DEFAULTS)
    raw = cfg.get("auto_extract")
    if isinstance(raw, dict):
        opts.update({k: v for k, v in raw.items() if v is not None})
    return opts


def is_write_in_progress(path: Path) -> bool:
    """True when ``path`` still looks like an unfinished download or temporary write."""
    name = path.name
    if not name or name.startswith("."):
        return True
    if path.suffix.lower() in _PARTIAL_SUFFIXES:
        return True
    if any(part.lower() in _PARTIAL_DIR_NAMES for part in path.parts[:-1]):
        return True
    for marker in _PARTIAL_CONTROL_SUFFIXES:
        try:
            if path.with_name(name + marker).exists():
                return True
        except OSError:
            return True
    return False


def is_excluded_path(path: Path, patterns: list[str]) -> bool:
    """True when ``path`` lives under one of the configured directory prefixes."""
    normalized = os.path.normpath(str(path))
    for pattern in patterns:
        prefix = os.path.normpath(str(pattern)).rstrip(os.sep)
        if not prefix:
            continue
        if normalized == prefix or normalized.startswith(prefix + os.sep):
            return True
    return False


def _file_signature(path: Path) -> tuple[int, int] | None:
    """(size, mtime_ns) of a file, or None when it cannot be stat'ed."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def normalize_time_format(time_str: str) -> str:
    """Normalize time format to HH:MM:SS or MM:SS."""
    time_str = time_str.strip()
    if "-" in time_str and len(time_str) in (11, 12, 13):
        return time_str
    # Compact format HMMSS or HHMMSS
    if time_str.isdigit():
        if len(time_str) == 5:
            return f"0{time_str[0]}:{time_str[1:3]}:{time_str[3:5]}"
        if len(time_str) == 6:
            return f"{time_str[0:2]}:{time_str[2:4]}:{time_str[4:6]}"
        if len(time_str) == 4:
            return f"00:{time_str[0:2]}:{time_str[2:4]}"
    parts = time_str.split(":")
    if len(parts) == 2:
        return f"00:{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    if len(parts) == 3:
        return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}:{parts[2].zfill(2)}"
    return time_str


def parse_interval(interval_str: str) -> tuple[str, str]:
    """Parse time interval like 10101-20356 or 00:01:00-00:02:30."""
    parts = interval_str.strip().split("-")
    if len(parts) == 2:
        return normalize_time_format(parts[0]), normalize_time_format(parts[1])
    raise ValueError(f"Invalid interval format: {interval_str}")


def search_files(
    keyword: str,
    cfg: dict[str, Any],
    limit: int = 20,
    skip_with_subtitles: bool = False,
) -> list[str]:
    """Search files in SQLite index if available, or walk scan_dirs.

    When ``skip_with_subtitles`` is set, files that already have a matching
    subtitle/lyric sidecar are omitted, so only files still needing transcription
    are surfaced.
    """
    db_path = cfg.get("db_path")
    exts = tuple(e.lower() for e in cfg.get("index_extensions", []))
    results: list[str] = []

    if db_path and os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            query = f"%{keyword}%"
            # Fetch a wider pool when filtering: sidecars are dropped after the query.
            fetch = limit * 8 if skip_with_subtitles else limit * 2
            c.execute(
                "SELECT path FROM files WHERE is_dir = 0 AND name LIKE ? ORDER BY mtime DESC LIMIT ?",
                (query, fetch),
            )
            found_any = False
            for row in c.fetchall():
                p = row[0]
                if any(p.lower().endswith(ext) for ext in exts) and os.path.exists(p):
                    found_any = True
                    if skip_with_subtitles and has_subtitle_sidecar(Path(p)):
                        continue
                    results.append(p)
                    if len(results) >= limit:
                        break
            conn.close()
            if found_any:
                return results
        except Exception as e:
            logger.warning("DB search error (%s), falling back to filesystem walk", e)

    # Fallback to filesystem scan
    for scan_dir in cfg.get("scan_dirs", []):
        base = Path(scan_dir)
        if not base.exists():
            continue
        for root, _, files in os.walk(base):
            siblings = [Path(root, f) for f in files]
            for f in files:
                if keyword.lower() in f.lower() and any(f.lower().endswith(ext) for ext in exts):
                    full_p = os.path.join(root, f)
                    if skip_with_subtitles and has_subtitle_sidecar(Path(full_p), siblings):
                        continue
                    results.append(full_p)
                    if len(results) >= limit:
                        return results
    return results


def resolve_target_files(
    target: str,
    cfg: dict[str, Any],
    skip_with_subtitles: bool = False,
) -> list[Path]:
    """Resolve a target string into concrete media files.

    Resolution order:
    - Existing file -> that file (an explicit path is always honored as-is).
    - Existing directory -> media files directly inside it, matching
      ``index_extensions``. Subdirectories are intentionally NOT traversed so
      pointing at a folder only extracts its immediate files instead of an
      entire tree. With ``skip_with_subtitles``, files that already have a
      subtitle/lyric sidecar are dropped.
    - Anything else -> index/keyword search (best single match).
    """
    p = Path(target).expanduser()
    if p.is_file():
        return [p.resolve()]

    if p.is_dir():
        exts = tuple(e.lower() for e in cfg.get("index_extensions", []))
        try:
            entries = list(p.iterdir())
        except OSError as e:
            logger.warning("Cannot scan directory %s: %s", p, e)
            return []
        media = [f for f in entries if f.is_file() and f.suffix.lower() in exts]
        if skip_with_subtitles:
            media = [f for f in media if not has_subtitle_sidecar(f, entries)]
        return sorted((f.resolve() for f in media), key=lambda f: f.name.lower())

    matches = search_files(target, cfg, limit=5, skip_with_subtitles=skip_with_subtitles)
    if matches:
        return [Path(matches[0]).resolve()]

    return []


def resolve_target_file(target: str, cfg: dict[str, Any]) -> Path | None:
    """Resolve a target string to a single concrete Path (direct path or index search)."""
    files = resolve_target_files(target, cfg)
    return files[0] if files else None


def search_candidates(
    target: str,
    cfg: dict[str, Any],
    limit: int = 10,
    skip_with_subtitles: bool = True,
) -> list[Path]:
    """Return *all* indexed files matching a keyword search (newest first).

    Unlike ``resolve_target_files`` (which deliberately returns the single best
    match so non-interactive callers never fan out), this exposes the full
    candidate list so an interactive caller can let the user pick which files to
    process. Files that already have a subtitle/lyric sidecar are skipped by
    default, since they no longer need transcription.
    """
    return [
        Path(m).resolve()
        for m in search_files(target, cfg, limit=limit, skip_with_subtitles=skip_with_subtitles)
    ]


DEFAULT_RECORD_DIR = "/mnt/media/record"
RECORD_UPLOAD_SUBDIR = "upload"

# Emby appends " (1)", " (2)" ... to the mobile renditions it generates. These are
# variants of the recording itself and go away with it.
_EMBY_MOBILE_SUFFIX_RE = re.compile(
    r"^(?:\(\d+\))+(?:-poster\.jpg|\.(?:mp4|mkv))$", re.IGNORECASE
)


def record_dir_setting(cfg: dict[str, Any]) -> Path:
    """Directory holding livestream recordings (``record_dir`` in the plugin config)."""
    raw = cfg.get("record_dir") or DEFAULT_RECORD_DIR
    return Path(str(raw)).expanduser().resolve()


def record_upload_dir(cfg: dict[str, Any]) -> Path:
    """Staging directory the clipped segment is published from (``<record_dir>/upload``)."""
    return record_dir_setting(cfg) / RECORD_UPLOAD_SUBDIR


def is_record_source(path: Path, cfg: dict[str, Any]) -> bool:
    """True when ``path`` is a recording that should be clipped into ``upload``.

    Files already inside ``upload`` are excluded: they were published earlier, so
    re-clipping one must not land on itself or trigger source cleanup.
    """
    root = record_dir_setting(cfg)
    upload = root / RECORD_UPLOAD_SUBDIR
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if resolved == root:
        return False
    if resolved == upload or upload in resolved.parents:
        return False
    return root in resolved.parents


def record_upload_path(source: Path, cfg: dict[str, Any]) -> Path:
    """Final path of a clipped recording: ``<record_dir>/upload/<original name>``."""
    return record_upload_dir(cfg) / source.name


def record_related_files(source: Path) -> list[Path]:
    """Sidecars Emby generates next to a recording, mirroring scripts/move_clip.sh."""
    parent = source.parent
    stem = source.stem
    related = [
        parent / f"{stem}-poster.jpg",
        parent / f"{stem}.jpg",
        parent / f"{stem}.nfo",
        parent / f"{stem} - mobile.mp4",
        parent / f"{stem} - mobile.mkv",
        parent / f"{stem} - mobile-poster.jpg",
    ]
    prefix = f"{stem} - mobile "
    try:
        entries = list(parent.iterdir())
    except OSError:
        return related
    for entry in entries:
        if entry.name.startswith(prefix) and _EMBY_MOBILE_SUFFIX_RE.fullmatch(entry.name[len(prefix):]):
            related.append(entry)
    return related


def cleanup_record_source(
    source: Path, cfg: dict[str, Any], keep: Path | None = None
) -> list[Path]:
    """Delete a clipped recording and its Emby sidecars; return the removed paths.

    Only call this once the clipped output is written and verified: the source is gone
    afterwards. ``keep`` protects a path (the clipped output) that happens to match the
    sidecar naming pattern. The source itself is deleted last so a partial failure still
    leaves it available for a retry.
    """
    if not is_record_source(source, cfg):
        return []
    keep_path = keep.resolve() if keep is not None else None
    targets = [p for p in record_related_files(source) if p != source]
    targets.append(source)

    deleted: list[Path] = []
    for path in targets:
        try:
            if keep_path is not None and path.resolve() == keep_path:
                continue
            if path.is_symlink() or path.is_file():
                path.unlink()
                deleted.append(path)
        except OSError as e:
            logger.warning("Failed to remove %s: %s", path, e)
    return deleted


def clip_video_file(
    input_file: Path,
    start_time: str,
    end_time: str,
    output_path: Path | None = None,
    reencode: bool = False,
) -> tuple[Path, str]:
    """Fast clip video segment (default: copy stream, no re-encoding). Returns (output_path, log).

    FFmpeg writes to a hidden staging file that is renamed only after the muxer exits
    cleanly, so a failed/aborted clip never leaves a truncated file at ``output_path``
    (which callers may treat as a finished clip before removing the source).
    """
    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")

    start_norm = normalize_time_format(start_time)
    end_norm = normalize_time_format(end_time)

    if not output_path:
        tag_start = start_norm.replace(":", "")
        tag_end = end_norm.replace(":", "")
        output_path = input_file.parent / f"{input_file.stem}_{tag_start}-{tag_end}{input_file.suffix}"

    if output_path.resolve() == input_file.resolve():
        raise ValueError(f"Output path equals input file, refusing to overwrite source: {input_file}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.with_name(f".{output_path.stem}.tmp{output_path.suffix}")

    cmd = ["ffmpeg", "-y", "-ss", start_norm, "-to", end_norm, "-i", str(input_file)]
    if not reencode:
        cmd.extend(["-c", "copy", "-avoid_negative_ts", "1"])
    else:
        cmd.extend(["-c:v", "libx264", "-crf", "22", "-c:a", "aac"])
    cmd.append(str(staging))

    cmd_str = " ".join(cmd)
    logger.info("Executing: %s", cmd_str)
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg clipping failed: {res.stderr}")
        if not staging.is_file() or staging.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg produced no output: {staging}")
        os.replace(staging, output_path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise

    return output_path, f"Command: {cmd_str}\nStatus: Success\nOutput: {output_path}"


# FFmpeg infers the output muxer from the file extension, but extraction writes to a
# ".tmp" path (see below) whose extension carries no format hint, so the muxer must be
# passed explicitly. Names differ from the common extension for a few containers.
_FFMPEG_AUDIO_MUXERS = {
    "mp3": "mp3",
    "m4a": "ipod",
    "aac": "adts",
    "wav": "wav",
    "flac": "flac",
    "ogg": "ogg",
    "opus": "opus",
}


def extract_audio_file(
    input_file: Path,
    out_dir: Path,
    jobs_dir: Path | None = None,
    format: str = "mp3",
) -> tuple[Path, str]:
    """Extract audio from video/audio file (16kHz mono for Whisper). Returns (output_path, log)."""
    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")

    out_dir.mkdir(parents=True, exist_ok=True)
    output_file = out_dir / f"{input_file.stem}.{format}"
    # Extract to a ".tmp" sibling first and rename on success: the rename is atomic on
    # the same filesystem, so download clients never pick up a half-written file.
    tmp_file = output_file.with_name(output_file.name + ".tmp")

    cmd = [
        "ffmpeg",
        "-y",
        "-nostats",
        "-i",
        str(input_file),
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        _FFMPEG_AUDIO_MUXERS.get(format.lower(), format),
        str(tmp_file),
    ]

    cmd_str = " ".join(cmd)
    logger.info("Executing: %s", cmd_str)
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg audio extraction failed: {res.stderr}")
        os.replace(tmp_file, output_file)
    except BaseException:
        tmp_file.unlink(missing_ok=True)
        raise
    logger.info("Audio extraction finalized: %s", output_file)

    job_info = ""
    if jobs_dir:
        jobs_dir.mkdir(parents=True, exist_ok=True)
        job_file = jobs_dir / f"{input_file.name}.json"
        job_data = {
            "video": str(input_file.resolve()),
            "mp3": str(output_file.resolve()),
            "created_at": time.time(),
        }
        with open(job_file, "w", encoding="utf-8") as f:
            json.dump(job_data, f, ensure_ascii=False, indent=2)
        job_info = f"Registered job: {job_file.name}"

    log = f"Command: {cmd_str}\nOutput: {output_file}\n{job_info}".strip()
    return output_file, log


def vtt_timestamp_to_lrc(vtt_ts: str) -> str:
    """Convert VTT timestamp (HH:MM:SS.mmm or MM:SS.mmm) to LRC ([MM:SS.xx])."""
    parts = vtt_ts.strip().split(":")
    if len(parts) == 3:
        h, m, s = parts
        total_minutes = int(h) * 60 + int(m)
    elif len(parts) == 2:
        m, s = parts
        total_minutes = int(m)
    else:
        return ""
    if "." in s:
        sec, frac = s.split(".")
        frac_2digit = frac[:2].ljust(2, "0")
    else:
        sec = s
        frac_2digit = "00"
    return f"[{total_minutes:02d}:{int(sec):02d}.{frac_2digit}]"


def srt_timestamp_to_lrc(srt_ts: str) -> str:
    """Convert SRT timestamp (HH:MM:SS,mmm) to LRC ([MM:SS.xx])."""
    return vtt_timestamp_to_lrc(srt_ts.replace(",", "."))


def convert_subtitle_to_lrc(sub_path: Path, lrc_path: Path) -> bool:
    """Convert SRT or VTT subtitle file to LRC format."""
    try:
        with open(sub_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        lrc_lines = []
        time_pattern = re.compile(
            r"((?:\d{2}:)?\d{2}:\d{2}[,\.]\d{2,3})\s*-->\s*(?:(?:\d{2}:)?\d{2}:\d{2}[,\.]\d{2,3})"
        )
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            match = time_pattern.search(line)
            if match:
                start_ts = match.group(1)
                lrc_ts = srt_timestamp_to_lrc(start_ts)
                text_lines = []
                i += 1
                while i < len(lines) and lines[i].strip():
                    cleaned = re.sub(r"<[^>]+>", "", lines[i].strip())
                    if cleaned and not cleaned.isdigit():
                        text_lines.append(cleaned)
                    i += 1
                text = " ".join(text_lines)
                if lrc_ts and text:
                    lrc_lines.append(f"{lrc_ts}{text}")
            else:
                i += 1
        with open(lrc_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lrc_lines) + "\n")
        return True
    except Exception as e:
        logger.error("Error converting to LRC: %s", e)
        return False


def sync_subtitles_once(cfg: dict[str, Any]) -> int:
    """Check jobs_dir for pending jobs, check out_dir for subtitles, and sync."""
    jobs_dir = Path(cfg["jobs_dir"])
    out_dir = Path(cfg["out_dir"])

    if not jobs_dir.exists():
        return 0

    processed_count = 0
    for job_file in jobs_dir.glob("*.json"):
        try:
            with open(job_file, "r", encoding="utf-8") as f:
                job = json.load(f)
            video_path = Path(job["video"])
            mp3_path = Path(job.get("mp3", ""))

            # The Temp audio is deleted when the pending transcription is abandoned, so a
            # vanished mp3 means the task was cancelled and its job file is dead weight.
            # A missing parent dir means the output share is unmounted, not a deletion:
            # never treat that as cancellation or one dead mount wipes every job.
            if job.get("mp3") and mp3_path.parent.is_dir() and not mp3_path.exists():
                logger.info("Cancelled job %s (audio deleted: %s)", job_file.name, mp3_path)
                job_file.unlink(missing_ok=True)
                continue

            ass_file = out_dir / f"{video_path.stem}.ass"
            srt_file = out_dir / f"{video_path.stem}.srt"
            vtt_file = out_dir / f"{video_path.stem}.vtt"

            matched_sub = None
            if ass_file.exists():
                matched_sub = ass_file
            elif srt_file.exists():
                matched_sub = srt_file
            elif vtt_file.exists():
                matched_sub = vtt_file

            if not matched_sub:
                continue

            logger.info("Found subtitle for job: %s -> %s", video_path.stem, matched_sub.name)
            video_ext = video_path.suffix.lower()

            if video_ext in (".flac", ".mp3", ".wav", ".m4a", ".aac"):
                target_lrc = video_path.with_suffix(".lrc")
                if matched_sub.suffix.lower() == ".ass":
                    target_sub = video_path.with_suffix(".ass")
                    shutil.move(str(matched_sub), str(target_sub))
                else:
                    ok = convert_subtitle_to_lrc(matched_sub, target_lrc)
                    if ok and matched_sub.exists():
                        matched_sub.unlink()
            else:
                if matched_sub.suffix.lower() == ".ass":
                    target_sub = video_path.with_suffix(".zh-CN.default.ass")
                elif matched_sub.suffix.lower() == ".vtt":
                    target_sub = video_path.with_suffix(".zh-CN.default.vtt")
                else:
                    target_sub = video_path.with_suffix(".zh-CN.default.srt")

                shutil.move(str(matched_sub), str(target_sub))

            if mp3_path.exists():
                mp3_path.unlink()
            if matched_sub and matched_sub.exists():
                matched_sub.unlink()

            job_file.unlink()
            processed_count += 1
        except Exception as e:
            logger.error("Error processing job %s: %s", job_file.name, e)

    return processed_count


def refresh_index(cfg: dict[str, Any], incremental: bool = True, track_new: bool = False) -> dict[str, Any]:
    """Scan scan_dirs and update SQLite file_index.db.

    With ``track_new`` the returned dict also carries ``new_files`` (paths that were
    absent from the index before this pass), which the periodic scan feeds into the
    auto-extraction planner. Tracking is opt-in so the index task's log stays small.
    """
    db_path = cfg.get("db_path")
    scan_dirs = cfg.get("scan_dirs", [])
    exts = set(e.lower() for e in cfg.get("index_extensions", []))
    now = int(time.time())
    new_files: list[str] = []

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            name TEXT,
            is_dir INTEGER,
            mtime INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS index_meta (\n            base_dir TEXT PRIMARY KEY,\n            last_updated TEXT,\n            file_count INTEGER\n        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_name ON files(name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_path_prefix ON files(path)")
    conn.commit()

    total_scanned = 0
    updated_records = 0

    for scan_dir in scan_dirs:
        base = Path(scan_dir).expanduser().resolve()
        if not base.exists():
            continue
        base_dir_str = str(base)

        batch = []
        scanned_paths: set[str] = set()

        for root, dirs, files in os.walk(base_dir_str):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ["@eaDir", "Temp", "tmp"]]

            for d in dirs:
                p = os.path.join(root, d)
                scanned_paths.add(p)
                if incremental:
                    cursor.execute("SELECT mtime FROM files WHERE path = ?", (p,))
                    row = cursor.fetchone()
                    try:
                        mtime = int(os.path.getmtime(p))
                    except Exception:
                        mtime = now
                    if not row or row[0] != mtime:
                        batch.append((p, d.lower(), 1, mtime))
                else:
                    batch.append((p, d.lower(), 1, now))

            for f in files:
                file_path = Path(root, f)
                if file_path.suffix.lower() not in exts:
                    continue
                p = str(file_path)
                scanned_paths.add(p)
                if incremental:
                    cursor.execute("SELECT mtime FROM files WHERE path = ?", (p,))
                    row = cursor.fetchone()
                    try:
                        mtime = int(os.path.getmtime(p))
                    except Exception:
                        mtime = now
                    if not row:
                        if track_new:
                            new_files.append(p)
                        batch.append((p, f.lower(), 0, mtime))
                    elif row[0] != mtime:
                        batch.append((p, f.lower(), 0, mtime))
                else:
                    batch.append((p, f.lower(), 0, now))

            if len(batch) >= 1000:
                cursor.executemany("REPLACE INTO files VALUES (?, ?, ?, ?)", batch)
                conn.commit()
                updated_records += len(batch)
                batch.clear()

        if batch:
            cursor.executemany("REPLACE INTO files VALUES (?, ?, ?, ?)", batch)
            conn.commit()
            updated_records += len(batch)
            batch.clear()

        prefix = base_dir_str if base_dir_str.endswith(os.sep) else base_dir_str + os.sep
        cursor.execute("SELECT path FROM files WHERE path = ? OR path LIKE ?", (base_dir_str, prefix + "%"))
        db_paths = [r[0] for r in cursor.fetchall()]
        to_delete = [(p,) for p in db_paths if p not in scanned_paths]
        if to_delete:
            cursor.executemany("DELETE FROM files WHERE path = ?", to_delete)
            conn.commit()

        total_scanned += len(scanned_paths)
        cursor.execute(
            "REPLACE INTO index_meta VALUES (?, ?, ?)",
            (base_dir_str, time.strftime("%Y-%m-%d %H:%M:%S"), len(scanned_paths)),
        )
        conn.commit()

    conn.close()
    result: dict[str, Any] = {"total_scanned": total_scanned, "updated": updated_records}
    if track_new:
        result["new_files"] = new_files
        result["new_count"] = len(new_files)
    return result


def _ensure_auto_state(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_extract (
            path TEXT PRIMARY KEY,
            size INTEGER,
            mtime INTEGER,
            status TEXT,
            reason TEXT,
            updated_at INTEGER
        )
        """
    )
    conn.commit()


def _record_auto_state(
    conn: sqlite3.Connection,
    path: Path,
    sig: tuple[int, int] | None,
    status: str,
    reason: str,
) -> None:
    conn.execute(
        "REPLACE INTO auto_extract VALUES (?, ?, ?, ?, ?, ?)",
        (str(path), sig[0] if sig else 0, sig[1] if sig else 0, status, reason, int(time.time())),
    )


def _open_auto_state(cfg: dict[str, Any]) -> sqlite3.Connection | None:
    db_path = cfg.get("db_path")
    if not db_path or not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    _ensure_auto_state(conn)
    return conn


def mark_auto_extract_status(
    cfg: dict[str, Any],
    paths: list[Path],
    status: str,
    reason: str = "",
) -> None:
    """Record the outcome for ``paths`` so the next scan does not queue them again."""
    conn = _open_auto_state(cfg)
    if conn is None:
        return
    try:
        for p in paths:
            _record_auto_state(conn, Path(p), _file_signature(Path(p)), status, reason)
        conn.commit()
    finally:
        conn.close()


def plan_auto_extraction(cfg: dict[str, Any], new_paths: list[str]) -> dict[str, Any]:
    """Decide which newly indexed media files are safe to extract.

    A candidate is queued only when it has no subtitle/lyric sidecar, carries no
    download-in-progress marker, has no existing extraction output, has been untouched
    for ``settle_seconds`` and keeps the same size/mtime across a second sample.
    Anything that is new but not yet safe is recorded as ``deferred`` in the index DB
    and re-checked by the next scan, so a file that was still being moved or written
    is picked up once it settles instead of being extracted half-written.

    Returns ``{"batches": [[Path, ...], ...], "counts": {...}, "enabled": bool}``;
    the caller submits the batches and then calls :func:`mark_auto_extract_status`
    with ``"queued"``.
    """
    opts = auto_extract_options(cfg)
    counts = {
        "new": len(new_paths),
        "queued": 0,
        "deferred": 0,
        "has_subtitle": 0,
        "in_progress": 0,
        "output_exists": 0,
        "excluded": 0,
        "missing": 0,
    }
    result: dict[str, Any] = {"batches": [], "counts": counts, "enabled": bool(opts["enabled"])}
    if not opts["enabled"]:
        return result

    conn = _open_auto_state(cfg)
    if conn is None:
        return result

    settle = max(0.0, float(opts["settle_seconds"]))
    sample_wait = max(0.0, float(opts["sample_wait_seconds"]))
    limit = max(1, int(opts["max_per_scan"]))
    batch_size = max(1, int(opts["batch_size"]))
    fmt = str(opts["format"]).lower()
    out_dir = Path(cfg["out_dir"]) if cfg.get("out_dir") else None
    exclude_paths = [str(p) for p in (opts.get("exclude_paths") or [])]

    siblings_cache: dict[str, list[Path]] = {}

    def siblings_of(p: Path) -> list[Path]:
        key = str(p.parent)
        if key not in siblings_cache:
            try:
                siblings_cache[key] = list(p.parent.iterdir())
            except OSError:
                siblings_cache[key] = []
        return siblings_cache[key]

    try:
        deferred_rows = [
            r[0] for r in conn.execute("SELECT path FROM auto_extract WHERE status = 'deferred'")
        ]
        candidates = sorted(set(new_paths) | set(deferred_rows), key=lambda s: s.lower())

        pending: list[tuple[Path, tuple[int, int]]] = []

        for raw in candidates:
            p = Path(raw)
            if not p.is_file():
                conn.execute("DELETE FROM auto_extract WHERE path = ?", (raw,))
                counts["missing"] += 1
                continue

            if is_excluded_path(p, exclude_paths):
                _record_auto_state(conn, p, _file_signature(p), "excluded", "path excluded")
                counts["excluded"] += 1
                continue

            sig = _file_signature(p)
            if sig is None:
                _record_auto_state(conn, p, sig, "deferred", "unreadable")
                counts["deferred"] += 1
                continue

            if has_subtitle_sidecar(p, siblings_of(p)):
                _record_auto_state(conn, p, sig, "has_subtitle", "subtitle/lyric sidecar present")
                counts["has_subtitle"] += 1
                continue

            if is_write_in_progress(p):
                _record_auto_state(conn, p, sig, "deferred", "download still in progress")
                counts["in_progress"] += 1
                continue

            if out_dir is not None and (out_dir / f"{p.stem}.{fmt}").is_file():
                _record_auto_state(conn, p, sig, "output_exists", "audio already extracted")
                counts["output_exists"] += 1
                continue

            age = max(0.0, time.time() - sig[1] / 1_000_000_000)
            if age < settle:
                _record_auto_state(conn, p, sig, "deferred", f"modified {age:.0f}s ago")
                counts["deferred"] += 1
                continue

            pending.append((p, sig))

        # One shared sleep for the whole scan: re-stat every plausible candidate after
        # a short interval, which catches writers that keep size or mtime unchanged
        # between two consecutive filesystem reads.
        ready: list[Path] = []
        if pending and sample_wait > 0:
            time.sleep(sample_wait)
        for p, sig in pending:
            if sample_wait > 0 and _file_signature(p) != sig:
                _record_auto_state(conn, p, sig, "deferred", "size/mtime still changing")
                counts["deferred"] += 1
                continue
            ready.append(p)

        if len(ready) > limit:
            for p in ready[limit:]:
                _record_auto_state(conn, p, _file_signature(p), "deferred", "rate limited")
                counts["deferred"] += 1
            ready = ready[:limit]

        batches = [ready[i:i + batch_size] for i in range(0, len(ready), batch_size)]
        counts["queued"] = len(ready)
        result["batches"] = batches
        conn.commit()
        return result
    finally:
        conn.close()


def list_active_jobs(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """List pending transcription jobs."""
    jobs_dir = Path(cfg["jobs_dir"])
    if not jobs_dir.exists():
        return []

    jobs = []
    for j in sorted(jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with open(j, "r", encoding="utf-8") as f:
                d = json.load(f)
            d["job_name"] = j.stem
            jobs.append(d)
        except Exception:
            jobs.append({"job_name": j.name, "error": "Failed to read"})
    return jobs
