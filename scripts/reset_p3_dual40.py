#!/usr/bin/env python3
"""Explicitly reset one DUAL40 ladder after operator review.

The service must be stopped first. A timestamped SQLite backup is always created.
Cycle history is deleted only with ``--clear-cycles``; active cycles may be
discarded only in PAPER scope with the separate explicit flag.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from p3_config import get_p3_settings
from p3_dual40_store import active_cycle, connect_dual40, reset_scope


CONFIRM = "RESET-DUAL40-AFTER-MANUAL-REVIEW"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("PAPER", "LIVE"), required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--clear-cycles", action="store_true")
    parser.add_argument("--clear-decisions", action="store_true")
    parser.add_argument("--discard-active-paper", action="store_true")
    parser.add_argument("--backup-dir", default="data/backups")
    args = parser.parse_args()

    if args.confirm != CONFIRM:
        print(f"REFUSED: exact --confirm {CONFIRM} required", file=sys.stderr)
        return 2

    settings = get_p3_settings()
    conn = connect_dual40(settings.p3_db_path)
    try:
        current = active_cycle(conn, scope=args.scope)
        if current is not None and not args.discard_active_paper:
            print(
                f"REFUSED: active cycle id={current.get('id')} "
                f"scope={current.get('scope')} status={current.get('status')}",
                file=sys.stderr,
            )
            return 3
        if args.discard_active_paper and args.scope != "PAPER":
            print("REFUSED: --discard-active-paper requires --scope PAPER", file=sys.stderr)
            return 4
        if args.discard_active_paper and not args.clear_cycles:
            print("REFUSED: --discard-active-paper requires --clear-cycles", file=sys.stderr)
            return 4

        backup_dir = Path(args.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = backup_dir / f"p3_arbitrage-before-{args.scope.lower()}-reset-{stamp}.sqlite"
        backup_conn = sqlite3.connect(backup_path)
        try:
            conn.backup(backup_conn)
        finally:
            backup_conn.close()

        reset_scope(
            conn,
            scope=args.scope,
            clear_cycles=bool(args.clear_cycles),
            clear_decisions=bool(args.clear_decisions),
            discard_active_paper=bool(args.discard_active_paper),
        )
    finally:
        conn.close()

    print(
        f"DUAL40 {args.scope} RESET PASS clear_cycles={bool(args.clear_cycles)} "
        f"clear_decisions={bool(args.clear_decisions)} backup={backup_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
