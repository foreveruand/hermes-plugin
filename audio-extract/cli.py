#!/usr/bin/env python3
"""Standalone CLI utility for Hermes Audio Extract & Video Clip plugin."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from .config import DEFAULT_CONFIG, load_config
    from .core import (
        auto_extract_options,
        clip_video_file,
        convert_subtitle_to_lrc,
        extract_audio_file,
        list_active_jobs,
        mark_auto_extract_status,
        parse_interval,
        plan_auto_extraction,
        refresh_index,
        resolve_target_file,
        resolve_target_files,
        search_files,
        sync_subtitles_once,
    )
    from .tasks import list_recent_tasks, load_task, run_task_now, submit_task
except ImportError:
    from config import DEFAULT_CONFIG, load_config
    from core import (
        auto_extract_options,
        clip_video_file,
        convert_subtitle_to_lrc,
        extract_audio_file,
        list_active_jobs,
        mark_auto_extract_status,
        parse_interval,
        plan_auto_extraction,
        refresh_index,
        resolve_target_file,
        resolve_target_files,
        search_files,
        sync_subtitles_once,
    )
    from tasks import list_recent_tasks, load_task, run_task_now, submit_task


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Audio Extract & Video Clip Plugin CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # search
    p_s = sub.add_parser("search", help="Search media files by keyword")
    p_s.add_argument("keyword", help="Search keyword")
    p_s.add_argument("--limit", type=int, default=15, help="Result limit")

    # clip
    p_c = sub.add_parser("clip", help="Clip video without re-encoding")
    p_c.add_argument("target", help="Keyword or exact file path")
    p_c.add_argument("start", nargs="?", default="", help="Start time (HH:MM:SS, MM:SS, or HMMSS)")
    p_c.add_argument("end", nargs="?", default="", help="End time (HH:MM:SS, MM:SS, or HMMSS)")
    p_c.add_argument("--interval", help="Compact interval (e.g. 10101-20356)")
    p_c.add_argument("--out", help="Output file path (optional)")
    p_c.add_argument("--reencode", action="store_true", help="Re-encode with x264/aac instead of copy")

    # extract
    p_e = sub.add_parser("extract", help="Extract 16kHz mono audio for transcription")
    p_e.add_argument("target", help="Keyword, file path, or directory path (directory = immediate media files, non-recursive)")
    p_e.add_argument("--out-dir", help="Output directory")
    p_e.add_argument("--format", default="mp3", help="Audio format (default: mp3)")
    p_e.add_argument("--no-job", action="store_true", help="Do not register in .jobs")

    # sync-subtitles
    p_sync = sub.add_parser("sync-subtitles", help="Scan and sync subtitles to source files")
    p_sync.add_argument("--watch", action="store_true", help="Continuous monitoring")
    p_sync.add_argument("--interval", type=int, default=10, help="Watch interval in seconds")

    # refresh-index
    p_ref = sub.add_parser("refresh-index", help="Refresh SQLite file index")
    p_ref.add_argument("--full", action="store_true", help="Perform full rebuild")

    # scan
    p_scan = sub.add_parser(
        "scan",
        help="Refresh index, then auto-queue extraction for new files lacking subtitles/lyrics",
    )
    p_scan.add_argument("--full", action="store_true", help="Perform full index rebuild")
    p_scan.add_argument("--no-queue", action="store_true", help="Only refresh the index")
    p_scan.add_argument("--settle-seconds", type=int, help="Override auto_extract.settle_seconds")
    p_scan.add_argument("--limit", type=int, help="Override auto_extract.max_per_scan")

    # jobs
    p_jobs = sub.add_parser("jobs", help="List active pending jobs")

    # tasks
    p_tasks = sub.add_parser("tasks", help="List recent background tasks")
    p_tasks.add_argument("--limit", type=int, default=10, help="Limit")

    # status
    p_status = sub.add_parser("status", help="Get status of specific task")
    p_status.add_argument("task_id", help="Task ID")

    # vtt-to-lrc
    p_vtt = sub.add_parser("vtt-to-lrc", help="Convert VTT subtitles to LRC lyrics")
    p_vtt.add_argument("input", type=Path, help="Input .vtt file")
    p_vtt.add_argument("-o", "--output", type=Path, help="Output .lrc file")

    args = parser.parse_args()
    cfg = load_config()

    if args.cmd == "search":
        results = search_files(args.keyword, cfg, limit=args.limit, skip_with_subtitles=True)
        if not results:
            print(f"No media files found matching '{args.keyword}'.")
            return
        print(f"Found {len(results)} matching file(s):")
        for i, r in enumerate(results, 1):
            print(f"{i}. {r}")

    elif args.cmd == "clip":
        start_time = args.start
        end_time = args.end
        if args.interval:
            start_time, end_time = parse_interval(args.interval)
        elif "-" in start_time and not end_time:
            start_time, end_time = parse_interval(start_time)

        if not start_time or not end_time:
            print("Error: Both start time and end time are required.", file=sys.stderr)
            sys.exit(1)

        resolved = resolve_target_file(args.target, cfg)
        if not resolved:
            print(f"Error: Target '{args.target}' not found.", file=sys.stderr)
            sys.exit(1)

        out_path = Path(args.out) if args.out else None
        res_file, log = clip_video_file(resolved, start_time, end_time, out_path, reencode=args.reencode)
        print(f"Successfully clipped video to: {res_file}")

    elif args.cmd == "extract":
        out_dir = Path(args.out_dir) if args.out_dir else Path(cfg["out_dir"])
        jobs_dir = None if args.no_job else Path(cfg["jobs_dir"])

        targets = resolve_target_files(args.target, cfg, skip_with_subtitles=True)
        if not targets:
            if Path(args.target).expanduser().is_dir():
                print(
                    f"No extractable media in '{args.target}' "
                    "(matching files already have subtitles/lyrics).",
                    file=sys.stderr,
                )
                return
            print(f"Error: Target '{args.target}' not found.", file=sys.stderr)
            sys.exit(1)
        if len(targets) > 1:
            print(f"Found {len(targets)} media file(s) in directory (non-recursive).")

        for t in targets:
            try:
                res_file, log = extract_audio_file(t, out_dir, jobs_dir, format=args.format)
                print(f"Extracted: {res_file}")
            except Exception as e:
                print(f"Failed to extract {t.name}: {e}", file=sys.stderr)

    elif args.cmd == "sync-subtitles":
        if args.watch:
            print(f"Starting subtitle sync watcher (interval: {args.interval}s, press Ctrl+C to stop)...")
            try:
                while True:
                    cnt = sync_subtitles_once(cfg)
                    if cnt > 0:
                        print(f"Processed {cnt} subtitle(s) at {time.strftime('%Y-%m-%d %H:%M:%S')}")
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nWatcher stopped.")
        else:
            cnt = sync_subtitles_once(cfg)
            print(f"Synced {cnt} subtitle task(s).")

    elif args.cmd == "refresh-index":
        res = refresh_index(cfg, incremental=not args.full)
        print(f"Index refresh complete. Scanned: {res['total_scanned']}, Updated: {res['updated']}.")

    elif args.cmd == "scan":
        scan_cfg = dict(cfg)
        if args.settle_seconds is not None or args.limit is not None:
            auto = dict(scan_cfg.get("auto_extract") or {})
            if args.settle_seconds is not None:
                auto["settle_seconds"] = args.settle_seconds
            if args.limit is not None:
                auto["max_per_scan"] = args.limit
            scan_cfg["auto_extract"] = auto

        res = refresh_index(scan_cfg, incremental=not args.full, track_new=True)
        new_files = res.get("new_files", [])
        print(
            f"Index refresh complete. Scanned: {res['total_scanned']}, "
            f"Updated: {res['updated']}, New: {len(new_files)}."
        )
        if args.no_queue:
            return

        opts = auto_extract_options(scan_cfg)
        plan = plan_auto_extraction(scan_cfg, new_files)
        counts = plan["counts"]

        queued = 0
        extracted = 0
        failed = 0
        out_dir = Path(scan_cfg["out_dir"])
        fmt = opts["format"]
        budget = float(opts.get("max_seconds_per_scan") or 0)
        scan_started = time.time()
        batches = plan["batches"]
        for idx, batch in enumerate(batches, 1):
            if budget and time.time() - scan_started > budget:
                for rest in batches[idx - 1:]:
                    mark_auto_extract_status(scan_cfg, rest, "deferred", "scan time budget reached")
                    counts["deferred"] += len(rest)
                print(f"    time budget reached, {sum(len(b) for b in batches[idx - 1:])} file(s) deferred", flush=True)
                break

            params = {"format": fmt, "no_job": False, "auto": True}
            if len(batch) == 1:
                target = batch[0].name
                resolved_path = str(batch[0])
            else:
                params["batch"] = True
                params["inputs"] = [str(p) for p in batch]
                target = f"自动扫描 ({len(batch)} 个新文件)"
                resolved_path = str(batch[0].parent)

            task = submit_task(
                task_type="extract",
                target=target,
                resolved_path=resolved_path,
                params=params,
                delivery_target=opts["delivery_target"] or None,
                cfg=scan_cfg,
                enqueue=False,
            )
            mark_auto_extract_status(scan_cfg, batch, "queued")
            queued += len(batch)

            print(f"[{idx}/{len(batches)}] extracting {len(batch)} file(s) -> {task.id}", flush=True)
            started = time.time()
            run_task_now(task, scan_cfg)

            done = [p for p in batch if (out_dir / f"{p.stem}.{fmt}").is_file()]
            missing = [p for p in batch if p not in done]
            if done:
                mark_auto_extract_status(scan_cfg, done, "completed")
            if missing:
                reason = (task.error or "; ".join(params.get("failures") or []) or "output missing")[:200]
                mark_auto_extract_status(scan_cfg, missing, "failed", reason)
            extracted += len(done)
            failed += len(missing)
            print(
                f"    {task.status}: {len(done)} ok, {len(missing)} failed, "
                f"{time.time() - started:.0f}s",
                flush=True,
            )

        print(
            f"Auto-extract: queued={queued}, extracted={extracted}, failed={failed}, "
            f"deferred={counts['deferred']}, has_subtitle={counts['has_subtitle']}, "
            f"in_progress={counts['in_progress']}, output_exists={counts['output_exists']}, "
            f"excluded={counts['excluded']}, missing={counts['missing']}."
        )

    elif args.cmd == "jobs":
        jobs = list_active_jobs(cfg)
        print(f"Active jobs ({len(jobs)}):")
        for j in jobs[:20]:
            created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(j.get("created_at", 0)))
            print(f"- {j.get('job_name')}")
            print(f"  Source: {j.get('video')}")
            print(f"  Temp MP3: {j.get('mp3')} | Created: {created}")

    elif args.cmd == "tasks":
        tasks_dir = cfg.get("tasks_dir", DEFAULT_CONFIG["tasks_dir"])
        tasks = list_recent_tasks(tasks_dir, limit=args.limit)
        print(f"Recent tasks ({len(tasks)}):")
        for t in tasks:
            print(f"- [{t.status}] {t.id} ({t.type}) -> {t.target}")
            if t.output_path:
                print(f"  Output: {t.output_path}")
            if t.error:
                print(f"  Error: {t.error}")

    elif args.cmd == "status":
        tasks_dir = cfg.get("tasks_dir", DEFAULT_CONFIG["tasks_dir"])
        t = load_task(args.task_id, tasks_dir)
        if not t:
            print(f"Task '{args.task_id}' not found.")
            sys.exit(1)
        print(json.dumps(t.to_dict(), indent=2, ensure_ascii=False))

    elif args.cmd == "vtt-to-lrc":
        out = args.output or args.input.with_suffix(".lrc")
        success = convert_subtitle_to_lrc(args.input, out)
        if success:
            print(f"Successfully converted VTT to LRC: {out}")
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()
