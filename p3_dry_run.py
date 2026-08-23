"""Window-level DRY execution analytics for P3 structural arbitrage.

A scanner can observe the same structural discrepancy many times while one
opportunity window is open. Counting those observations as independent trades
inflates sample size and PnL.

P3.6 adds entry-survival confirmation. Instead of automatically treating the
first positive print as a trade, the DRY policy waits for a configurable number
of milliseconds and selects the first still-positive opportunity at/after that
confirmation point. This rejects short-lived two-book repricing gaps while
preserving the one-attempt-per-independent-window rule.

It is analytics only: no credentials, signatures or order submission exist here.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from p3_config import P3Settings


def wilson_lower(successes: int, total: int, z: float = 1.959963984540054) -> float:
    if total <= 0:
        return 0.0
    n = float(total)
    p = float(successes) / n
    z2 = z * z
    centre = p + z2 / (2.0 * n)
    spread = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)
    return max(0.0, (centre - spread) / (1.0 + z2 / n))


def _window_rows(
    conn: sqlite3.Connection,
    replay_delay_ms: int,
    confirm_ms: int,
) -> list[sqlite3.Row]:
    """Return every window and its first surviving opportunity, if one exists.

    A LEFT JOIN is deliberate: windows that die before confirmation must remain in
    the denominator and be reported as skipped/pending rather than disappearing.
    """
    return conn.execute(
        """
        SELECT
            w.id AS window_id,
            w.strategy,
            w.condition_id,
            w.combo_key,
            w.opened_ts_ms,
            w.last_seen_ts_ms,
            w.closed_ts_ms,
            w.status AS window_status,
            o.id AS opportunity_id,
            o.detected_ts_ms,
            o.quantity_shares,
            o.capital_usdc,
            o.net_profit_usdc AS theoretical_net_profit_usdc,
            o.net_roi AS theoretical_net_roi,
            r.outcome,
            r.both_fill,
            r.cycle_net_pnl_usdc,
            r.unwind_loss_usdc
        FROM p3_windows AS w
        LEFT JOIN p3_opportunities AS o
          ON o.id = (
              SELECT o2.id
              FROM p3_opportunities AS o2
              WHERE o2.strategy=w.strategy
                AND o2.condition_id=w.condition_id
                AND o2.detected_ts_ms>=w.opened_ts_ms + ?
                AND o2.detected_ts_ms<=w.last_seen_ts_ms
              ORDER BY o2.detected_ts_ms,o2.id
              LIMIT 1
          )
        LEFT JOIN p3_replays AS r
          ON r.opportunity_id=o.id AND r.delay_ms=?
        ORDER BY w.opened_ts_ms,w.id
        """,
        (int(confirm_ms), int(replay_delay_ms)),
    ).fetchall()


def _build_policy_summary(
    conn: sqlite3.Connection,
    settings: P3Settings,
    *,
    confirm_ms: int,
) -> dict[str, Any]:
    rows = _window_rows(conn, settings.dry_latency_ms, confirm_ms)
    bankroll = float(settings.dry_start_bankroll_usdc)
    high_water = bankroll
    max_drawdown = 0.0
    cumulative_pnl = 0.0
    attempts: list[dict[str, Any]] = []
    pair_fills = 0
    one_leg = 0
    positive = 0
    negative = 0
    pending_replay = 0
    pending_confirmation = 0
    skipped_confirmation = 0
    skipped_capital = 0
    skipped_edge = 0
    confirmed_windows = 0

    for row in rows:
        window_id = int(row["window_id"])
        opened_ts_ms = int(row["opened_ts_ms"])
        confirmation_target_ts_ms = opened_ts_ms + int(confirm_ms)
        opportunity_id = row["opportunity_id"]
        window_status = str(row["window_status"])

        if opportunity_id is None:
            if window_status == "OPEN":
                status = "PENDING_CONFIRMATION"
                pending_confirmation += 1
            else:
                status = "SKIPPED_CONFIRMATION"
                skipped_confirmation += 1
            attempts.append(
                {
                    "window_id": window_id,
                    "combo_key": str(row["combo_key"]),
                    "strategy": str(row["strategy"]),
                    "opened_ts_ms": opened_ts_ms,
                    "window_status": window_status,
                    "confirmation_target_ts_ms": confirmation_target_ts_ms,
                    "entry_confirm_ms": int(confirm_ms),
                    "entry_age_ms": None,
                    "opportunity_id": None,
                    "quantity_shares": None,
                    "capital_usdc": None,
                    "theoretical_net_profit_usdc": None,
                    "theoretical_net_roi": None,
                    "replay_outcome": None,
                    "dry_status": status,
                    "cycle_net_pnl_usdc": None,
                    "bankroll_after_usdc": None,
                }
            )
            continue

        confirmed_windows += 1
        detected_ts_ms = int(row["detected_ts_ms"])
        capital = float(row["capital_usdc"])
        theoretical = float(row["theoretical_net_profit_usdc"])
        roi = float(row["theoretical_net_roi"])
        status = "PENDING_REPLAY"
        pnl = None

        if capital > float(settings.max_capital_per_cycle_usdc) + 1e-9:
            status = "SKIPPED_CAPITAL_LIMIT"
            skipped_capital += 1
        elif theoretical < float(settings.dry_min_net_profit_usdc) or roi < float(settings.dry_min_net_roi):
            status = "SKIPPED_EDGE_GATE"
            skipped_edge += 1
        elif row["outcome"] is None or row["cycle_net_pnl_usdc"] is None:
            pending_replay += 1
        else:
            status = "DRY_EXECUTED"
            pnl = float(row["cycle_net_pnl_usdc"])
            cumulative_pnl += pnl
            bankroll += pnl
            high_water = max(high_water, bankroll)
            max_drawdown = max(max_drawdown, high_water - bankroll)
            if int(row["both_fill"] or 0):
                pair_fills += 1
            if str(row["outcome"] or "").startswith("ONE_LEG"):
                one_leg += 1
            if pnl > 0:
                positive += 1
            elif pnl < 0:
                negative += 1

        attempts.append(
            {
                "window_id": window_id,
                "combo_key": str(row["combo_key"]),
                "strategy": str(row["strategy"]),
                "opened_ts_ms": opened_ts_ms,
                "window_status": window_status,
                "confirmation_target_ts_ms": confirmation_target_ts_ms,
                "entry_confirm_ms": int(confirm_ms),
                "entry_age_ms": detected_ts_ms - opened_ts_ms,
                "opportunity_id": int(opportunity_id),
                "quantity_shares": float(row["quantity_shares"]),
                "capital_usdc": capital,
                "theoretical_net_profit_usdc": theoretical,
                "theoretical_net_roi": roi,
                "replay_outcome": (str(row["outcome"]) if row["outcome"] is not None else None),
                "dry_status": status,
                "cycle_net_pnl_usdc": pnl,
                "bankroll_after_usdc": bankroll if status == "DRY_EXECUTED" else None,
            }
        )

    executed = sum(1 for item in attempts if item["dry_status"] == "DRY_EXECUTED")
    pair_rate = pair_fills / executed if executed else 0.0
    one_leg_rate = one_leg / executed if executed else 0.0
    average_pnl = cumulative_pnl / executed if executed else 0.0
    wilson = wilson_lower(pair_fills, executed)
    survival_rate = confirmed_windows / len(rows) if rows else 0.0

    reasons: list[str] = []
    if executed < settings.readiness_min_windows:
        reasons.append(f"INSUFFICIENT_INDEPENDENT_WINDOWS:{executed}/{settings.readiness_min_windows}")
    if pair_rate < settings.readiness_min_pair_completion:
        reasons.append(f"PAIR_COMPLETION_TOO_LOW:{pair_rate:.4f}")
    if wilson < settings.readiness_min_pair_wilson_lower:
        reasons.append(f"PAIR_WILSON_LOWER_TOO_LOW:{wilson:.4f}")
    if one_leg_rate > settings.readiness_max_one_leg_rate:
        reasons.append(f"ONE_LEG_RATE_TOO_HIGH:{one_leg_rate:.4f}")
    if cumulative_pnl <= 0:
        reasons.append(f"CUMULATIVE_PNL_NOT_POSITIVE:{cumulative_pnl:.6f}")
    if average_pnl <= 0:
        reasons.append(f"AVERAGE_PNL_NOT_POSITIVE:{average_pnl:.6f}")
    if max_drawdown > settings.readiness_max_drawdown_usdc:
        reasons.append(f"MAX_DRAWDOWN_EXCEEDED:{max_drawdown:.6f}")

    return {
        "enabled": bool(settings.dry_enabled),
        "policy": "ONE_CONFIRMED_ENTRY_PER_INDEPENDENT_WINDOW",
        "entry_confirm_ms": int(confirm_ms),
        "latency_ms": int(settings.dry_latency_ms),
        "start_bankroll_usdc": float(settings.dry_start_bankroll_usdc),
        "bankroll_usdc": bankroll,
        "cumulative_pnl_usdc": cumulative_pnl,
        "average_pnl_usdc": average_pnl,
        "max_drawdown_usdc": max_drawdown,
        "windows_seen": len(rows),
        "confirmed_windows": confirmed_windows,
        "confirmation_survival_rate": survival_rate,
        "attempts_executed": executed,
        "pair_fills": pair_fills,
        "pair_completion_rate": pair_rate,
        "pair_completion_wilson_lower_95": wilson,
        "one_leg": one_leg,
        "one_leg_rate": one_leg_rate,
        "positive_attempts": positive,
        "negative_attempts": negative,
        "pending_confirmation": pending_confirmation,
        "skipped_confirmation": skipped_confirmation,
        "pending_replay": pending_replay,
        "skipped_capital": skipped_capital,
        "skipped_edge": skipped_edge,
        "max_capital_per_cycle_usdc": float(settings.max_capital_per_cycle_usdc),
        "readiness": {
            "status": "DRY_VALIDATED" if not reasons else "NOT_READY",
            "reasons": reasons,
            "min_windows": int(settings.readiness_min_windows),
            "min_pair_completion": float(settings.readiness_min_pair_completion),
            "min_pair_wilson_lower": float(settings.readiness_min_pair_wilson_lower),
            "max_one_leg_rate": float(settings.readiness_max_one_leg_rate),
            "max_drawdown_usdc": float(settings.readiness_max_drawdown_usdc),
        },
        "recent_attempts": list(reversed(attempts[-30:])),
    }


def _compact_policy(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_confirm_ms": int(summary["entry_confirm_ms"]),
        "windows_seen": int(summary["windows_seen"]),
        "confirmed_windows": int(summary["confirmed_windows"]),
        "confirmation_survival_rate": float(summary["confirmation_survival_rate"]),
        "attempts_executed": int(summary["attempts_executed"]),
        "skipped_confirmation": int(summary["skipped_confirmation"]),
        "pending_confirmation": int(summary["pending_confirmation"]),
        "pending_replay": int(summary["pending_replay"]),
        "pair_fills": int(summary["pair_fills"]),
        "pair_completion_rate": float(summary["pair_completion_rate"]),
        "pair_completion_wilson_lower_95": float(summary["pair_completion_wilson_lower_95"]),
        "one_leg": int(summary["one_leg"]),
        "one_leg_rate": float(summary["one_leg_rate"]),
        "cumulative_pnl_usdc": float(summary["cumulative_pnl_usdc"]),
        "average_pnl_usdc": float(summary["average_pnl_usdc"]),
        "max_drawdown_usdc": float(summary["max_drawdown_usdc"]),
    }


def build_dry_summary(conn: sqlite3.Connection, settings: P3Settings) -> dict[str, Any]:
    """Build the active confirmed-entry DRY result plus a same-data policy grid."""
    primary_confirm = int(settings.dry_entry_confirm_ms)
    primary = _build_policy_summary(conn, settings, confirm_ms=primary_confirm)

    confirms = set(settings.dry_survival_delays())
    confirms.add(0)
    confirms.add(primary_confirm)
    survival: dict[str, dict[str, Any]] = {}
    for confirm_ms in sorted(confirms):
        if confirm_ms == primary_confirm:
            summary = primary
        else:
            summary = _build_policy_summary(conn, settings, confirm_ms=confirm_ms)
        survival[str(confirm_ms)] = _compact_policy(summary)

    baseline = survival.get("0")
    if baseline is not None:
        for item in survival.values():
            item["pnl_delta_vs_0_usdc"] = (
                float(item["cumulative_pnl_usdc"])
                - float(baseline["cumulative_pnl_usdc"])
            )
            item["one_leg_rate_delta_vs_0"] = (
                float(item["one_leg_rate"])
                - float(baseline["one_leg_rate"])
            )
            item["pair_completion_delta_vs_0"] = (
                float(item["pair_completion_rate"])
                - float(baseline["pair_completion_rate"])
            )

    primary["survival_by_confirm_ms"] = survival
    return primary
