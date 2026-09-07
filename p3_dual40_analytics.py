"""Read-only operational analytics for the DUAL40 cohort."""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any

from p3_dual40_core import DEFAULT_LADDER, DUAL40_STRATEGY
from p3_dual40_store import (
    active_cycle,
    active_cycles,
    connect_dual40,
    DUAL40_ASSETS,
    ladder_state,
    market_decisions,
    read_scan_status,
)
from p3_schema import open_p26_read_only


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
    for source, target in (("gate_json", "gate"), ("details_json", "details")):
        try:
            item[target] = json.loads(str(item.pop(source, "{}") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            item[target] = {}
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
    recovery = [row for row in rows if int(row.get("level_index") or 0) > 0]
    recovery_settled = [row for row in recovery if row.get("realized_pnl_usdc") is not None]
    recovery_pnl = sum(float(row.get("realized_pnl_usdc") or 0.0) for row in recovery_settled)
    recovery_durations = [
        (int(row["resolved_at_ms"]) - int(row["created_at_ms"])) / 1000.0
        for row in recovery_settled
        if row.get("resolved_at_ms") is not None and row.get("created_at_ms") is not None
    ]
    return {
        "cycles": len(rows),
        "settled": len(settled),
        "wins": wins,
        "losses": losses,
        "flat": flats,
        "hit_rate": wins / len(settled) if settled else None,
        "realized_pnl_usdc": round(pnl, 6),
        "average_pnl_usdc": round(pnl / len(settled), 6) if settled else None,
        "ev_per_settled_usdc": round(pnl / len(settled), 6) if settled else None,
        "max_drawdown_usdc": round(_drawdown(settled), 6),
        "matched_cycles": matched,
        "single_leg_cycles": single_leg,
        "partial_unequal_cycles": partial_unequal,
        "no_fill_cycles": no_fill,
        "pair_completion_rate": matched / denomin if denomin else None,
        "single_leg_rate": single_leg / denomin if denomin else None,
        "near_touch_41_cycles": touched_41,
        "recovery_cycles": len(recovery),
        "recovery_settled": len(recovery_settled),
        "recovery_realized_pnl_usdc": round(recovery_pnl, 6),
        "average_recovery_duration_sec": (
            round(sum(recovery_durations) / len(recovery_durations), 3)
            if recovery_durations
            else None
        ),
    }


def _decision_metrics(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    rejected = [
        item
        for item in decisions
        if str(item.get("decision") or "").startswith("REJECTED")
        or str(item.get("decision") or "").startswith("SKIPPED")
    ]
    reason_counts: dict[str, int] = defaultdict(int)
    for item in rejected:
        reason_counts[str(item.get("reason") or "UNKNOWN")] += 1
    return {
        "markets_seen": len(decisions),
        "markets_eligible": sum(bool(item.get("eligible")) for item in decisions),
        "would_open_base": sum(bool((item.get("final_gate") or {}).get("would_open_base")) for item in decisions),
        "would_open_opening": sum(bool((item.get("final_gate") or {}).get("would_open_opening")) for item in decisions),
        "would_open_opening_forecast": sum(
            bool((item.get("final_gate") or {}).get("would_open_opening_forecast"))
            for item in decisions
        ),
        "would_open_strict_recovery": sum(
            bool((item.get("final_gate") or {}).get("would_open_strict_recovery"))
            for item in decisions
        ),
        "rejected": len(rejected),
        "markets_skipped": len(rejected),
        "reason_counts": dict(reason_counts),
    }


def _cohort_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields = {
        "base": "would_open_base",
        "opening": "would_open_opening",
        "opening_forecast": "would_open_opening_forecast",
        "strict_recovery": "would_open_strict_recovery",
    }
    return {
        name: _scope_metrics(
            [row for row in rows if bool((row.get("gate") or {}).get(field))]
        )
        for name, field in fields.items()
    }


def p26_paper_decision_summary(
    path: str,
    *,
    strategy_version: str = "RESEARCH_PAPER_V2",
    limit: int = 50,
) -> dict[str, Any]:
    if not Path(path).exists():
        return {"status": "DATABASE_NOT_FOUND", "candidates": 0, "recent": []}
    conn = open_p26_read_only(path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='p26_paper_decisions'"
        ).fetchone()
        if exists is None:
            return {"status": "AUDIT_TABLE_NOT_READY", "candidates": 0, "recent": []}
        totals = conn.execute(
            """
            SELECT COUNT(*) AS candidates,COALESCE(SUM(would_open),0) AS would_open,
                   COALESCE(SUM(CASE WHEN decision='OPENED' THEN 1 ELSE 0 END),0) AS opened
            FROM p26_paper_decisions WHERE strategy_version=?
            """,
            (str(strategy_version),),
        ).fetchone()
        reasons = conn.execute(
            """
            SELECT reason,COUNT(*) AS n FROM p26_paper_decisions
            WHERE strategy_version=? GROUP BY reason ORDER BY n DESC,reason
            """,
            (str(strategy_version),),
        ).fetchall()
        recent_rows = conn.execute(
            """
            SELECT condition_id,combo_key,horizon,decision_ts_ms,observed_at_ms,
                   stage,decision,reason,eligible,would_open,model_artifact_id,
                   alpha_artifact_id
            FROM p26_paper_decisions WHERE strategy_version=?
            ORDER BY updated_at_ms DESC,id DESC LIMIT ?
            """,
            (str(strategy_version), max(1, min(500, int(limit)))),
        ).fetchall()
        readiness_row = conn.execute(
            "SELECT value FROM p26_meta WHERE key='paper_v2_audit_status_json'"
        ).fetchone()
        try:
            readiness = (
                json.loads(str(readiness_row["value"]))
                if readiness_row is not None
                else None
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            readiness = None
        reason_counts = {str(row["reason"]): int(row["n"]) for row in reasons}
        not_ready = any(
            key.startswith("MODEL_ARTIFACT_NOT_READY")
            or key.startswith("ALPHA_ARTIFACT_NOT_READY")
            for key in reason_counts
        )
        return {
            "status": (
                str(readiness.get("status") or "OBSERVING")
                if isinstance(readiness, dict)
                else ("NOT_READY" if not_ready else "OBSERVING")
            ),
            "readiness": readiness,
            "candidates": int(totals["candidates"] if totals else 0),
            "would_open": int(totals["would_open"] if totals else 0),
            "opened": int(totals["opened"] if totals else 0),
            "reason_counts": reason_counts,
            "recent": [dict(row) for row in recent_rows],
        }
    finally:
        conn.close()


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
        by_scope_asset: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            scope = str(row.get("scope") or "UNKNOWN")
            by_scope[scope].append(row)
            asset = str(row.get("asset") or row.get("combo_key") or "UNKNOWN").partition(":")[0]
            by_scope_asset[(scope, asset)].append(row)

        recent = list(reversed(rows))[: max(1, min(500, int(limit)))]
        active = active_cycles(conn)
        active_by_asset = {
            f"{cycle.get('scope')}:{cycle.get('asset')}": cycle
            for cycle in active
        }
        states = {
            scope: {
                asset: {
                    **ladder_state(conn, scope, asset),
                    "active_cycle": active_by_asset.get(f"{scope}:{asset}"),
                }
                for asset in DUAL40_ASSETS
            }
            for scope in ("PAPER", "LIVE")
        }
        all_decisions = market_decisions(conn, limit=None)
        decisions = all_decisions[: max(1, min(500, int(limit)))]
        legacy_row = conn.execute(
            "SELECT value FROM p3_meta WHERE key='dual40_legacy_global_state_review_required'"
        ).fetchone()
        try:
            migration_review = json.loads(str(legacy_row["value"])) if legacy_row else None
        except (TypeError, ValueError, json.JSONDecodeError):
            migration_review = {"status": "LEGACY_GLOBAL_STATE_REVIEW_REQUIRED"}
        decisions_by_scope_asset: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for decision in all_decisions:
            decision_scope = str(decision.get("scope") or "UNKNOWN")
            decision_asset = str(decision.get("asset") or "UNKNOWN")
            decisions_by_scope_asset[(decision_scope, decision_asset)].append(decision)
        paper_rows = by_scope.get("PAPER", [])
        return {
            "strategy": DUAL40_STRATEGY,
            "ladder": list(DEFAULT_LADDER),
            "state": states,
            "active_cycle": active_cycle(conn),
            "active_cycles": active,
            "scan": read_scan_status(conn),
            "market_decisions": decisions,
            "migration_review": migration_review,
            "performance": {
                "PAPER": _scope_metrics(paper_rows),
                "LIVE": _scope_metrics(by_scope.get("LIVE", [])),
            },
            "decision_metrics": _decision_metrics(all_decisions),
            "observed_cohorts": _cohort_metrics(paper_rows),
            "cohort_note": "Observed opened-cycle subsets; not a causal or profitability claim.",
            "by_asset": {
                asset: {
                    **_scope_metrics(by_scope_asset.get(("PAPER", asset), [])),
                    **_decision_metrics(
                        decisions_by_scope_asset.get(("PAPER", asset), [])
                    ),
                    "performance": {
                        scope: _scope_metrics(by_scope_asset.get((scope, asset), []))
                        for scope in ("PAPER", "LIVE")
                    },
                    "decisions": {
                        scope: _decision_metrics(
                            decisions_by_scope_asset.get((scope, asset), [])
                        )
                        for scope in ("PAPER", "LIVE")
                    },
                    "state": {
                        "PAPER": {
                            **states["PAPER"][asset],
                            "recovery_debt_usdc": float(states["PAPER"][asset]["loss_pool_usdc"]),
                            "markets_skipped": _decision_metrics(
                                decisions_by_scope_asset.get(("PAPER", asset), [])
                            )["markets_skipped"],
                        },
                        "LIVE": states["LIVE"][asset],
                    },
                }
                for asset in DUAL40_ASSETS
            },
            "cycles": recent,
        }
    finally:
        conn.close()
