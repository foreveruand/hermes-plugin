"""Baidu Netdisk (bdpan) upload backend for the Hermes Media Downloader plugin.

Used when ``upload_mode == "bdpan"``. Completed downloads are packed into a single
7z archive with AES-256 data encryption and header/filename encryption (``-mhe=on``),
optionally split into fixed-size volumes (``-v``), then uploaded with the ``bdpan``
CLI (paths are scoped under ``/apps/bdpan/``).

The outer archive name is neutral by default (``pkg_YYYYMMDD_xxxxxx.7z``): netdisk
keyword review must never see the work title or circle. Every volume is uploaded
independently, so a broken transfer retries one part instead of the whole package.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import string
import subprocess
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from .config import DEFAULT_CONFIG
    from .core import _staged_files, find_binary, get_subprocess_env, safe_stage_and_move
except ImportError:
    from config import DEFAULT_CONFIG
    from core import _staged_files, find_binary, get_subprocess_env, safe_stage_and_move

logger = logging.getLogger("media-downloader")

_LOG_TAIL_LINES = 20
_ENCRYPTION_LABEL = "AES-256 + 文件名加密(-mhe=on)"
# Pause between upload attempts of the same volume (tests set this to 0).
_UPLOAD_RETRY_DELAY_SECONDS = 3

# Keep CJK and other word characters; collapse everything else into separators.
_SLUG_RE = re.compile(r"[^\w\-. ]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_PCT_RE = re.compile(r"(\d{1,3})%")
# 7z volume suffixes: .7z.001, .7z.010, ... and the unsplit .7z itself.
_VOLUME_PART_RE = re.compile(r"\.7z(?:\.(\d+))?$", re.IGNORECASE)
# Values that explicitly disable volume splitting.
_VOLUME_DISABLED = {"", "0", "none", "disabled", "off", "false"}


def _tail(text: str, limit: int = _LOG_TAIL_LINES) -> str:
    lines = [ln for ln in str(text).splitlines() if ln.strip()]
    return "\n".join(lines[-limit:])


def random_password(length: int = 20) -> str:
    """Generate an argv-safe random password (alphanumerics only)."""
    length = max(8, int(length))
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def make_archive_slug(title: str, fallback: str = "media") -> str:
    """Turn a title / circle into a filesystem- and netdisk-safe readable slug."""
    cleaned = unicodedata.normalize("NFKC", str(title or "")).strip()
    cleaned = _SLUG_RE.sub("_", cleaned)
    cleaned = _WS_RE.sub("_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._- ")
    if not cleaned:
        cleaned = _SLUG_RE.sub("_", str(fallback or "media"))
        cleaned = _WS_RE.sub("_", cleaned)
        cleaned = re.sub(r"_+", "_", cleaned).strip("._- ") or "media"
    return cleaned[:50].strip("._- ") or "media"


def build_archive_name(slug: str, suffix_len: int = 6) -> str:
    """Append a random hex suffix to the slug, e.g. ``aoi_kasumi_a3f9c2.7z``."""
    suffix_len = max(2, int(suffix_len))
    token = secrets.token_hex((suffix_len + 1) // 2)[:suffix_len]
    return f"{slug}_{token}.7z"


def _ascii_prefix(prefix: str) -> str:
    """Reduce ``prefix`` to plain ASCII word characters (no metadata can slip in)."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", str(prefix or ""))
    return cleaned or "pkg"


def build_obfuscated_archive_name(prefix: str = "pkg", suffix_len: int = 6) -> str:
    """Build a neutral archive name: ``{prefix}_{YYYYMMDD}_{token}.7z``.

    The name carries nothing but the configured prefix, today's date and a random hex
    token, so netdisk keyword review cannot match a work title or circle. Filesystem
    and netdisk safe: ASCII only, no spaces, no CJK.
    """
    suffix_len = max(2, int(suffix_len))
    token = secrets.token_hex((suffix_len + 1) // 2)[:suffix_len]
    stamp = datetime.now().strftime("%Y%m%d")
    return f"{_ascii_prefix(prefix)}_{stamp}_{token}.7z"


def normalize_volume_size(value: Any) -> str:
    """Return a 7z volume size (``2g`` / ``500m``) or ``""`` when splitting is off."""
    text = str(value or "").strip().lower().replace(" ", "")
    return "" if text in _VOLUME_DISABLED else text


def find_archive_volumes(archive_path: Path) -> list[Path]:
    """Return the files 7z produced for ``archive_path`` in natural volume order.

    A split run writes ``name.7z.001``, ``name.7z.002``, ... and never the unsplit
    ``name.7z``; an unsplit run writes just ``name.7z``. Ordering is numeric so
    ``.7z.010`` follows ``.7z.009`` instead of sorting lexicographically.
    """
    archive_path = Path(archive_path)
    parent = archive_path.parent
    if not parent.is_dir():
        return []
    base = archive_path.name
    found = [p for p in parent.glob(f"{base}*") if p.is_file() and p.name.startswith(base)]

    def _key(path: Path) -> tuple[int, str]:
        match = _VOLUME_PART_RE.search(path.name)
        part = match.group(1) if match else None
        return (int(part) if part else -1, path.name)

    return sorted(found, key=_key)


def unique_archive_path(parent: Path, name: str) -> Path:
    """Return a non-colliding path for ``name`` under ``parent``."""
    candidate = Path(parent) / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    counter = 0
    while True:
        counter += 1
        candidate = Path(parent) / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate


def _resolve_bdpan_config(cfg: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(DEFAULT_CONFIG["bdpan"])
    overrides = cfg.get("bdpan") or {}
    if isinstance(overrides, dict):
        resolved.update({k: v for k, v in overrides.items() if v is not None})
    return resolved


def create_encrypted_archive(assembly_dir: Path, archive_path: Path, password: str,
                             seven_zip_bin: str, compress_level: int = 9,
                             timeout_seconds: int = 7200,
                             progress_cb: Optional[Callable[[str], None]] = None,
                             extra_flags: Optional[list[str]] = None,
                             volume_size: str = "") -> str:
    """Pack ``assembly_dir`` into ``archive_path`` with AES-256 + header encryption.

    ``volume_size`` (7z syntax, e.g. ``2g``) splits the archive into ``.7z.001``,
    ``.7z.002``, ... — 7z then never writes the unsplit file. Callers enumerate the
    parts with :func:`find_archive_volumes`.

    Returns the captured (non-progress) log tail; raises ``RuntimeError`` on failure.
    """
    children = [c.name for c in sorted(assembly_dir.iterdir())]
    if not children:
        raise RuntimeError("assembly 为空，无可压缩内容")

    volume = normalize_volume_size(volume_size)
    cmd = [
        seven_zip_bin,
        "a",
        "-t7z",
        "-m0=lzma2",
        f"-mx={int(compress_level)}",
        "-mhe=on",
        f"-p{password}",
        "-bsp1",
        "-y",
    ]
    if volume:
        cmd.append(f"-v{volume}")
    for flag in extra_flags or []:
        cmd.append(str(flag))
    cmd.extend([str(archive_path), *children])

    try:
        proc = subprocess.Popen(cmd, cwd=str(assembly_dir), env=get_subprocess_env(None),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError as exc:
        raise RuntimeError(f"未找到 7z 可执行文件（{seven_zip_bin}）：{exc}") from exc

    stdout = proc.stdout
    assert stdout is not None
    other_lines: list[str] = []

    def _handle(segment: str) -> None:
        segment = segment.strip()
        if not segment:
            return
        m = _PCT_RE.search(segment)
        if m and progress_cb:
            try:
                progress_cb(f"{m.group(1)}%")
            except Exception:
                logger.debug("7z progress callback failed", exc_info=True)
        if not m:
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

    reader = threading.Thread(target=_reader, daemon=True, name="bdpan-7z-reader")
    reader.start()

    timed_out = False
    try:
        proc.wait(timeout=max(60, int(timeout_seconds)))
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    reader.join(timeout=5)

    log = _tail("\n".join(other_lines))
    if timed_out:
        raise RuntimeError(f"7z 压缩超时（>{timeout_seconds}s）")
    if proc.returncode != 0:
        detail = f"\n{log}" if log else ""
        raise RuntimeError(f"7z 压缩失败（退出码 {proc.returncode}）{detail}")
    return log


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    """Parse the first JSON object found in ``text`` (tolerating surrounding output)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def bdpan_upload(archive_path: Path, remote_rel: str, cfg: dict[str, Any],
                 timeout_seconds: int = 7200) -> dict[str, Any]:
    """Upload one file via the bdpan CLI and normalize its JSON result."""
    bcfg = _resolve_bdpan_config(cfg)
    bdpan_bin = find_binary(str(bcfg.get("bin", "bdpan")), cfg)

    cmd = [bdpan_bin, "upload", str(archive_path), remote_rel, "--json", "--no-check-update"]
    agentname = str(bcfg.get("agentname") or "").strip()
    if agentname:
        cmd.extend(["--agentname", agentname])

    try:
        res = subprocess.run(cmd, env=get_subprocess_env(None), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace",
                             timeout=max(60, int(timeout_seconds)))
    except FileNotFoundError as exc:
        raise RuntimeError(f"未找到 bdpan 可执行文件（{bdpan_bin}）：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"bdpan 上传超时（>{timeout_seconds}s）") from exc

    out = res.stdout or ""
    obj = _extract_json(out)
    if obj is None:
        hint = "（疑似 bdpan 未登录或 Token 过期，请先完成登录）" if res.returncode != 0 else ""
        detail = _tail(out)
        return {
            "ok": False,
            "log": (detail or f"bdpan 返回非 JSON 输出（退出码 {res.returncode}）") + hint,
            "code": None,
        }

    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    code = obj.get("code", 0 if isinstance(obj.get("data"), dict) else -1)
    ok = code == 0 and res.returncode == 0
    saved = data.get("saved_path") or data.get("remote_path") or data.get("path") or ""
    return {
        "ok": ok,
        "code": code,
        "saved_path": saved,
        "remote_path": data.get("remote_path") or data.get("remote") or remote_rel,
        "return_url": data.get("return_url") or "",
        "return_markdown": data.get("return_markdown") or "",
        "fsid": data.get("fsid") or data.get("fs_id") or "",
        "log": _tail(out),
    }


def _park_volumes(volumes: list[Path], preserved_dir: Path, password: str,
                  password_name: str) -> list[Path]:
    """Move every generated volume into ``preserved_dir`` and drop a password sidecar."""
    preserved_dir.mkdir(parents=True, exist_ok=True)
    kept: list[Path] = []
    for volume in volumes:
        try:
            destination = preserved_dir / volume.name
            if destination.exists():
                destination.unlink()
            shutil.move(str(volume), str(destination))
            kept.append(destination)
        except OSError:
            logger.debug("failed to preserve volume %s", volume, exc_info=True)
    try:
        (preserved_dir / f"{password_name}.password.txt").write_text(password + "\n",
                                                                    encoding="utf-8")
    except OSError:
        logger.debug("failed to write password sidecar", exc_info=True)
    return kept


def bdpan_archive_transfer(staging_dir: Path, slug_hint: str,
                           cfg: dict[str, Any], internal_prefix: tuple[str, ...] = (),
                           progress_cb: Optional[Callable[[str], None]] = None
                           ) -> tuple[list[str], str, dict[str, Any]]:
    """Compress staged files into an encrypted 7z and upload it to Baidu Netdisk.

    The archive is split into ``volume_size`` parts (7z ``-v``) and every part is
    uploaded separately under ``<remote_dir>/<archive_stem>/``, retried
    ``upload_retries`` times. Returns ``(files, log, transfer)`` mirroring
    ``archive_staged_files``. Raises ``RuntimeError`` on failure while preserving the
    completed download: a failed 7z run parks the plaintext files, a failed upload
    keeps every encrypted volume + the password sidecar.
    """
    bcfg = _resolve_bdpan_config(cfg)
    staged = _staged_files(staging_dir)
    if not staged:
        raise RuntimeError(f"无文件可打包：暂存目录为空 {staging_dir}")

    work_root = staging_dir.parent
    assembly_dir = work_root / f".bdpan-asm-{staging_dir.name}"
    suffix_len = int(bcfg.get("archive_suffix_len", 6) or 6)
    if bool(bcfg.get("obfuscate_filename", True)):
        # Neutral name: the title/circle stays inside the encrypted payload only.
        archive_name = build_obfuscated_archive_name(
            prefix=str(bcfg.get("archive_name_prefix", "pkg") or "pkg"),
            suffix_len=suffix_len)
    else:
        slug = make_archive_slug(slug_hint, fallback=staging_dir.name or "media")
        archive_name = build_archive_name(slug, suffix_len)
    archive_path = unique_archive_path(work_root, archive_name)
    password = random_password(int(bcfg.get("password_length", 20)))
    seven_zip_bin = find_binary(str(bcfg.get("seven_zip_bin", "7z")), cfg)
    try:
        timeout_seconds = max(60, int(bcfg.get("timeout_seconds", 7200)))
    except (TypeError, ValueError):
        timeout_seconds = 7200
    try:
        upload_retries = max(1, int(bcfg.get("upload_retries", 3) or 1))
    except (TypeError, ValueError):
        upload_retries = 3
    volume_size = normalize_volume_size(bcfg.get("volume_size", ""))
    internal_structure = "/".join(p for p in internal_prefix if p)
    fallback_root = Path(cfg.get("staging_fallback", DEFAULT_CONFIG["staging_fallback"]))

    try:
        target = assembly_dir.joinpath(*internal_prefix) if internal_prefix else assembly_dir
        target.mkdir(parents=True, exist_ok=True)
        safe_stage_and_move(staging_dir, target)

        try:
            create_encrypted_archive(assembly_dir, archive_path, password, seven_zip_bin,
                                     int(bcfg.get("compress_level", 9)), timeout_seconds,
                                     progress_cb=progress_cb,
                                     extra_flags=list(bcfg.get("seven_zip_flags") or []),
                                     volume_size=volume_size)
        except Exception as exc:
            preserved_dir = fallback_root / staging_dir.name
            preserved = 0
            try:
                preserved = len(safe_stage_and_move(assembly_dir, preserved_dir))
            except Exception:
                logger.debug("failed to park plaintext staging after 7z error", exc_info=True)
            raise RuntimeError(
                f"7z 压缩失败，明文已暂存到本地 {preserved_dir}（{preserved} 个文件）：{exc}"
            ) from exc

        volumes = find_archive_volumes(archive_path)
        if not volumes:
            raise RuntimeError(
                f"7z 未生成归档文件（{archive_path.name}）：请检查 seven_zip_bin 与输出路径")

        # Scheme A: one folder per archive, so every volume of a package stays together.
        archive_stem = archive_path.stem
        remote_folder = "/".join(
            x for x in [str(bcfg.get("remote_dir", "")).strip().strip("/"), archive_stem] if x
        )
        total_volumes = len(volumes)
        results: list[dict[str, Any]] = []
        for index, volume in enumerate(volumes, 1):
            volume_remote_rel = "/".join(x for x in [remote_folder, volume.name] if x)
            res: dict[str, Any] = {}
            for attempt in range(1, upload_retries + 1):
                if progress_cb:
                    note = f"上传分卷 ({index}/{total_volumes}) {volume.name}"
                    if attempt > 1:
                        note += f" 重试 {attempt}/{upload_retries}"
                    try:
                        progress_cb(note)
                    except Exception:
                        logger.debug("upload progress callback failed", exc_info=True)
                try:
                    res = bdpan_upload(volume, volume_remote_rel, cfg, timeout_seconds)
                except Exception as exc:  # timeout / missing binary: retry the part
                    res = {"ok": False, "log": str(exc)}
                if res.get("ok"):
                    results.append(res)
                    break
                if attempt < upload_retries:
                    time.sleep(_UPLOAD_RETRY_DELAY_SECONDS)
            else:
                reason = res.get("log") or "(无详细错误输出)"
                preserved_dir = fallback_root / staging_dir.name
                kept = _park_volumes(volumes, preserved_dir, password, archive_path.name)
                raise RuntimeError(
                    f"bdpan 上传失败（分卷 {index}/{total_volumes}：{volume.name}，"
                    f"已重试 {upload_retries} 次）：{reason}\n"
                    f"已保留 {len(kept)} 个加密分卷到 {preserved_dir}"
                    f"（解压密码：{password}），可手动上传"
                )

        first = results[0] if results else {}
        first_remote_rel = "/".join(x for x in [remote_folder, volumes[0].name] if x)
        saved_path = (first.get("saved_path") or first.get("remote_path")
                      or first_remote_rel)
        archive_size = sum(v.stat().st_size for v in volumes if v.exists())
        transfer = {
            "engine": "bdpan",
            "moved": len(staged),
            "failed": 0,
            "total": len(staged),
            "archive_name": archive_path.name,
            "archive_size": archive_size,
            "volumes": [v.name for v in volumes],
            "volume_count": total_volumes,
            "remote_folder": remote_folder,
            "password": password,
            "saved_path": saved_path,
            "remote_path": first.get("remote_path") or first_remote_rel,
            "remote_rel": first_remote_rel,
            "return_url": first.get("return_url") or "",
            "return_markdown": first.get("return_markdown") or "",
            "fsid": first.get("fsid") or "",
            "encryption": _ENCRYPTION_LABEL,
            "internal_structure": internal_structure,
        }
        label = (f"Uploaded {archive_path.name} ({total_volumes} volume(s))"
                 if total_volumes > 1 else f"Uploaded {archive_path.name}")
        return [saved_path], label, transfer
    finally:
        shutil.rmtree(assembly_dir, ignore_errors=True)
        for leftover in find_archive_volumes(archive_path):
            try:
                leftover.unlink()
            except OSError:
                logger.debug("failed to remove local volume %s", leftover, exc_info=True)
