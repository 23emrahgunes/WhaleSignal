"""Tick-level dual-side low-price paper-entry evaluator for P2.5.

The strategy is deliberately paper-only.  A validated research probability is used
for both binary outcomes: P(UP)=p and P(DOWN)=1-p.  Each side is independently
checked against the public P2.6 full-depth CLOB snapshot.  At most one $1-style paper
entry is admitted per market: the executable candidate with the strongest value
multiple wins.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from p25_paper import PaperEntryDecision, PaperPolicy
from p26_fee import FeeSchedule


@dataclass(frozen=True)
class DeepValueDepth:
    token_id: str
    bid: float | None
    ask: float
    fill_price: float
    capacity_shares: float
    required_shares: float
    age_ms: int
    fee_usdc: float
    fee_source: str
    price_band: str


@dataclass(frozen=True)
class DeepValueCandidate:
    side: str
    selected_probability: float
    depth: DeepValueDepth
    edge: float
    value_multiple: float


def price_band(price: float) -> str:
    p = float(price)
    if p <= 0.03:
        return "01-03c"
    if p <= 0.05:
        return "03-05c"
    if p <= 0.10:
        return "05-10c"
    if p <= 0.15:
        return "10-15c"
    if p <= 0.25:
        return "15-25c"
    if p <= 0.45:
        return "25-45c"
    return "45c+"


def _levels(raw: object) -> list[tuple[float, float]]:
    try:
        values = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    out: list[tuple[float, float]] = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            price = float(item[0])
            size = max(0.0, float(item[1]))
        except (TypeError, ValueError):
            continue
        if 0.0 < price < 1.0 and size > 0:
            out.append((price, size))
    return out


def _fee_schedule(
    conn: sqlite3.Connection,
    condition_id: str,
    token_id: str,
) -> FeeSchedule | None:
    try:
        row = conn.execute(
            """
            SELECT condition_id,token_id,enabled,rate,exponent,taker_only,
                   source,source_ts_ms,formula_version
            FROM p26_fee_schedules
            WHERE condition_id=? AND token_id=?
            LIMIT 1
            """,
            (condition_id, token_id),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return FeeSchedule(
        condition_id=str(row["condition_id"]),
        token_id=str(row["token_id"]),
        enabled=bool(row["enabled"]),
        rate=float(row["rate"]),
        exponent=float(row["exponent"]),
        taker_only=bool(row["taker_only"]),
        source=str(row["source"]),
        source_ts_ms=int(row["source_ts_ms"]),
        formula_version=str(row["formula_version"]),
    )


def load_fresh_depth(
    *,
    db_path: str,
    condition_id: str,
    side: str,
    stake_usdc: float,
    slippage: float,
    max_age_ms: int,
    require_fee_schedule: bool,
    fallback_fee_bps: float,
    now_ms: int | None = None,
) -> tuple[DeepValueDepth | None, str]:
    """Read the latest public full-depth book and prove a fixed-dollar fill."""
    path = Path(db_path)
    if not path.exists():
        return None, "P26_DB_MISSING"
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=0.25)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT token_id,recv_ts_ms,bids_json,asks_json
            FROM p26_clob_books
            WHERE condition_id=? AND side=?
            ORDER BY recv_ts_ms DESC,id DESC
            LIMIT 1
            """,
            (str(condition_id), str(side).upper()),
        ).fetchone()
        if row is None:
            conn.close()
            return None, "DEPTH_MISSING"

        observed_now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        age_ms = max(0, observed_now - int(row["recv_ts_ms"]))
        if age_ms > int(max_age_ms):
            conn.close()
            return None, f"DEPTH_STALE_{age_ms}MS"

        asks = sorted(_levels(row["asks_json"]), key=lambda x: x[0])
        bids = sorted(_levels(row["bids_json"]), key=lambda x: x[0], reverse=True)
        if not asks:
            conn.close()
            return None, "ASK_DEPTH_EMPTY"

        ask = float(asks[0][0])
        bid = float(bids[0][0]) if bids else None
        fill = min(0.999, ask + max(0.0, float(slippage)))
        required = float(stake_usdc) / max(fill, 1e-12)
        capacity = sum(size for price, size in asks if price <= fill + 1e-12)
        if capacity + 1e-9 < required:
            conn.close()
            return None, f"DEPTH_INSUFFICIENT_{capacity:.6f}_LT_{required:.6f}"

        token_id = str(row["token_id"])
        schedule = _fee_schedule(conn, str(condition_id), token_id)
        if schedule is None and require_fee_schedule:
            conn.close()
            return None, "FEE_SCHEDULE_MISSING"
        if schedule is not None:
            fee = schedule.fee_usdc(shares=required, price=fill)
            fee_source = schedule.source
        else:
            fee = float(stake_usdc) * max(0.0, float(fallback_fee_bps)) / 10000.0
            fee_source = "PAPER_FEE_BPS_FALLBACK"
        conn.close()
        return (
            DeepValueDepth(
                token_id=token_id,
                bid=bid,
                ask=ask,
                fill_price=fill,
                capacity_shares=float(capacity),
                required_shares=float(required),
                age_ms=age_ms,
                fee_usdc=float(fee),
                fee_source=fee_source,
                price_band=price_band(ask),
            ),
            "OK",
        )
    except sqlite3.Error as exc:
        return None, f"DEPTH_DB_ERROR_{type(exc).__name__}"


def _forecast_gate(
    trace: dict[str, Any],
    policy: PaperPolicy,
) -> tuple[dict[str, Any] | None, str]:
    """Validate forecast quality, but do not force the forecast label as trade side."""
    if not bool(trace.get("feature_ready")):
        return None, "FEATURE_NOT_READY"

    status = str(trace.get("forecast_status") or "UNKNOWN").upper()
    if status not in policy.allowed_statuses:
        return None, f"STATUS_{status}_NOT_ALLOWED"
    grade = str(trace.get("forecast_grade") or "UNKNOWN").upper()
    if grade not in policy.allowed_grades:
        return None, f"GRADE_{grade}_NOT_ALLOWED"

    confidence = float(trace.get("forecast_confidence") or 0.0)
    if confidence < policy.min_confidence:
        return None, "LOW_CONFIDENCE"
    agreement = float(trace.get("forecast_agreement") or 0.0)
    if agreement < policy.min_agreement:
        return None, "LOW_AGREEMENT"

    p_up_raw = trace.get("forecast_p_up")
    if p_up_raw is None:
        return None, "FORECAST_PROBABILITY_MISSING"
    p_up = max(0.0, min(1.0, float(p_up_raw)))
    return {
        "p_up": p_up,
        "p_down": 1.0 - p_up,
        "confidence": confidence,
        "agreement": agreement,
    }, "OK"


def _snapshot_prices(snap, side: str) -> tuple[float | None, float | None]:  # noqa: ANN001
    if side == "UP":
        return snap.up_bid, snap.up_ask
    return snap.down_bid, snap.down_ask


def _candidate_for_side(
    *,
    side: str,
    selected_probability: float,
    ref,
    snap,
    policy: PaperPolicy,
    cfg,
    now_ms: int,
    available_bankroll_usdc: float,
) -> tuple[DeepValueCandidate | None, dict[str, Any]]:  # noqa: ANN001
    """Evaluate one outcome independently and return a fully executable candidate."""
    diag: dict[str, Any] = {
        "side": side,
        "selected_probability": selected_probability,
    }
    snap_bid, snap_ask = _snapshot_prices(snap, side)
    if snap_ask is None:
        diag["reason"] = "SNAP_ASK_MISSING"
        return None, diag

    max_prefilter = float(cfg.paper_deep_value_max_ask) + float(
        cfg.paper_deep_value_prefilter_buffer
    )
    if float(snap_ask) > max_prefilter:
        diag["reason"] = "WAITING_FOR_DIP"
        diag["snap_ask"] = float(snap_ask)
        return None, diag

    if bool(cfg.paper_deep_value_require_depth):
        depth, depth_reason = load_fresh_depth(
            db_path=str(cfg.paper_deep_value_p26_db_path),
            condition_id=str(ref.condition_id),
            side=side,
            stake_usdc=float(policy.stake_usdc),
            slippage=float(policy.slippage),
            max_age_ms=int(cfg.paper_deep_value_max_book_age_ms),
            require_fee_schedule=bool(cfg.paper_deep_value_require_fee_schedule),
            fallback_fee_bps=float(policy.fee_bps),
            now_ms=now_ms,
        )
        if depth is None:
            diag["reason"] = depth_reason
            return None, diag
    else:
        ask = float(snap_ask)
        fill = min(0.999, ask + float(policy.slippage))
        required = float(policy.stake_usdc) / max(fill, 1e-12)
        depth = DeepValueDepth(
            token_id=str(ref.up_token_id if side == "UP" else ref.down_token_id),
            bid=float(snap_bid) if snap_bid is not None else None,
            ask=ask,
            fill_price=fill,
            capacity_shares=required,
            required_shares=required,
            age_ms=0,
            fee_usdc=float(policy.stake_usdc) * float(policy.fee_bps) / 10000.0,
            fee_source="PAPER_FEE_BPS_FALLBACK",
            price_band=price_band(ask),
        )

    diag.update(
        {
            "depth_source": (
                "P26_FULL_DEPTH"
                if cfg.paper_deep_value_require_depth
                else "P25_BEST_ASK"
            ),
            "token_id": depth.token_id,
            "entry_ask": depth.ask,
            "fill_price": depth.fill_price,
            "depth_capacity_shares": depth.capacity_shares,
            "depth_required_shares": depth.required_shares,
            "depth_age_ms": depth.age_ms,
            "fee_source": depth.fee_source,
            "price_band": depth.price_band,
        }
    )

    if not float(cfg.paper_deep_value_min_ask) <= depth.ask <= float(
        cfg.paper_deep_value_max_ask
    ):
        diag["reason"] = "WAITING_FOR_DIP"
        return None, diag

    edge = float(selected_probability) - depth.fill_price
    value_multiple = float(selected_probability) / max(depth.fill_price, 1e-12)
    diag["forecast_edge"] = edge
    diag["value_multiple"] = value_multiple
    if edge + 1e-12 < float(policy.min_edge):
        diag["reason"] = "EDGE_BELOW_MINIMUM"
        return None, diag
    if value_multiple + 1e-12 < float(cfg.paper_deep_value_min_value_multiple):
        diag["reason"] = "VALUE_MULTIPLE_BELOW_MINIMUM"
        return None, diag

    stake = float(policy.stake_usdc)
    if available_bankroll_usdc + 1e-12 < stake + float(depth.fee_usdc):
        diag["reason"] = "INSUFFICIENT_PAPER_BANKROLL"
        return None, diag

    diag["reason"] = "ELIGIBLE"
    return (
        DeepValueCandidate(
            side=side,
            selected_probability=float(selected_probability),
            depth=depth,
            edge=float(edge),
            value_multiple=float(value_multiple),
        ),
        diag,
    )


def _best_failure_reason(side_diags: dict[str, dict[str, Any]]) -> str:
    """Expose the most informative rejection while retaining per-side diagnostics."""
    reasons = [str(side_diags.get(side, {}).get("reason") or "") for side in ("UP", "DOWN")]
    if reasons and all(reason == "WAITING_FOR_DIP" for reason in reasons):
        return "WAITING_FOR_DIP"
    priority = (
        "INSUFFICIENT_PAPER_BANKROLL",
        "FEE_SCHEDULE_MISSING",
        "DEPTH_DB_ERROR_",
        "DEPTH_STALE_",
        "DEPTH_INSUFFICIENT_",
        "ASK_DEPTH_EMPTY",
        "DEPTH_MISSING",
        "VALUE_MULTIPLE_BELOW_MINIMUM",
        "EDGE_BELOW_MINIMUM",
        "SNAP_ASK_MISSING",
    )
    for prefix in priority:
        for reason in reasons:
            if reason.startswith(prefix):
                return reason
    return next((reason for reason in reasons if reason), "NO_DUAL_SIDE_VALUE")


def evaluate_deep_value_watch(
    *,
    ref,
    snap,
    trace: dict[str, Any],
    policy: PaperPolicy,
    cfg,
    available_bankroll_usdc: float,
) -> tuple[PaperEntryDecision | None, dict[str, Any]]:  # noqa: ANN001
    """Scan UP and DOWN independently and open the strongest executable value side.

    The model's display direction is deliberately *not* the side gate.  Both P(UP)
    and P(DOWN)=1-P(UP) are priced against their own fresh ask.  A market still gets
    at most one paper entry because the recorder enforces one strategy attempt per
    condition id.
    """
    diag: dict[str, Any] = {
        "entry_mode": "DEEP_VALUE_WATCH",
        "scan_mode": "DUAL_SIDE_VALUE",
    }
    if not bool(getattr(cfg, "paper_deep_value_enabled", False)):
        diag["reason"] = "MODE_DISABLED"
        return None, diag
    if not policy.enabled:
        diag["reason"] = "PAPER_DISABLED"
        return None, diag
    if not getattr(ref, "condition_id", None):
        diag["reason"] = "CONDITION_ID_MISSING"
        return None, diag

    tte = snap.tte_sec if snap.tte_sec is not None else snap.seconds_remaining
    if tte is None or float(tte) < float(cfg.paper_deep_value_min_tte_sec):
        diag["reason"] = "TTE_TOO_LOW"
        return None, diag

    forecast, reason = _forecast_gate(trace, policy)
    if forecast is None:
        diag["reason"] = reason
        return None, diag

    probabilities = {
        "UP": float(forecast["p_up"]),
        "DOWN": float(forecast["p_down"]),
    }
    now_ms = int(time.time() * 1000)
    candidates: list[DeepValueCandidate] = []
    side_diags: dict[str, dict[str, Any]] = {}
    for side in ("UP", "DOWN"):
        candidate, side_diag = _candidate_for_side(
            side=side,
            selected_probability=probabilities[side],
            ref=ref,
            snap=snap,
            policy=policy,
            cfg=cfg,
            now_ms=now_ms,
            available_bankroll_usdc=available_bankroll_usdc,
        )
        side_diags[side] = side_diag
        if candidate is not None:
            candidates.append(candidate)

    diag["candidate_reasons"] = {
        side: side_diags[side].get("reason") for side in ("UP", "DOWN")
    }
    diag["candidate_values"] = {
        side: side_diags[side].get("value_multiple") for side in ("UP", "DOWN")
    }
    diag["candidate_asks"] = {
        side: side_diags[side].get("entry_ask", side_diags[side].get("snap_ask"))
        for side in ("UP", "DOWN")
    }

    if not candidates:
        diag["reason"] = _best_failure_reason(side_diags)
        return None, diag

    # Strongest model-value candidate wins.  Ties prefer larger absolute edge, then
    # cheaper fill, which is the more convex fixed-dollar payout.
    chosen = max(
        candidates,
        key=lambda item: (
            item.value_multiple,
            item.edge,
            -item.depth.fill_price,
        ),
    )
    chosen_diag = side_diags[chosen.side]
    diag.update(
        {
            "reason": "OPEN",
            "selected_side": chosen.side,
            "selected_probability": chosen.selected_probability,
            "depth_source": chosen_diag.get("depth_source"),
            "token_id": chosen.depth.token_id,
            "entry_ask": chosen.depth.ask,
            "fill_price": chosen.depth.fill_price,
            "depth_capacity_shares": chosen.depth.capacity_shares,
            "depth_required_shares": chosen.depth.required_shares,
            "depth_age_ms": chosen.depth.age_ms,
            "fee_source": chosen.depth.fee_source,
            "price_band": chosen.depth.price_band,
            "forecast_edge": chosen.edge,
            "value_multiple": chosen.value_multiple,
        }
    )

    return (
        PaperEntryDecision(
            eligible=True,
            reason="OPEN",
            side=chosen.side,
            selected_probability=chosen.selected_probability,
            entry_bid=(
                float(chosen.depth.bid) if chosen.depth.bid is not None else None
            ),
            entry_ask=float(chosen.depth.ask),
            fill_price=float(chosen.depth.fill_price),
            forecast_edge=float(chosen.edge),
            stake_usdc=float(policy.stake_usdc),
            shares=float(chosen.depth.required_shares),
            slippage=float(policy.slippage),
            fee_usdc=float(chosen.depth.fee_usdc),
        ),
        diag,
    )
