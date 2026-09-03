#!/usr/bin/env python3
"""Explicitly reset one DUAL40 ladder after operator review.

The service must be stopped first. This utility never deletes cycle history unless
``--clear-cycles`` is also supplied and never accepts an abbreviated confirmation.
"""
from __future__ import annotations

import argparse
import sys

from p3_config import get_p3_settings
from p3_dual40_store import active_cycle, connect_dual40, reset_scope


CONFIRM = "RESET-DUAL40-AFTER-MANUAL-REVIEW"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("PAPER", "LIVE"), required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--clear-cycles", action="store_true")
    args = parser.parse_args()

    if args.confirm != CONFIRM:
        print(f"REFUSED: exact --confirm {CONFIRM} required", file=sys.stderr)
        return 2

    settings = get_p3_settings()
    conn = connect_dual40(settings.p3_db_path)
    try:
        current = active_cycle(conn)
        if current is not None:
            print(
                f"REFUSED: active cycle id={current.get('id')} "
                f"scope={current.get('scope')} status={current.get('status')}",
                file=sys.stderr,
            )
            return 3
        reset_scope(
            conn,
            scope=args.scope,
            clear_cycles=bool(args.clear_cycles),
        )
    finally:
        conn.close()

    print(
        f"DUAL40 {args.scope} RESET PASS clear_cycles={bool(args.clear_cycles)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
