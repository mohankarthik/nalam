"""Sync the Drive Medical folder into Paperless. Phase 0 entry point."""

from __future__ import annotations

import argparse
import logging

from src.drive_sync import report_failures, sync


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Show the plan, upload nothing")
    p.add_argument("--limit", type=int, default=0, help="Upload at most N documents")
    p.add_argument(
        "--report", action="store_true", help="List documents Paperless refused to consume"
    )
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.report:
        report_failures()
        return
    sync(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
