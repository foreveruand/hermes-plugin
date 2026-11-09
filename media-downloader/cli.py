#!/usr/bin/env python3
"""Standalone CLI utility for Hermes Media Downloader Plugin.

Runs downloads synchronously (useful for cron jobs and shell wrappers) and inspects
background task records written by the plugin worker.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

try:
    from .asmr import parse_work_id
    from .config import DEFAULT_CONFIG, load_config
    from .core import format_archived_paths, is_asmr_target, parse_extra_args, run_download_target
    from .handoff import run_import_target
    from .tasks import list_recent_tasks, load_task
except ImportError:
    from asmr import parse_work_id
    from config import DEFAULT_CONFIG, load_config
    from core import format_archived_paths, is_asmr_target, parse_extra_args, run_download_target
    from handoff import run_import_target
    from tasks import list_recent_tasks, load_task


def _emit_result(result: dict, as_json: bool) -> int:
    """Print a download result and return the matching exit code."""
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["files"] or result["success"] else 1

    if result["files"]:
        print(f"\nSuccessfully downloaded and archived {len(result['files'])} file(s):")
        print(format_archived_paths(result["files"]))
    transfer = result.get("transfer") or {}
    if transfer.get("manifest"):
        print(f"\n入库指令: /dl {transfer['manifest']}")
    if result["files"]:
        return 0
    if result["success"]:
        print("\nDownload finished (possibly skipped existing archive records).")
        return 0
    print("\nFailed to download media.", file=sys.stderr)
    print(result["log"], file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes Media Downloader Plugin CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_d = sub.add_parser("download", help="Download a URL synchronously and archive it")
    p_d.add_argument("url", help="Media URL, or a DLSite work number such as RJ123456")
    p_d.add_argument("--type", choices=["auto", "video", "image", "asmr"], default="auto", help="Content category")
    p_d.add_argument("--author", default="", help="Author subdirectory for Pixiv / X galleries")
    p_d.add_argument("--out", default="", help="Custom archive directory")
    p_d.add_argument("--proxy", default="", help="Custom proxy URL")
    p_d.add_argument("--no-proxy", action="store_true", help="Force direct connection")
    p_d.add_argument("--force-proxy", action="store_true", help="Force proxy usage")
    p_d.add_argument("--extra", default="", help="Extra arguments passed to yt-dlp / gallery-dl")
    p_d.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    p_a = sub.add_parser("asmr", help="Download an ASMR.ONE work by RJ code and archive it")
    p_a.add_argument("rj_code", help="DLSite work number, e.g. RJ123456")
    p_a.add_argument("--out", default="", help="Custom archive directory")
    p_a.add_argument("--proxy", default="", help="Custom proxy URL")
    p_a.add_argument("--no-proxy", action="store_true", help="Force direct connection")
    p_a.add_argument("--force-proxy", action="store_true", help="Force proxy usage")
    p_a.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    p_i = sub.add_parser("import", help="Download, decrypt and import an HMD1 handoff code")
    p_i.add_argument("code", help="Handoff code emitted by a bdpan upload (HMD1:...)")
    p_i.add_argument("--to", default="", help="Destination category or absolute local directory")
    p_i.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    p_s = sub.add_parser("status", help="Show a background task record")
    p_s.add_argument("task_id", help="Task ID")

    p_t = sub.add_parser("tasks", help="List recent background tasks")
    p_t.add_argument("--limit", type=int, default=10, help="Result limit")

    args = parser.parse_args()
    cfg = load_config()
    tasks_dir = cfg.get("tasks_dir", DEFAULT_CONFIG["tasks_dir"])

    if args.cmd == "download":
        result = run_download_target(
            target=args.url,
            cfg=cfg,
            category=args.type,
            author=args.author,
            out_dir=args.out,
            proxy=args.proxy,
            force_proxy=args.force_proxy,
            no_proxy=args.no_proxy,
            extra_args=parse_extra_args(args.extra),
        )
        return _emit_result(result, args.json)

    if args.cmd == "asmr":
        if not is_asmr_target(args.rj_code):
            print(f"Invalid work number: {args.rj_code}", file=sys.stderr)
            return 2
        result = run_download_target(
            target=parse_work_id(args.rj_code),
            cfg=cfg,
            category="asmr",
            out_dir=args.out,
            proxy=args.proxy,
            force_proxy=args.force_proxy,
            no_proxy=args.no_proxy,
        )
        return _emit_result(result, args.json)

    if args.cmd == "import":
        result = run_import_target(code=args.code, cfg=cfg, category=args.to)
        if args.json:
            return _emit_result(result, True)
        print(f"Imported {result['count']} file(s) into {result['destination']}")
        return 0

    if args.cmd == "status":
        task = load_task(args.task_id, tasks_dir)
        if not task:
            print(f"Task not found: {args.task_id}", file=sys.stderr)
            return 1
        print(json.dumps(task.to_dict(), ensure_ascii=False, indent=2))
        return 0

    tasks = list_recent_tasks(tasks_dir, limit=args.limit)
    if not tasks:
        print("No background tasks recorded.")
        return 0
    for t in tasks:
        created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t.created_at))
        print(f"{t.id}\t{t.status}\t{len(t.files)} file(s)\t{created}\t{t.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
