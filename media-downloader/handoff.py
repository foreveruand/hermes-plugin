"""Encrypted archive handoff for the Hermes Media Downloader plugin.

When the bdpan backend uploads a download, it also carries a compact, copy-pasteable
handoff code (HMD1:<base64url-json>).  A different host running this plugin feeds that
code to /dl (or "cli.py import"); the archive is downloaded from Baidu Netdisk,
decrypted/extracted with the embedded password, and filed into the host's own
destinations tree.  The manifest never carries absolute local paths: it only names a
destination category (asmr/asmr_r/bilibili/pixiv/extract) plus the path prefix relative
to that category, so each host keeps its own layout.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from .config import DEFAULT_CONFIG
    from .core import (
        _staged_files,
        find_binary,
        get_subprocess_env,
        resolve_staging_dir,
        rclone_transfer,
        safe_stage_and_move,
    )
except ImportError:
    from config import DEFAULT_CONFIG
    from core import (
        _staged_files,
        find_binary,
        get_subprocess_env,
        resolve_staging_dir,
        rclone_transfer,
        safe_stage_and_move,
    )

logger = logging.getLogger("media-downloader")

MANIFEST_PREFIX = "HMD1:"
MANIFEST_VERSION = 1
_LOG_TAIL_LINES = 20
_PCT_RE = re.compile(r"(\d{1,3})%")
# A trailing archive filename ("pkg.7z", "pkg.7z.001") marks a file, not a folder.
_ARCHIVE_NAME_RE = re.compile(r"\.7z(?:\.\d+)?$", re.IGNORECASE)


class ImportDestinationRequired(RuntimeError):
    """Raised when a handoff code cannot pick a destination on this host."""


def _clean_code(code: str) -> str:
    """Strip code fences, backticks and surrounding whitespace from a pasted code."""
    text = str(code or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip().strip("`").strip()


def encode_manifest(payload: dict[str, Any]) -> str:
    """Encode a manifest payload into the HMD1:<base64url> handoff format."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{MANIFEST_PREFIX}{token}"


def is_handoff_code(text: str) -> bool:
    """Return True when text looks like a handoff code rather than a URL."""
    cleaned = _clean_code(text)
    if cleaned[: len(MANIFEST_PREFIX)].upper() == MANIFEST_PREFIX:
        return True
    return cleaned.startswith("{") and '"v"' in cleaned


def split_handoff(text: str) -> Optional[tuple[str, str]]:
    """Split a pasted handoff command into (code, remaining options).

    Accepts a bare code, a fenced block, or a forwarded "/dl HMD1:... --to ..." line.
    """
    cleaned = _clean_code(text)
    if cleaned.lower().startswith(("dl ", "/dl ")):
        cleaned = cleaned.split(None, 1)[1].strip()
    if not is_handoff_code(cleaned):
        return None
    if cleaned.startswith("{"):
        return cleaned, ""
    parts = cleaned.split(None, 1)
    return parts[0], (parts[1].strip() if len(parts) > 1 else "")


def decode_manifest(code: str) -> dict[str, Any]:
    """Decode and validate a handoff code, raising ValueError when it is unusable."""
    text = _clean_code(code)
    try:
        if text.startswith("{"):
            payload = json.loads(text)
        else:
            if text[: len(MANIFEST_PREFIX)].upper() != MANIFEST_PREFIX:
                raise ValueError("不是有效的入库指令（应以 HMD1: 开头）")
            token = text[len(MANIFEST_PREFIX):].strip()
            padding = "=" * (-len(token) % 4)
            payload = json.loads(base64.urlsafe_b64decode(token + padding).decode("utf-8"))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"入库指令解析失败: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("入库指令内容不是对象")
    try:
        version = int(payload.get("v") or 0)
    except (TypeError, ValueError):
        version = 0
    if version != MANIFEST_VERSION:
        raise ValueError(f"不支持的入库指令版本: {payload.get('v')}")

    engine = str(payload.get("engine") or "bdpan").strip().lower()
    if engine != "bdpan":
        raise ValueError(f"不支持的传输引擎: {engine}（当前仅支持 bdpan）")
    payload["engine"] = engine

    for key, label in (("remote", "网盘路径"), ("name", "归档文件名"), ("password", "解压密码")):
        if not str(payload.get(key) or "").strip():
            raise ValueError(f"入库指令缺少{label}")

    volumes = payload.get("volumes")
    if volumes is not None:
        if not isinstance(volumes, list) or not volumes:
            raise ValueError("入库指令的分卷列表无效")
        cleaned: list[str] = []
        for item in volumes:
            name = str(item or "").strip()
            if not name or name in (".", "..") or "/" in name or "\\" in name:
                raise ValueError(f"入库指令包含非法分卷名: {item!r}")
            cleaned.append(name)
        payload["volumes"] = cleaned
    return payload


def relative_destination(destination: str, cfg: dict[str, Any],
                         internal_prefix: tuple[str, ...] = ()) -> tuple[str, str]:
    """Split a destination into (category key, path relative to that category root).

    internal_prefix is the directory structure already packed inside the archive; it is
    removed from the relative path so the receiver knows which prefix to create before
    extracting.  A destination outside every configured root yields ("", "") and forces
    the receiver to choose with --to.
    """
    dest = str(destination or "").strip().rstrip("/")
    destinations = cfg.get("destinations") or DEFAULT_CONFIG["destinations"]
    best_key, best_root = "", ""
    for key, value in destinations.items():
        root = str(value or "").strip().rstrip("/")
        if not root:
            continue
        if dest == root or dest.startswith(root + "/"):
            if len(root) > len(best_root):
                best_key, best_root = key, root
    if not best_key:
        return "", ""

    parts = [p for p in dest[len(best_root):].strip("/").split("/") if p]
    prefix = [p for p in internal_prefix if p]
    if prefix and len(parts) >= len(prefix) and parts[-len(prefix):] == prefix:
        parts = parts[:-len(prefix)]
    return best_key, "/".join(parts)


def build_manifest(transfer: dict[str, Any], destination: str, cfg: dict[str, Any],
                   internal_prefix: tuple[str, ...] = (), title: str = "") -> str:
    """Build the handoff code for a completed bdpan transfer."""
    category, sub = relative_destination(destination, cfg, internal_prefix)
    volumes = [str(v).strip() for v in (transfer.get("volumes") or []) if str(v).strip()]
    remote_folder = str(transfer.get("remote_folder") or "").strip().strip("/")
    # Prefer the exact path accepted by "bdpan upload" (relative to /apps/bdpan/);
    # the API's saved_path is display-oriented and may not be downloadable as-is.
    remote = str(transfer.get("remote_rel") or transfer.get("remote_path")
                 or transfer.get("saved_path") or "").strip()
    if volumes and remote_folder:
        # Multi-volume packages live in one folder; the receiver downloads each part.
        remote = remote_folder
    saved = str(transfer.get("saved_path") or "").strip()
    payload: dict[str, Any] = {
        "v": MANIFEST_VERSION,
        "engine": "bdpan",
        "remote": remote,
        "name": str(transfer.get("archive_name") or Path(remote).name),
        "password": str(transfer.get("password") or ""),
        "cat": category,
        "sub": sub,
        "title": str(title or "").strip()[:120],
        "ts": int(time.time()),
    }
    if volumes:
        payload["volumes"] = volumes
    if remote_folder:
        payload["remote_folder"] = remote_folder
    prefix = "/".join(p for p in internal_prefix if p)
    if prefix:
        payload["prefix"] = prefix
    if saved and saved != remote:
        payload["saved"] = saved
    if transfer.get("archive_size"):
        payload["size"] = int(transfer["archive_size"])
    return encode_manifest(payload)


def attach_manifest(transfer: dict[str, Any], destination: str, cfg: dict[str, Any],
                    internal_prefix: tuple[str, ...] = (), title: str = "") -> dict[str, Any]:
    """Attach a handoff code to a bdpan transfer summary (best effort)."""
    if transfer.get("engine") != "bdpan":
        return transfer
    try:
        transfer["manifest"] = build_manifest(transfer, destination, cfg,
                                              internal_prefix=internal_prefix, title=title)
    except Exception:
        logger.warning("failed to build handoff manifest for %s", destination, exc_info=True)
    return transfer


def _safe_sub(value: Any) -> str:
    """Normalize a relative import prefix, rejecting traversal components."""
    parts: list[str] = []
    for part in str(value or "").replace("\\", "/").split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError("入库指令包含非法路径（..）")
        parts.append(part)
    return "/".join(parts)


def is_remote_destination(value: Any) -> bool:
    """True when a destination is an rclone spec such as remote:path."""
    root = str(value or "").strip()
    return ":" in root and not root.startswith("/")


def join_destination(root: str, sub: str = "") -> str:
    """Join a category root (local path or rclone spec) with a relative prefix."""
    base = str(root or "").rstrip("/")
    if not sub:
        return base
    return f"{base}/{str(sub).strip('/')}"


def _destination_root(value: Any, key: str) -> str:
    """Return a category root, expanding local paths but preserving rclone specs."""
    root = str(value or "").strip().rstrip("/")
    if not root:
        raise ValueError(f"destinations.{key} 未配置")
    if is_remote_destination(root):
        return root
    return str(Path(root).expanduser())


def destination_menu(cfg: dict[str, Any]) -> str:
    """Render the configured destination categories for an interactive reply."""
    destinations = cfg.get("destinations") or DEFAULT_CONFIG["destinations"]
    lines = []
    for key, value in destinations.items():
        root = str(value or "").strip()
        if not root:
            continue
        suffix = "（rclone 远端）" if is_remote_destination(root) else ""
        lines.append(f"• `{key}` → `{root}`{suffix}")
    return "\n".join(lines) or "• （本机未配置任何 destinations）"


def resolve_import_destination(manifest: dict[str, Any], cfg: dict[str, Any],
                               override: str = "") -> tuple[str, str, str]:
    """Resolve (local root, category label, sub path) for an incoming archive.

    An override wins over the manifest: it may be a configured category key or an
    absolute local path.  Without a usable category the caller gets
    ImportDestinationRequired carrying the choice menu.
    """
    destinations = cfg.get("destinations") or DEFAULT_CONFIG["destinations"]
    sub = _safe_sub(manifest.get("sub"))
    choice = str(override or "").strip()

    if choice:
        key = choice
        if key in destinations:
            return _destination_root(destinations[key], key), key, sub
        path = Path(key).expanduser()
        if path.is_absolute():
            return str(path), "custom", sub
        if is_remote_destination(key):
            return key, "custom", sub
        raise ValueError("--to 需为分类名（" + "/".join(destinations) + "）、绝对路径或 rclone 远端")

    category = str(manifest.get("cat") or "").strip()
    if category in destinations:
        try:
            root = _destination_root(destinations[category], category)
        except ValueError as exc:
            raise ImportDestinationRequired(
                f"{exc}\n\n请用 --to 指定其他分类或绝对路径：\n{destination_menu(cfg)}"
            ) from exc
        return root, category, sub

    if category:
        reason = f"本机未配置 destinations.{category}"
    else:
        reason = "入库包未指定入库分类"
    raise ImportDestinationRequired(
        f"{reason}，请用 --to 选择目标目录：\n\n{destination_menu(cfg)}\n\n"
        f"用法: `/dl <入库指令> --to <分类|路径|远端>`"
    )


def _manifest_candidates(manifest: dict[str, Any]) -> list[str]:
    """Ordered remote paths to try with bdpan download."""
    remote = str(manifest.get("remote") or "").strip()
    saved = str(manifest.get("saved") or "").strip()
    candidates: list[str] = []
    if saved.startswith("/"):
        candidates.append(saved)
    if remote:
        candidates.append(remote if remote.startswith("/") else f"/apps/bdpan/{remote.lstrip('/')}")
        candidates.append(remote)
    out: list[str] = []
    for item in candidates:
        if item and item not in out:
            out.append(item)
    return out


def _run_stream(cmd: list[str], timeout_seconds: int, progress_cb: Optional[Callable[[str], None]],
                label: str) -> dict[str, Any]:
    """Run a command, forwarding percent progress and returning a short log tail."""
    try:
        proc = subprocess.Popen(cmd, env=get_subprocess_env(None), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    except FileNotFoundError as exc:
        return {"ok": False, "code": 127, "log": f"未找到可执行文件: {exc}", "progress": ""}

    stdout = proc.stdout
    assert stdout is not None
    other_lines: list[str] = []
    state = {"progress": ""}

    def _handle(segment: str) -> None:
        segment = segment.strip()
        if not segment:
            return
        match = _PCT_RE.search(segment)
        if match:
            state["progress"] = f"{match.group(1)}%"
            if progress_cb:
                try:
                    progress_cb(f"{label} {match.group(1)}%")
                except Exception:
                    logger.debug("import progress callback failed", exc_info=True)
            return
        other_lines.append(segment)

    def _reader() -> None:
        buf = ""
        while True:
            try:
                chunk = stdout.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            parts = re.split(r"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                _handle(part)
        if buf:
            _handle(buf)

    reader = threading.Thread(target=_reader, daemon=True, name="media-import-reader")
    reader.start()

    timed_out = False
    try:
        proc.wait(timeout=max(60, int(timeout_seconds)))
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    reader.join(timeout=5)

    log = "\n".join(other_lines[-_LOG_TAIL_LINES:])
    if timed_out:
        note = f"{label}超时（>{timeout_seconds}s）"
        log = f"{log}\n{note}" if log else note
    return {"ok": proc.returncode == 0 and not timed_out, "code": proc.returncode,
            "log": log, "progress": state["progress"]}


def _bdpan_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG["bdpan"])
    merged.update(cfg.get("bdpan") or {})
    return merged


def _find_downloaded(root: Path, name: str) -> Optional[Path]:
    """Locate the archive bdpan wrote, tolerating its directory layout choices."""
    exact = root / name
    if exact.is_file():
        return exact
    files = [p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".part")]
    if not files:
        return None
    named = [p for p in files if p.name == name]
    if named:
        return named[0]
    archives = [p for p in files if p.suffix.lower() == ".7z"]
    pool = archives or files
    return max(pool, key=lambda p: p.stat().st_size)


def _as_folder(value: Any) -> str:
    """Return the folder part of a remote path, or ``""`` for a bare filename."""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if _ARCHIVE_NAME_RE.search(text):
        if "/" not in text:
            return ""
        text = text.rsplit("/", 1)[0]
    return text


def _remote_roots(manifest: dict[str, Any]) -> list[str]:
    """Remote directories (relative to /apps/bdpan/ or absolute) that hold the volumes."""
    roots: list[str] = []
    for raw in (_as_folder(manifest.get("remote_folder")),
                _as_folder(manifest.get("remote")),
                _as_folder(manifest.get("saved"))):
        if not raw:
            continue
        if raw.startswith("/"):
            candidates = [raw, raw.lstrip("/")]
        else:
            candidates = [raw, f"/apps/bdpan/{raw}"]
        for candidate in candidates:
            candidate = candidate.rstrip("/")
            if candidate and candidate not in roots:
                roots.append(candidate)
    return roots


def _volume_remote_candidates(manifest: dict[str, Any], volume: str) -> list[str]:
    """Ordered remote file paths to try for one volume of a split package."""
    out: list[str] = []
    for root in _remote_roots(manifest):
        candidate = f"{root}/{volume}"
        if candidate not in out:
            out.append(candidate)
    return out


def _download_volume_set(manifest: dict[str, Any], volumes: list[str], download_dir: Path,
                         cfg: dict[str, Any],
                         progress_cb: Optional[Callable[[str], None]]) -> tuple[Path, str, str]:
    """Download every volume into one directory, in order, retrying each part."""
    bcfg = _bdpan_settings(cfg)
    bdpan_bin = find_binary(str(bcfg.get("bin", "bdpan")), cfg)
    timeout = max(60, int(bcfg.get("timeout_seconds", 7200)))
    try:
        retries = max(1, int(bcfg.get("upload_retries", 3) or 1))
    except (TypeError, ValueError):
        retries = 3

    # 7z x on the first volume discovers the remaining parts in the same directory,
    # so a split package must land in one folder with its original names.
    target_dir = download_dir / "volumes"
    target_dir.mkdir(parents=True, exist_ok=True)
    used_remote = ""
    logs: list[str] = []
    total = len(volumes)

    for index, volume in enumerate(volumes, 1):
        candidates = _volume_remote_candidates(manifest, volume)
        if not candidates:
            raise RuntimeError(f"入库指令未给出分卷 {volume} 的网盘目录")
        local_file = target_dir / volume
        errors: list[str] = []
        res: dict[str, Any] = {}
        done = False
        for remote in candidates:
            for attempt in range(1, retries + 1):
                if progress_cb:
                    note = f"下载分卷 ({index}/{total}) {volume}"
                    if attempt > 1:
                        note += f" 重试 {attempt}/{retries}"
                    try:
                        progress_cb(note)
                    except Exception:
                        logger.debug("import progress callback failed", exc_info=True)
                res = _run_stream(
                    [bdpan_bin, "download", remote, str(local_file), "--no-check-update"],
                    timeout, progress_cb, "下载")
                found = next((p for p in target_dir.rglob(volume) if p.is_file()), None)
                if res["ok"] and found and found.stat().st_size > 0:
                    if found != local_file:
                        shutil.move(str(found), str(local_file))
                    used_remote = remote
                    done = True
                    break
                if res["ok"]:
                    errors.append(f"{remote}: 下载成功但未找到分卷 {volume}")
                else:
                    errors.append(f"{remote}: {res['log'] or '下载失败'}")
                for stale in (local_file, Path(f"{local_file}.part")):
                    try:
                        if stale.exists():
                            stale.unlink()
                    except OSError:
                        logger.debug("failed to remove partial volume %s", stale, exc_info=True)
            if done:
                break
        if not done:
            detail = "\n".join(errors[-3:]) or "(无详细错误输出)"
            raise RuntimeError(
                f"bdpan 分卷下载失败（分卷 {index}/{total}：{volume}）：\n{detail}")
        if res.get("log"):
            logs.append(str(res["log"]))

    return target_dir / volumes[0], used_remote, "\n".join(logs)


def _download_archive(manifest: dict[str, Any], download_dir: Path, cfg: dict[str, Any],
                      progress_cb: Optional[Callable[[str], None]]) -> tuple[Path, str, str]:
    """Download the archive over bdpan, trying the remote path variants in order.

    Split manifests carry a ``volumes`` list: every part is fetched into a shared
    directory and the first volume is returned for extraction.
    """
    volumes = [str(v).strip() for v in (manifest.get("volumes") or []) if str(v).strip()]
    if volumes:
        return _download_volume_set(manifest, volumes, download_dir, cfg, progress_cb)

    bcfg = _bdpan_settings(cfg)
    bdpan_bin = find_binary(str(bcfg.get("bin", "bdpan")), cfg)
    timeout = max(60, int(bcfg.get("timeout_seconds", 7200)))
    name = str(manifest.get("name") or "")
    attempts: list[str] = []

    for index, remote in enumerate(_manifest_candidates(manifest)):
        attempt_dir = download_dir / f"try{index}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        # bdpan treats <local> as the target file path, not a directory: it
        # downloads to "<local>.part" then renames onto <local>, which fails
        # with EEXIST when <local> is the pre-created attempt directory.
        local_file = attempt_dir / (name or Path(remote).name or "archive")
        res = _run_stream([bdpan_bin, "download", remote, str(local_file), "--no-check-update"],
                          timeout, progress_cb, "下载")
        if res["ok"]:
            found = _find_downloaded(attempt_dir, name)
            if found:
                return found, remote, res["log"]
            attempts.append(f"{remote}: 下载成功但未找到归档文件")
        else:
            attempts.append(f"{remote}: {res['log'] or '下载失败'}")
        shutil.rmtree(attempt_dir, ignore_errors=True)

    detail = "\n".join(attempts[-3:]) or "(无详细错误输出)"
    raise RuntimeError(f"bdpan 下载失败：\n{detail}")


def _extract_archive(archive: Path, extract_dir: Path, password: str, cfg: dict[str, Any],
                     progress_cb: Optional[Callable[[str], None]]) -> str:
    """Extract the encrypted 7z, raising when the password is wrong or output is empty.

    For a split package ``archive`` is the first volume (``.7z.001``); 7z then reads
    the following ``.7z.002``, ``.7z.003`` ... parts from the same directory.
    """
    bcfg = _bdpan_settings(cfg)
    seven_zip_bin = find_binary(str(bcfg.get("seven_zip_bin", "7z")), cfg)
    timeout = max(60, int(bcfg.get("timeout_seconds", 7200)))
    res = _run_stream(
        [seven_zip_bin, "x", str(archive), f"-o{extract_dir}", f"-p{password}", "-y", "-bsp1"],
        timeout, progress_cb, "解压")
    if not res["ok"]:
        detail = res["log"] or "(无详细错误输出)"
        raise RuntimeError(f"7z 解压失败（退出码 {res['code']}，疑似密码错误）：\n{detail}")
    if not _staged_files(extract_dir):
        raise RuntimeError("解压后没有文件（归档为空或密码错误）")
    return res["log"]


def run_import_target(code: str, cfg: dict[str, Any], category: str = "",
                      progress_cb: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """Download, decrypt, extract and file one handoff code into the local library."""
    manifest = decode_manifest(code)
    root, label, sub = resolve_import_destination(manifest, cfg, override=category)
    prefix = _safe_sub(manifest.get("prefix"))
    remote_target = is_remote_destination(root)
    target = join_destination(root, sub)
    # The archive already contains its internal structure; report the deepest
    # directory that actually received files, not just the category root.
    display = join_destination(target, prefix)

    staging = resolve_staging_dir(cfg)
    download_dir = staging / "download"
    extract_dir = staging / "extract"
    download_dir.mkdir(parents=True, exist_ok=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    log_parts = [
        f"Package: {manifest.get('name')}",
        f"Remote: {manifest.get('remote') or manifest.get('saved')}",
        f"Destination: {display}",
    ]
    volumes = [str(v) for v in (manifest.get("volumes") or []) if str(v).strip()]
    if len(volumes) > 1:
        log_parts.insert(1, f"Volumes: {len(volumes)}")
    try:
        archive, used_remote, download_log = _download_archive(
            manifest, download_dir, cfg, progress_cb)
        size = archive.stat().st_size if archive.exists() else 0
        log_parts.append(f"Downloaded: {archive.name} ({size} bytes) via {used_remote}")
        if download_log:
            log_parts.append(download_log)

        extract_log = _extract_archive(
            archive, extract_dir, str(manifest.get("password") or ""), cfg, progress_cb)
        if extract_log:
            log_parts.append(extract_log)

        staged = _staged_files(extract_dir)
        total = len(staged)

        if remote_target:
            res = rclone_transfer(extract_dir, target, cfg, progress_cb=progress_cb)
            if not res["ok"]:
                fallback_root = Path(cfg.get("staging_fallback", DEFAULT_CONFIG["staging_fallback"]))
                preserved_dir = fallback_root / f"import-{Path(str(manifest.get('name') or 'archive')).stem}"
                preserved = 0
                try:
                    preserved = len(safe_stage_and_move(extract_dir, preserved_dir))
                except Exception:
                    logger.debug("failed to park extracted files after rclone error", exc_info=True)
                failed = res["errors"] if res["errors"] is not None else len(staged)
                done = res["moved"] if res["moved"] is not None else max(0, total - failed)
                reason = res["log"] or "(无详细错误输出)"
                raise RuntimeError(
                    f"rclone 入库传输失败（成功 {done}/{total}，失败 {failed}）：\n{reason}\n"
                    f"已保留解压文件到 {preserved_dir}（{preserved} 个）"
                )
            moved_count = int(res["moved"]) if res["moved"] is not None else total
            skipped = max(0, total - moved_count)
            files = [join_destination(target, p.relative_to(extract_dir).as_posix())
                     for p in staged]
            destination = display
        else:
            moved = safe_stage_and_move(extract_dir, Path(target))
            moved_count = len(moved)
            skipped = max(0, total - moved_count)
            files = [str(p) for p in moved]
            destination = display if Path(display).exists() else target
            if total and not moved:
                log_parts.append("目标目录中已存在全部文件，未覆盖")

        log_parts.append(f"Extracted: {total} file(s); imported {moved_count} (skipped {skipped})")

        return {
            "url": f"bdpan:{manifest.get('name')}",
            "category": label,
            "destination": destination,
            "files": files,
            "count": moved_count,
            "success": True,
            "transfer": {
                "engine": "bdpan-import",
                "archive_name": str(manifest.get("name") or ""),
                "remote": used_remote,
                "password": str(manifest.get("password") or ""),
                "destination": target,
                "extracted": total,
                "moved": moved_count,
                "skipped": skipped,
                "volumes": volumes,
                "volume_count": len(volumes),
            },
            "log": "\n".join(log_parts),
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)
