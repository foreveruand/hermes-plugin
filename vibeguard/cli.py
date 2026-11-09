#!/usr/bin/env python3
"""VibeGuard maintenance CLI: seed or rebuild the site/device mapping."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import mapping  # noqa: E402
import topology  # noqa: E402


def _emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_seed(args: argparse.Namespace) -> int:
    """Adopt the codes an existing note already shows (one-time migration)."""
    cfg = config.load_config()
    note = Path(args.note).expanduser() if args.note else topology.archive_path(cfg)
    if note is None or not note.is_file():
        _emit({"error": "note not found; pass --note <path>"})
        return 2
    target = config.mapping_path(cfg)
    if target.is_file() and not args.force:
        _emit({"error": "%s already exists; pass --force to merge into it" % target})
        return 2
    entities = mapping.Entities(target)
    count = entities.seed_from_note(note.read_text(encoding="utf-8"))
    entities.source = note.name
    entities.save(target)
    _emit({"mapping": str(target), "seeded": count,
           "sites": len(entities.sites), "devices": len(entities.devices)})
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = config.load_config()
    if not (cfg.get("topology") or {}).get("enabled", True):
        _emit({"error": "topology.enabled is false in %s" % config.get_config_path()})
        return 2
    result = topology.sync(cfg, path=args.doc, force=args.force,
                           dry_run=args.dry_run, out=args.out)
    _emit(result)
    return 0 if "error" not in result else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vibeguard", description=__doc__)
    top = parser.add_subparsers(dest="command", required=True)

    inventory = top.add_parser("inventory", help="site and device inventory")
    actions = inventory.add_subparsers(dest="action", required=True)

    sync_cmd = actions.add_parser("sync", help="rebuild mapping.json and the vault note")
    sync_cmd.add_argument("--doc", help="inventory document (default: newest topology.glob match)")
    sync_cmd.add_argument("--out", help="write the note here instead of topology.archive")
    sync_cmd.add_argument("--force", action="store_true", help="re-parse even if the document is unchanged")
    sync_cmd.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    sync_cmd.set_defaults(func=cmd_sync)

    seed_cmd = actions.add_parser("seed", help="seed mapping.json from an existing note")
    seed_cmd.add_argument("--note", help="note to read codes from (default: topology.archive)")
    seed_cmd.add_argument("--force", action="store_true", help="merge into an existing mapping.json")
    seed_cmd.set_defaults(func=cmd_seed)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
