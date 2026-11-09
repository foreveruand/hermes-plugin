"""Slash commands for the ``qbittorrent-manager`` plugin: /qb, /qbsub, /qbsearch."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .client import (
    QBClient,
    default_tag,
    format_size,
    format_state,
    qb_rule,
    resolve_targets,
    save_cache,
)

logger = logging.getLogger("qbittorrent-manager")

HELP = """⚡ qBittorrent 快捷指令指南：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📡 订阅管理：
  • /qb sub <关键词>       新增 RSS 订阅词（可输入多个）
  • /qb sub               查看当前全部订阅关键词
  • /qb unsub <关键词>     删除指定的订阅词
  • /qb clean             手动清理已下载命中的订阅词（已有定时任务常驻）

🔍 种子与任务操作：
  • /qb search <关键词>    搜索种子（支持简写 /qb s）
  • /qb tag <序号/哈希> [标签] 给种子打标签（默认标签: 完成日期+7天，格式 MM-DD）
  • /qb del <序号/哈希>    删除种子及本地文件（加 --keep-files 保留）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💡 技巧：搜索后可直接用序号操作，如：/qb tag 1 或 /qb del 1,2"""


def _words(raw: str) -> list[str]:
    return [t.strip() for t in raw.replace(",", " ").split() if t.strip()]


def _render_search(results: list[dict[str, Any]], query: str, limit: int = 15) -> str:
    lines = [f"🔍 找到 {len(results)} 个匹配 \"{query}\" 的任务（已缓存序号）："]
    for i, t in enumerate(results[:limit], 1):
        state_str = format_state(t.get("state", ""))
        prog = f"{t.get('progress', 0) * 100:.1f}%"
        size_str = format_size(t.get("size", 0))
        tag_str = t.get("tags", "") or "无"
        comp = (
            datetime.fromtimestamp(t["completion_on"]).strftime("%Y-%m-%d %H:%M")
            if t.get("completion_on")
            else "未完成"
        )
        lines.append(f"[{i}] {t['name']}")
        lines.append(f"    • {state_str} ({prog}) | 大小: {size_str} | 标签: {tag_str} | 完成: {comp}")
    if len(results) > limit:
        lines.append(f"    ... 还有 {len(results) - limit} 个任务未列出")
    lines.append("\n💡 快捷操作：/qb tag 1 或 /qb del 1")
    return "\n".join(lines)


async def cmd_qb(raw_args: str) -> str:
    """``/qb`` — sub / unsub / clean / search / tag / del."""
    args = _words(raw_args)
    if not args:
        return HELP
    cmd = args[0].lower()
    sub_args = args[1:]

    async with QBClient() as qb:
        try:
            if cmd in ("help", "-h", "--help"):
                return HELP

            if cmd in ("sub", "rss-add", "subscribe"):
                return await _sub(qb, sub_args)
            if cmd in ("unsub", "rss-del", "unsubscribe"):
                return await _unsub(qb, sub_args)
            if cmd in ("clean", "rss-clean", "subclean", "sub-clean"):
                return await _clean(qb, sub_args)
            if cmd in ("search", "s", "find"):
                return await _search(qb, sub_args)
            if cmd in ("tag", "tagging"):
                return await _tag(qb, sub_args)
            if cmd in ("del", "delete", "rm"):
                return await _del(qb, sub_args)
            if cmd in ("list", "ls", "recent"):
                return "💡 /qb list 指令已停用，请使用 /qb search <关键词> 搜索指定种子。"

            # Bare keywords fall through to search, matching the old CLI.
            return await _search(qb, args)
        except Exception as exc:
            return f"❌ 执行出错: {exc}"


async def cmd_qbsub(raw_args: str) -> str:
    """``/qbsub <关键词>`` — shortcut for ``/qb sub``."""
    async with QBClient() as qb:
        try:
            return await _sub(qb, _words(raw_args))
        except Exception as exc:
            return f"❌ 执行出错: {exc}"


async def cmd_qbsearch(raw_args: str) -> str:
    """``/qbsearch <关键词>`` — shortcut for ``/qb search``."""
    async with QBClient() as qb:
        try:
            return await _search(qb, _words(raw_args))
        except Exception as exc:
            return f"❌ 执行出错: {exc}"


async def _sub(qb: QBClient, sub_args: list[str]) -> str:
    rule = qb_rule()
    if not sub_args:
        kws = await qb.get_rss_keywords()
        lines = [f"📋 当前 RSS 订阅规则 [{rule}] 共有 {len(kws)} 个关键词："]
        if not kws:
            lines.append("  （暂无订阅关键词）")
        else:
            lines.extend(f"  {i}. {kw}" for i, kw in enumerate(kws, 1))
        return "\n".join(lines)

    keywords = sub_args
    ok, current = await qb.add_rss_keywords(keywords)
    if not ok:
        return "❌ 添加订阅关键词失败"
    lines = [f"✅ 成功添加订阅关键词: {', '.join(keywords)} 到规则 [{rule}]",
             f"📋 当前订阅列表 (共 {len(current)} 个):"]
    lines.extend(f"  • {kw}" for kw in current[:15])
    if len(current) > 15:
        lines.append(f"  ... 以及其他 {len(current) - 15} 个")
    return "\n".join(lines)


async def _unsub(qb: QBClient, sub_args: list[str]) -> str:
    if not sub_args:
        return "❌ 请提供要删除的订阅关键词，例如：/qb unsub SNOS-450"
    kw = " ".join(sub_args).strip()
    ok, current = await qb.remove_rss_keyword(kw)
    if not ok:
        return "❌ 移除关键词失败"
    return f"🗑️ 已从规则 [{qb_rule()}] 移除订阅关键词: {kw}\n📋 剩余关键词: {len(current)} 个"


async def _clean(qb: QBClient, sub_args: list[str]) -> str:
    is_watchdog = any(a in sub_args for a in ("--watchdog", "-w", "--quiet", "-q"))
    removed_cnt, remaining_cnt, removed_list = await qb.clean_rss_keywords()
    if removed_cnt > 0:
        lines = [f"🧹 [qBittorrent 自动清理] 检测到已下载种子，已自动清理以下 {removed_cnt} 个 RSS 订阅词："]
        for item in removed_list:
            lines.append(f"  • 关键词: {item['keyword']} (匹配种子: {item['matched_torrent']})")
        lines.append(f"📋 规则 [{qb_rule()}] 剩余订阅词: {remaining_cnt} 个")
        return "\n".join(lines)
    if is_watchdog:
        return ""
    return f"✨ 规则 [{qb_rule()}] 无需清理，当前 {remaining_cnt} 个关键词均未匹配到已下载的种子。"


async def _search(qb: QBClient, sub_args: list[str]) -> str:
    if not sub_args:
        return "❌ 请输入搜索关键词，例如：/qb search SNOS-405"
    query = " ".join(sub_args).strip()
    results = await qb.search_torrents(query)
    if not results:
        return f"🔍 未找到匹配 \"{query}\" 的种子任务。"
    save_cache(results)
    return _render_search(results, query)


async def _tag(qb: QBClient, sub_args: list[str]) -> str:
    if not sub_args:
        return ("❌ 用法：/qb tag <序号/哈希/关键词> [自定义标签]\n"
                "示例：/qb tag 1（默认打上完成日期+7天） 或 /qb tag 1,2 自定义标签")

    target = sub_args[0]
    custom_tag = " ".join(sub_args[1:]).strip() if len(sub_args) > 1 else None

    resolved = await resolve_targets(target, qb)
    if not resolved:
        return (f"❌ 未找到匹配的目标任务: '{target}'。请先使用 /qb search 搜索，或提供有效序号/哈希。")

    tag_to_hashes: dict[str, list[str]] = {}
    tagged_info: list[tuple[str, str, str]] = []

    for item in resolved:
        h = item["hash"]
        name = item.get("name", h)
        if custom_tag:
            applied_tag, note = custom_tag, "自定义"
        else:
            applied_tag = default_tag(item.get("completion_on") or 0)
            note = "完成时间+7天" if item.get("completion_on") else "当前时间+7天(未完成)"
        tag_to_hashes.setdefault(applied_tag, []).append(h)
        tagged_info.append((name, applied_tag, note))

    all_ok = True
    for tag_val, hashes_list in tag_to_hashes.items():
        if not await qb.add_tags(hashes_list, tag_val):
            all_ok = False

    if not all_ok:
        return "❌ 添加标签失败"
    lines = [f"🏷️ 成功为 {len(tagged_info)} 个任务打上标签："]
    for name, tag_val, note in tagged_info[:5]:
        lines.append(f"  • {name} -> 标签: '{tag_val}' ({note})")
    if len(tagged_info) > 5:
        lines.append(f"  ... 以及其他 {len(tagged_info) - 5} 个")
    return "\n".join(lines)


async def _del(qb: QBClient, sub_args: list[str]) -> str:
    if not sub_args:
        return "❌ 用法：/qb del <序号/哈希/关键词> [--keep-files]\n示例：/qb del 1 或 /qb del 1,2"

    delete_files = True
    cleaned: list[str] = []
    for a in sub_args:
        if a in ("--keep-files", "-k", "keep"):
            delete_files = False
        else:
            cleaned.append(a)

    if not cleaned:
        return "❌ 请提供要删除的序号或哈希"

    target = " ".join(cleaned)
    resolved = await resolve_targets(target, qb)
    if not resolved:
        return f"❌ 未找到匹配的目标任务: '{target}'。请先使用 /qb search 搜索，或提供有效序号/哈希。"

    ok = await qb.delete_torrents([item["hash"] for item in resolved], delete_files=delete_files)
    if not ok:
        return "❌ 删除种子失败"
    file_status = "保留本地文件" if not delete_files else "已一并删除本地文件"
    lines = [f"🗑️ 成功删除 {len(resolved)} 个种子任务（{file_status}）："]
    for item in resolved[:5]:
        lines.append(f"  • {item['name']}")
    if len(resolved) > 5:
        lines.append(f"  ... 以及其他 {len(resolved) - 5} 个")
    return "\n".join(lines)


def register_all_commands(ctx: Any) -> None:
    """Register this plugin's slash commands."""
    for name, handler, desc, hint in (
        ("qb", cmd_qb, "qBittorrent 统一管理（sub/unsub/clean/search/tag/del）", "[子指令] [参数]"),
        ("qbsub", cmd_qbsub, "快捷新增 RSS 订阅关键词", "<关键词>"),
        ("qbsearch", cmd_qbsearch, "快捷搜索种子", "<关键词>"),
    ):
        if hasattr(ctx, "register_command"):
            ctx.register_command(name=name, handler=handler, description=desc, args_hint=hint)
            logger.info("Registered slash command /%s", name)
