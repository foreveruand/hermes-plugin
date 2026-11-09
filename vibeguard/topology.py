"""Site and device inventory from a local inventory document.

The authoritative source is a Word document listing, for every site, the
state of each group line and every device on it ("测220级东线：测101设备、Ⅰ丙联1设备运行").
This module turns that document into the fixed-code vocabulary plus the Markdown
note the vault keeps:

    site   "测试甲站" -> "测试甲" -> S12   (__VG_HIDDEN_S12__ at the model boundary)
    device "测101"   -> K405  (__VG_HIDDEN_K405__ at the model boundary)

Codes are never renumbered: an existing mapping.json keeps its numbers and new
devices get max+1, so links in older notes and chats stay valid.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from . import mapping
    from .config import mapping_path
except ImportError:
    import mapping
    from config import mapping_path

#: "测试甲站", "64.己区站", "王家庄站点", "测试丁站" — the suffix is peeled off afterwards.
#: The suffix must stay inside the group, otherwise the greedy body eats "服务".
_SITE_HEAD = re.compile(
    r"^\s*(?:\d+\s*[.．、]\s*)?([\u4e00-\u9fa5A-Za-z0-9]{1,10}(?:服务站|站点|站))\s*$"
)
#: "（1）某市市区", "（一）220级站点" — subsections that carry no block of their own.
_SUBSECTION = re.compile(r"^\s*[（(]\s*[0-9一二三四五六七八九十]+\s*[）)]")
#: "...220级站点" in a subsection title, used as the section label.
_SECTION_LEVEL = re.compile(r"([0-9]{2,3})\s*级")

_STATE_SUFFIX = ("待机", "备用", "运行", "联动", "在开启", "在关闭", "关闭", "开启", "解除")
_DEVICE_SUFFIX = ("设备",)
#: Tokens that are group or host references, never a device.
_NOT_A_DEVICE = ("级", "主机", "干线", "支线", "线", "等级")
#: "甲2055", "乙1021", "丙100" — device numbers written without the 设备 suffix.
_BARE_ID = re.compile(r"^[\u4e00-\u9fa5ⅠⅡⅢⅣⅤⅥ]{1,4}\d{3,4}[甲乙]?$")
_ID_MARK = re.compile(r"[0-9ⅠⅡⅢⅣⅤⅥ#＃]")

_SPLIT = re.compile(r"[、，,；;]")
#: "测220级东线", "王35级线", "楼李110级上线"
_GROUP_LINE = re.compile(
    r"^[\u4e00-\u9fa5A-Za-z0-9]{1,8}(?P<level>\d{2,3})级(?P<side>[东南西北中上下]?)线$"
)

_DOC_COMMANDS = (
    ("catdoc", ["-w", "-d", "utf-8"]),
    ("antiword", ["-m", "UTF-8.txt"]),
)
_TAG_RE = re.compile(r"<[^>]+>")

TICK = "`"
HEADER = """\
---
title: 站点与设备对照
type: 索引
source: {source}
generated: {generated}
sites: {sites}
devices: {devices}
tags:
  - 资产
  - 站点
---

# 站点与设备对照

> 源：`{source}`，生成于 {generated}。由 `vibeguard inventory sync` 自动重建，请勿手工编辑。

## 用法

- 每节标题形如 `## __VG_HIDDEN_S12__ 某（110级，24 台）`，代号在前、名称在后。
- 站点名已去掉「服务站 / 站点 / 站」后缀，几种写法都归到同一代号。
- 按代号查：`grep -n '^## __VG_HIDDEN_S12__ ' 站点清单.md`（注意代号后的空格）。
- 按名称查：`grep -n '^## .*<名称>' 站点清单.md`。
- 按设备查：`grep -n '<设备编号>' 站点清单.md`，命中行就是它所在的站点与分组。
- 取整段：`grep -A 14 '^## __VG_HIDDEN_S12__ ' 站点清单.md`。
- 代号与脱敏占位符一一对应：`S12` 在对话里显示为 `__VG_HIDDEN_S12__`，`K3` 显示为 `__VG_HIDDEN_K3__`。
- 设备名是站点内编号，已去掉「设备」后缀。

## 统计

| 等级 | 站点 | 设备 |
| --- | --- | --- |
{stats}
"""


def _group_name(line: str) -> str:
    """Group heading of a line ("测220级东线"), or "" when the line has none."""
    head = line.split("：", 1)[0] if "：" in line else line.split(":", 1)[0] if ":" in line else ""
    head = head.strip()
    return head if _GROUP_LINE.match(head) else ""


def _tokens(line: str) -> Iterable[str]:
    """Split a group line into candidate device tokens, dropping state words."""
    body = line.split("：", 1)[1] if "：" in line else (line.split(":", 1)[1] if ":" in line else line)
    for part in _SPLIT.split(body):
        # "甲2055（Ⅰ甲金线）": the parenthesis holds the line name, not the device.
        part = part.split("（", 1)[0].split("(", 1)[0]
        part = part.strip().strip("。.")
        for word in _STATE_SUFFIX:
            if part.endswith(word) and len(part) > len(word):
                part = part[: -len(word)]
        part = part.strip()
        if part:
            yield part


def _device(token: str):
    """(id, kind) for a device token, or None when the token is not a device."""
    base = None
    kind = "设备"
    for suffix in _DEVICE_SUFFIX:
        if token.endswith(suffix) and len(token) > len(suffix):
            base = token[: -len(suffix)]
            kind = suffix
            break
    if base is None and _BARE_ID.match(token):
        base = token
    if not base or len(base) < 2 or len(base) > 10:
        return None
    if any(bad in base for bad in _NOT_A_DEVICE):
        return None
    if not _ID_MARK.search(base):
        return None
    return base, kind


def parse(text: str) -> Dict[str, Any]:
    """Stations and their devices, in document order."""
    sites: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    current: Optional[str] = None
    section = ""
    # A site can be listed in more than one block; dedupe per site, not globally,
    # so two sites may legitimately share the same (device, group) pair.
    seen: Dict[str, set] = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _SUBSECTION.match(line):
            match = _SECTION_LEVEL.search(line)
            if match:
                section = match.group(1) + "级"
            continue
        head = _SITE_HEAD.fullmatch(line)
        if head:
            name = mapping.site_base(head.group(1))
            # "220级站点" is a level label, not a site.
            if "级" in name:
                continue
            if name not in sites:
                sites[name] = {"name": name, "section": section,
                                  "device_names": [], "devices": []}
                order.append(name)
            current = name
            seen.setdefault(name, set())
            continue
        if current is None:
            continue
        entry = sites[current]
        group = _group_name(line)
        for token in _tokens(line):
            found = _device(token)
            if not found:
                continue
            device, kind = found
            if device not in entry["device_names"]:
                entry["device_names"].append(device)
            key = (device, group)
            keys = seen.setdefault(current, set())
            if key not in keys:
                keys.add(key)
                entry["devices"].append({"name": device, "type": kind, "group": group})

    return {
        "sites": [sites[name] for name in order],
        "site_count": len(order),
        "device_count": sum(len(sites[name]["device_names"]) for name in order),
    }


def _doc_text(path: Path) -> Optional[str]:
    """Paragraph text from a .doc (catdoc/antiword) or .docx (zip + tag strip)."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(path) as archive:
                names = [n for n in archive.namelist()
                         if n.startswith("word/") and n.endswith(".xml")]
                chunks = []
                for name in names[:64]:
                    try:
                        chunks.append(_TAG_RE.sub(" ", archive.read(name).decode("utf-8", "ignore")))
                    except (KeyError, OSError):
                        continue
                return " ".join(chunks) or None
        except (zipfile.BadZipFile, OSError):
            return None
    if suffix in (".txt", ".md"):
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
    for name, args in _DOC_COMMANDS:
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            proc = subprocess.run([exe, *args, str(path)], capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.decode("utf-8", "ignore").replace("\r\n", "\n")
    return None


def resolve_document(config: Dict[str, Any], explicit: Optional[str] = None) -> Optional[Path]:
    """Newest file matching topology.glob, or the explicit path."""
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    patterns = ((config.get("topology") or {}).get("glob")) or []
    if isinstance(patterns, str):
        patterns = [patterns]
    candidates: List[Path] = []
    for pattern in patterns:
        globber = Path("/").glob(pattern.lstrip("/")) if pattern.startswith("/") else Path(".").glob(pattern)
        for match in globber:
            if match.is_file():
                candidates.append(match)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def archive_path(config: Dict[str, Any]) -> Optional[Path]:
    rel = (config.get("topology") or {}).get("archive")
    if not rel:
        return None
    path = Path(str(rel)).expanduser()
    if not path.is_absolute():
        path = Path(config.get("base_dir") or ".") / path
    if path.suffix.lower() != ".md":
        path = path.with_name(path.name + ".md")
    return path


def _tick(text: str) -> str:
    return TICK + text + TICK


def _clean(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def render(parsed: Dict[str, Any], entities: "mapping.Entities", source: str,
           generated: str) -> str:
    """Body of the single note, codes taken from the mapping."""
    sites = parsed["sites"]

    by_level: Dict[str, List[int]] = {}
    for site in sites:
        bucket = by_level.setdefault(site["section"] or "未知", [0, 0])
        bucket[0] += 1
        bucket[1] += len(site["device_names"])
    rows = []
    for level in sorted(by_level, key=lambda v: (-int("".join(c for c in v if c.isdigit()) or 0), v)):
        count, devices = by_level[level]
        rows.append("| " + level + " | " + str(count) + " | " + str(devices) + " |")
    rows.append("| **合计** | **" + str(len(sites)) + "** | **" + str(parsed["device_count"]) + "** |")

    head = HEADER.format(source=source, generated=generated, sites=len(sites),
                         devices=parsed["device_count"], stats="\n".join(rows))
    out = [head.rstrip(), ""]

    for site in sites:
        name = mapping.site_base(site["name"])
        code = entities.sites.get(name, "")
        devices = site.get("devices") or []
        title = "## " + ((f"__VG_HIDDEN_{code}__ ") if code else "") + name
        out.append(title + "（" + (site["section"] or "未知") + "，" + str(len(devices)) + " 台）")
        out.append("")

        groups: List[str] = []
        for device in devices:
            group = device.get("group") or ""
            if group and group not in groups:
                groups.append(group)

        def table(items: List[Dict[str, Any]]) -> None:
            out.append("| 代号 | 设备 | 类型 |")
            out.append("| --- | --- | --- |")
            for device in items:
                code_val = entities.devices.get(device["name"], "")
                cell = f"__VG_HIDDEN_{code_val}__" if code_val else ""
                out.append("| " + _tick(cell) + " | "
                           + _clean(device["name"]) + " | " + device["type"] + " |")
            out.append("")

        for group in groups:
            out.append("### " + group)
            out.append("")
            table([d for d in devices if d.get("group") == group])

        loose = [d for d in devices if not d.get("group")]
        if loose:
            out.append("### 其他设备")
            out.append("")
            table(loose)

        if not devices:
            out.append("（源文档中该站点只列了分组，没有单独列出的设备。）")
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def write(path: Path, body: str, dry_run: bool = False) -> Dict[str, Any]:
    path = Path(path)
    result = {"file": str(path), "bytes": len(body.encode("utf-8")), "written": 0}
    if dry_run:
        return result
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_text(encoding="utf-8") != body:
        path.write_text(body, encoding="utf-8")
        result["written"] = 1
    return result


def sync(config: Dict[str, Any], path: Optional[str] = None, force: bool = False,
         dry_run: bool = False, out: Optional[str] = None) -> Dict[str, Any]:
    """Rebuild mapping.json and the Markdown note from the newest document."""
    document = resolve_document(config, path)
    if document is None:
        return {"error": "no document matched topology.glob (or the path does not exist)"}

    stat = document.stat()
    stamp = {"mtime": int(stat.st_mtime), "size": stat.st_size}
    target_mapping = mapping_path(config)
    entities = mapping.Entities.load(target_mapping)
    unchanged = entities.stamp == stamp

    text = _doc_text(document)
    if not text:
        return {"error": "cannot extract text from %s (install catdoc for .doc)" % document}
    parsed = parse(text)

    if unchanged and not force:
        added = {"S": 0, "K": 0}
    else:
        added = entities.assign(parsed)
        entities.source = document.name
        entities.stamp = stamp
        if not dry_run:
            entities.save(target_mapping)

    body = render(parsed, entities, document.name, time.strftime("%Y-%m-%d"))

    target_note = Path(out) if out else archive_path(config)
    result: Dict[str, Any] = {
        "file": str(document),
        "unchanged": unchanged,
        "sites": parsed["site_count"],
        "devices": parsed["device_count"],
        "added": added,
        "mapping": str(target_mapping),
    }
    if target_note is None:
        result["note"] = {"error": "topology.archive is not configured"}
    else:
        result["note"] = write(target_note, body, dry_run=dry_run)
    return result
