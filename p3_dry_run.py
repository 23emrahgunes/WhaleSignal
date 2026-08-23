"""Independent-window DRY analytics for P3 structural arbitrage.

P3.6.1 separates two evidence levels:

- STRICT: every positive scanner touch is timestamped, confirmation must be a
  continuous positive chain, and execution is replayed from the actual confirmed
  observation timestamp.
- LEGACY / INDICATIVE: pre-timeline windows can still be inspected, but they are
  excluded from strict bankroll/readiness because continuity cannot be reconstructed.

No credentials, signatures or order submission exist here.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from p3_config import P3Settings
from p3_confirmation import (
    CONFIRMED,
    CONFIRMATION_GAP,
    LEGACY_CONFIRMATION_UNPROVEN,
    PENDING_CONFIRMATION,
    SKIPPED_CONFIRMATION,
    select_confirmed_observation,
)


def wilson_lower(successes: int, total: int, z: float = 1.959963984540054) -> float:
    if total <= 0:
        return 0.0
    n = float(total)
    p = float(successes) / n
    z2 = z * z
    centre = p + z2 / (2.0 * n)
    spread = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)
    return max(0.0, (centre - spread) / (1.0 + z2 / n))


def _legacy_window_rows(
    conn: sqlite3.Connection,
    replay_delay_ms: int,
    confirm_ms: int,
) -> list[sqlite3.Row]:
    """Old coarse confirmation model, retained only for indicative comparison."""
    return conn.execute(
        """
        SELECT
            w.id AS window_id,w.strategy,w.condition_id,w.combo_key,
            w.opened_ts_ms,w.last_seen_ts_ms,w.closed_ts_ms,w.status AS window_status,
            o.id AS opportunity_id,o.detected_ts_ms,o.quantity_shares,o.capital_usdc,
            o.net_profit_usdc AS theoretical_net_profit_usdc,o.net_roi AS theoretical_net_roi,
            r.outcome,r.both_fill,r.cycle_net_pnl_usdc,r.unwind_loss_usdc
        FROM p3_windows AS w
        LEFT JOIN p3_opportunities AS o
          ON o.id = (
              SELECT o2.id FROM p3_opportunities AS o2
              WHERE o2.strategy=w.strategy
                AND o2.condition_id=w.condition_id
                AND o2.detected_ts_ms>=w.opened_ts_ms + ?
                AND o2.detected_ts_ms<=w.last_seen_ts_ms
              ORDER BY o2.detected_ts_ms,o2.id LIMIT 1
          )
        LEFT JOIN p3_replays AS r
          ON r.opportunity_id=o.id AND r.delay_ms=?
        ORDER BY w.opened_ts_ms,w.id
        """,
        (int(confirm_ms), int(replay_delay_ms)),
    ).fetchall()


def _build_legacy_indicative(
    conn: sqlite3.Connection,
    settings: P3Settings,
    *,
    confirm_ms: int,
) -> dict[str, Any]:
    rows = _legacy_window_rows(conn, settings.dry_latency_ms, confirm_ms)
    bankroll = float(settings.dry_start_bankroll_usdc)
    high_water = bankroll
    cumulative = 0.0
    max_dd = 0.0
    executed = pair = one_leg = 0
    for row in rows:
        if row["opportunity_id"] is None:
            continue
        capital = float(row["capital_usdc"])
        theoretical = float(row["theoretical_net_profit_usdc"])
        roi = float(row["theoretical_net_roi"])
        if capital > float(settings.max_capital_per_cycle_usdc) + 1e-9:
            continue
        if theoretical < float(settings.dry_min_net_profit_usdc) or roi < float(settings.dry_min_net_roi):
            continue
        if row["outcome"] is None or row["cycle_net_pnl_usdc"] is None:
            continue
        pnl = float(row["cycle_net_pnl_usdc"])
        executed += 1
        cumulative += pnl
        bankroll += pnl
        high_water = max(high_water, bankroll)
        max_dd = max(max_dd, high_water - bankroll)
        if int(row["both_fill"] or 0):
            pair += 1
        if str(row["outcome"] or "").startswith("ONE_LEG"):
            one_leg += 1
    return {
        "evidence_level": "LEGACY_INDICATIVE_ONLY",
        "entry_confirm_ms": int(confirm_ms),
        "windows_seen": len(rows),
        "attempts_executed": executed,
        "pair_completion_rate": pair / executed if executed else 0.0,
        "one_leg_rate": one_leg / executed if executed else 0.0,
        "cumulative_pnl_usdc": cumulative,
        "bankroll_usdc": bankroll,
        "max_drawdown_usdc": max_dd,
        "note": "Coarse pre-timeline confirmation; excluded from strict readiness.",
    }


def _strict_windows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id,strategy,condition_id,combo_key,opened_ts_ms,last_seen_ts_ms,
               closed_ts_ms,status
        FROM p3_windows ORDER BY opened_ts_ms,id
        """
    ).fetchall()


def _build_policy_summary(
    conn: sqlite3.Connection,
    settings: P3Settings,
    *,
    confirm_ms: int,
) -> dict[str, Any]:
    windows = _strict_windows(conn)
    bankroll = float(settings.dry_start_bankroll_usdc)
    high_water = bankroll
    max_drawdown = 0.0
    cumulative_pnl = 0.0
    attempts: list[dict[str, Any]] = []
    pair_fills = one_leg = positive = negative = 0
    pending_replay = pending_confirmation = skipped_confirmation = 0
    confirmation_gaps = legacy_unproven = 0
    skipped_capital = skipped_edge = confirmed_windows = 0
    strict_timeline_windows = 0

    for window in windows:
        window_id = int(window["id"])
        opened = int(window["opened_ts_ms"])
        selection = select_confirmed_observation(
            conn,
            window_id=window_id,
            confirm_ms=int(confirm_ms),
            max_gap_ms=int(settings.dry_confirm_max_gap_ms),
        )

        base = {
            "window_id": window_id,
            "combo_key": str(window["combo_key"]),
            "strategy": str(window["strategy"]),
            "opened_ts_ms": opened,
            "window_status": str(window["status"]),
            "confirmation_target_ts_ms": int(selection.target_ts_ms),
            "entry_confirm_ms": int(confirm_ms),
            "max_confirmation_gap_ms": int(settings.dry_confirm_max_gap_ms),
            "max_gap_seen_ms": selection.max_gap_seen_ms,
            "confirmation_reason": selection.reason,
        }

        if selection.status == LEGACY_CONFIRMATION_UNPROVEN:
            legacy_unproven += 1
            attempts.append({
                **base,
                "entry_age_ms": None,
                "observation_id": None,
                "opportunity_id": None,
                "quantity_shares": None,
                "capital_usdc": None,
                "theoretical_net_profit_usdc": None,
                "theoretical_net_roi": None,
                "replay_outcome": None,
                "dry_status": LEGACY_CONFIRMATION_UNPROVEN,
                "cycle_net_pnl_usdc": None,
                "bankroll_after_usdc": None,
            })
            continue

        strict_timeline_windows += 1
        if selection.status == CONFIRMATION_GAP:
            confirmation_gaps += 1
        elif selection.status == PENDING_CONFIRMATION:
            pending_confirmation += 1
        elif selection.status == SKIPPED_CONFIRMATION:
            skipped_confirmation += 1

        if selection.status != CONFIRMED:
            attempts.append({
                **base,
                "entry_age_ms": None,
                "observation_id": None,
                "opportunity_id": None,
                "quantity_shares": None,
                "capital_usdc": None,
                "theoretical_net_profit_usdc": None,
                "theoretical_net_roi": None,
                "replay_outcome": None,
                "dry_status": selection.status,
                "cycle_net_pnl_usdc": None,
                "bankroll_after_usdc": None,
            })
            continue

        confirmed_windows += 1
        assert selection.opportunity_id is not None
        assert selection.observation_id is not None
        assert selection.entry_ts_ms is not None
        opp = conn.execute(
            "SELECT * FROM p3_opportunities WHERE id=?",
            (int(selection.opportunity_id),),
        ).fetchone()
        if opp is None:
            raise RuntimeError(f"confirmed opportunity missing: {selection.opportunity_id}")
        replay = conn.execute(
            """
            SELECT * FROM p3_entry_replays
            WHERE window_id=? AND confirm_ms=? AND delay_ms=?
            """,
            (window_id, int(confirm_ms), int(settings.dry_latency_ms)),
        ).fetchone()

        capital = float(opp["capital_usdc"])
        theoretical = float(opp["net_profit_usdc"])
        roi = float(opp["net_roi"])
        status = "PENDING_ENTRY_REPLAY"
        pnl = None
        replay_outcome = None

        if capital > float(settings.max_capital_per_cycle_usdc) + 1e-9:
            status = "SKIPPED_CAPITAL_LIMIT"
            skipped_capital += 1
        elif theoretical < float(settings.dry_min_net_profit_usdc) or roi < float(settings.dry_min_net_roi):
            status = "SKIPPED_EDGE_GATE"
            skipped_edge += 1
        elif replay is None or replay["cycle_net_pnl_usdc"] is None:
            pending_replay += 1
            replay_outcome = str(replay["outcome"]) if replay is not None else None
        else:
            status = "DRY_EXECUTED"
            replay_outcome = str(replay["outcome"])
            pnl = float(replay["cycle_net_pnl_usdc"])
            cumulative_pnl += pnl
            bankroll += pnl
            high_water = max(high_water, bankroll)
            max_drawdown = max(max_drawdown, high_water - bankroll)
            if int(replay["both_fill"] or 0):
                pair_fills += 1
            if replay_outcome.startswith("ONE_LEG"):
                one_leg += 1
            if pnl > 0:
                positive += 1
            elif pnl < 0:
                negative += 1

        attempts.append({
            **base,
            "entry_age_ms": int(selection.entry_ts_ms) - opened,
            "observation_id": int(selection.observation_id),
            "opportunity_id": int(selection.opportunity_id),
            "quantity_shares": float(opp["quantity_shares"]),
            "capital_usdc": capital,
            "theoretical_net_profit_usdc": theoretical,
            "theoretical_net_roi": roi,
            "replay_outcome": replay_outcome,
            "dry_status": status,
            "cycle_net_pnl_usdc": pnl,
            "bankroll_after_usdc": bankroll if status == "DRY_EXECUTED" else None,
        })

    executed = sum(1 for item in attempts if item["dry_status"] == "DRY_EXECUTED")
    pair_rate = pair_fills / executed if executed else 0.0
    one_leg_rate = one_leg / executed if executed else 0.0
    average_pnl = cumulative_pnl / executed if executed else 0.0
    wilson = wilson_lower(pair_fills, executed)
    survival_rate = confirmed_windows / strict_timeline_windows if strict_timeline_windows else 0.0

    reasons: list[str] = []
    if strict_timeline_windows == 0:
        reasons.append("NO_STRICT_TIMELINE_EVIDENCE")
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
        "evidence_level": "STRICT_CONTINUOUS_TIMELINE",
        "policy": "ONE_STRICT_CONFIRMED_ENTRY_PER_INDEPENDENT_WINDOW",
        "entry_confirm_ms": int(confirm_ms),
        "confirm_max_gap_ms": int(settings.dry_confirm_max_gap_ms),
        "latency_ms": int(settings.dry_latency_ms),
        "start_bankroll_usdc": float(settings.dry_start_bankroll_usdc),
        "bankroll_usdc": bankroll,
        "cumulative_pnl_usdc": cumulative_pnl,
        "average_pnl_usdc": average_pnl,
        "max_drawdown_usdc": max_drawdown,
        "windows_seen": len(windows),
        "strict_timeline_windows": strict_timeline_windows,
        "legacy_unproven_windows": legacy_unproven,
        "confirmed_windows": confirmed_windows,
        "confirmation_survival_rate": survival_rate,
        "confirmation_gaps": confirmation_gaps,
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
    keys = (
        "entry_confirm_ms", "windows_seen", "strict_timeline_windows",
        "legacy_unproven_windows", "confirmed_windows", "confirmation_survival_rate",
        "confirmation_gaps", "attempts_executed", "skipped_confirmation",
        "pending_confirmation", "pending_replay", "pair_fills",
        "pair_completion_rate", "pair_completion_wilson_lower_95", "one_leg",
        "one_leg_rate", "cumulative_pnl_usdc", "average_pnl_usdc", "max_drawdown_usdc",
    )
    return {key: summary[key] for key in keys}


def build_dry_summary(conn: sqlite3.Connection, settings: P3Settings) -> dict[str, Any]:
    """Build strict active DRY evidence and keep old coarse evidence visibly separate."""
    primary_confirm = int(settings.dry_entry_confirm_ms)
    primary = _build_policy_summary(conn, settings, confirm_ms=primary_confirm)

    confirms = set(settings.dry_survival_delays())
    confirms.add(0)
    confirms.add(primary_confirm)
    survival: dict[str, dict[str, Any]] = {}
    for confirm_ms in sorted(confirms):
        summary = primary if confirm_ms == primary_confirm else _build_policy_summary(
            conn, settings, confirm_ms=confirm_ms
        )
        survival[str(confirm_ms)] = _compact_policy(summary)

    baseline = survival.get("0")
    if baseline is not None:
        for item in survival.values():
            item["pnl_delta_vs_0_usdc"] = (
                float(item["cumulative_pnl_usdc"])
                - float(baseline["cumulative_pnl_usdc"])
            )
            item["one_leg_rate_delta_vs_0"] = (
                float(item["one_leg_rate"]) - float(baseline["one_leg_rate"])
            )
            item["pair_completion_delta_vs_0"] = (
                float(item["pair_completion_rate"])
                - float(baseline["pair_completion_rate"])
            )

    primary["survival_by_confirm_ms"] = survival
    primary["legacy_indicative"] = _build_legacy_indicative(
        conn, settings, confirm_ms=primary_confirm
    )
    return primary
