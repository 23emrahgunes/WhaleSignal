"""Read-only operational analytics for the DUAL40 cohort."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from p3_dual40_core import DEFAULT_LADDER, DUAL40_STRATEGY
from p3_dual40_store import (
    active_cycle,
    connect_dual40,
    ladder_state,
    read_scan_status,
)


def _drawdown(rows: list[dict[str, Any]]) -> float:
    equity = peak = 0.0
    maximum = 0.0
    for row in rows:
        equity += float(row.get("realized_pnl_usdc") or 0.0)
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _cycle_dict(row) -> dict[str, Any]:  # noqa: ANN001
    item = dict(row)
    # Large gate/details JSON is not required by the frequent dashboard route.
    item.pop("gate_json", None)
    item.pop("details_json", None)
    return item


def _scope_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [row for row in rows if row.get("realized_pnl_usdc") is not None]
    pnl = sum(float(row.get("realized_pnl_usdc") or 0.0) for row in settled)
    wins = sum(1 for row in settled if float(row.get("realized_pnl_usdc") or 0.0) > 1e-9)
    losses = sum(1 for row in settled if float(row.get("realized_pnl_usdc") or 0.0) < -1e-9)
    flats = len(settled) - wins - losses
    no_fill = sum(1 for row in rows if row.get("status") == "NO_FILL")
    matched = sum(
        1
        for row in rows
        if row.get("status") in {"PAPER_MATCHED", "LIVE_MATCHED_MERGED"}
    )
    single_leg = sum(
        1
        for row in settled
        if (
            (float(row.get("up_filled_shares") or 0.0) > 1e-9)
            != (float(row.get("down_filled_shares") or 0.0) > 1e-9)
        )
    )
    partial_unequal = sum(
        1
        for row in settled
        if min(
            float(row.get("up_filled_shares") or 0.0),
            float(row.get("down_filled_shares") or 0.0),
        ) > 1e-9
        and abs(
            float(row.get("up_filled_shares") or 0.0)
            - float(row.get("down_filled_shares") or 0.0)
        ) > 1e-9
    )
    touched_41 = sum(
        1
        for row in rows
        if bool(row.get("near_touch_up_41")) or bool(row.get("near_touch_down_41"))
    )
    denomin = matched + single_leg + partial_unequal + no_fill
    return {
        "cycles": len(rows),
        "settled": len(settled),
        "wins": wins,
        "losses": losses,
        "flat": flats,
        "hit_rate": wins / len(settled) if settled else None,
        "realized_pnl_usdc": round(pnl, 6),
        "average_pnl_usdc": round(pnl / len(settled), 6) if settled else None,
        "max_drawdown_usdc": round(_drawdown(settled), 6),
        "matched_cycles": matched,
        "single_leg_cycles": single_leg,
        "partial_unequal_cycles": partial_unequal,
        "no_fill_cycles": no_fill,
        "pair_completion_rate": matched / denomin if denomin else None,
        "single_leg_rate": single_leg / denomin if denomin else None,
        "near_touch_41_cycles": touched_41,
    }


def build_dual40_summary(path: str, *, limit: int = 100) -> dict[str, Any]:
    conn = connect_dual40(path)
    try:
        rows = [
            _cycle_dict(row)
            for row in conn.execute(
                """
                SELECT * FROM p3_dual40_cycles
                ORDER BY COALESCE(resolved_at_ms,created_at_ms),id
                """
            ).fetchall()
        ]
        by_scope: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_scope[str(row.get("scope") or "UNKNOWN")].append(row)
            asset = str(row.get("combo_key") or "UNKNOWN").partition(":")[0]
            by_asset[asset].append(row)

        recent = list(reversed(rows))[: max(1, min(500, int(limit)))]
        return {
            "strategy": DUAL40_STRATEGY,
            "ladder": list(DEFAULT_LADDER),
            "state": {
                scope: ladder_state(conn, scope)
                for scope in ("PAPER", "LIVE")
            },
            "active_cycle": active_cycle(conn),
            "scan": read_scan_status(conn),
            "performance": {
                "PAPER": _scope_metrics(by_scope.get("PAPER", [])),
                "LIVE": _scope_metrics(by_scope.get("LIVE", [])),
            },
            "by_asset": {
                asset: _scope_metrics(asset_rows)
                for asset, asset_rows in sorted(by_asset.items())
            },
            "cycles": recent,
        }
    finally:
        conn.close()
