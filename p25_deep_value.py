"""Tick-level low-price paper-entry evaluator for P2.5.

The strategy is deliberately paper-only. It watches the current research forecast,
then verifies the selected outcome against the public P2.6 full-depth CLOB snapshot.
A $1-style fixed stake is admitted only when enough visible depth exists at or better
than the conservative ask+slippage fill price.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from models import Decision
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


def _fee_schedule(conn: sqlite3.Connection, condition_id: str, token_id: str) -> FeeSchedule | None:
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


def _forecast_gate(trace: dict[str, Any], policy: PaperPolicy) -> tuple[dict[str, Any] | None, str]:
    direction = str(trace.get("forecast_direction") or "").upper()
    if direction not in {Decision.UP.value, Decision.DOWN.value}:
        return None, "NO_DIRECTIONAL_FORECAST"
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
    selected_probability = p_up if direction == Decision.UP.value else 1.0 - p_up
    return {
        "side": direction,
        "selected_probability": selected_probability,
        "confidence": confidence,
        "agreement": agreement,
    }, "OK"


def evaluate_deep_value_watch(
    *,
    ref,
    snap,
    trace: dict[str, Any],
    policy: PaperPolicy,
    cfg,
    available_bankroll_usdc: float,
) -> tuple[PaperEntryDecision | None, dict[str, Any]]:  # noqa: ANN001
    """Return an OPEN decision only when the deep-value touch is executable in depth.

    Non-qualifying ticks deliberately return ``None`` and are not persisted. This is
    essential: a 40c observation must not consume the market's one-shot attempt before
    the same contract later touches 10c or 5c.
    """
    diag: dict[str, Any] = {"entry_mode": "DEEP_VALUE_WATCH"}
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
    side = str(forecast["side"])
    selected_probability = float(forecast["selected_probability"])

    # Cheap prefilter: avoid opening the P2.6 DB for normal 30c/70c states. The
    # authoritative trigger still comes from the P2.6 full-depth snapshot below.
    snap_ask = snap.up_ask if side == "UP" else snap.down_ask
    if snap_ask is None:
        diag["reason"] = "SNAP_ASK_MISSING"
        return None, diag
    if float(snap_ask) > float(cfg.paper_deep_value_max_ask) + float(
        cfg.paper_deep_value_prefilter_buffer
    ):
        diag["reason"] = "WAITING_FOR_DIP"
        return None, diag

    depth: DeepValueDepth | None
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
            now_ms=int(time.time() * 1000),
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
            bid=(snap.up_bid if side == "UP" else snap.down_bid),
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
            "depth_source": "P26_FULL_DEPTH" if cfg.paper_deep_value_require_depth else "P25_BEST_ASK",
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

    edge = selected_probability - depth.fill_price
    value_multiple = selected_probability / max(depth.fill_price, 1e-12)
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

    diag["reason"] = "OPEN"
    return (
        PaperEntryDecision(
            eligible=True,
            reason="OPEN",
            side=side,
            selected_probability=selected_probability,
            entry_bid=float(depth.bid) if depth.bid is not None else None,
            entry_ask=float(depth.ask),
            fill_price=float(depth.fill_price),
            forecast_edge=float(edge),
            stake_usdc=stake,
            shares=float(depth.required_shares),
            slippage=float(policy.slippage),
            fee_usdc=float(depth.fee_usdc),
        ),
        diag,
    )
