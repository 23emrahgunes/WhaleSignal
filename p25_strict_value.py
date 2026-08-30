"""STRICT V1 value evaluator for the independent PTB+Binance paper cohort.

Unlike the legacy dual-side deep-value experiment, this evaluator may only price the
side selected by the independent alpha. It also requires a configurable liquidity
buffer above the exact paper quantity. Polymarket pricing remains a value/fill input,
never an alpha input.
"""
from __future__ import annotations

import time
from typing import Any

from p25_deep_value import _candidate_for_side, _forecast_gate
from p25_paper import PaperEntryDecision, PaperPolicy


def evaluate_strict_value_watch(
    *,
    ref,
    snap,
    trace: dict[str, Any],
    policy: PaperPolicy,
    cfg,
    available_bankroll_usdc: float,
) -> tuple[PaperEntryDecision | None, dict[str, Any]]:  # noqa: ANN001
    diag: dict[str, Any] = {
        "entry_mode": "DEEP_VALUE_WATCH",
        "scan_mode": "DIRECTION_LOCKED_STRICT",
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

    side = str(trace.get("forecast_direction") or "").upper()
    if side not in {"UP", "DOWN"}:
        diag["reason"] = "STRICT_DIRECTION_MISSING"
        return None, diag
    selected_probability = (
        float(forecast["p_up"]) if side == "UP" else float(forecast["p_down"])
    )
    diag["alpha_side"] = side
    diag["selected_probability"] = selected_probability

    candidate, side_diag = _candidate_for_side(
        side=side,
        selected_probability=selected_probability,
        ref=ref,
        snap=snap,
        policy=policy,
        cfg=cfg,
        now_ms=int(time.time() * 1000),
        available_bankroll_usdc=available_bankroll_usdc,
    )
    diag.update(side_diag)
    diag["candidate_reasons"] = {side: side_diag.get("reason")}
    diag["candidate_values"] = {side: side_diag.get("value_multiple")}
    diag["candidate_asks"] = {
        side: side_diag.get("entry_ask", side_diag.get("snap_ask"))
    }
    if candidate is None:
        diag["reason"] = side_diag.get("reason") or "NO_DIRECTION_LOCKED_VALUE"
        return None, diag

    min_depth_multiple = float(
        getattr(cfg, "paper_deep_value_min_depth_multiple", 1.0)
    )
    required_buffered = candidate.depth.required_shares * min_depth_multiple
    diag["depth_min_multiple"] = min_depth_multiple
    diag["depth_required_buffered_shares"] = required_buffered
    if (
        bool(getattr(cfg, "paper_deep_value_require_depth", False))
        and candidate.depth.capacity_shares + 1e-9 < required_buffered
    ):
        diag["reason"] = (
            "DEPTH_BUFFER_INSUFFICIENT_"
            f"{candidate.depth.capacity_shares:.6f}_LT_{required_buffered:.6f}"
        )
        return None, diag

    diag.update(
        {
            "reason": "OPEN",
            "selected_side": candidate.side,
            "selected_probability": candidate.selected_probability,
            "token_id": candidate.depth.token_id,
            "entry_ask": candidate.depth.ask,
            "fill_price": candidate.depth.fill_price,
            "depth_capacity_shares": candidate.depth.capacity_shares,
            "depth_required_shares": candidate.depth.required_shares,
            "depth_age_ms": candidate.depth.age_ms,
            "fee_source": candidate.depth.fee_source,
            "price_band": candidate.depth.price_band,
            "forecast_edge": candidate.edge,
            "value_multiple": candidate.value_multiple,
        }
    )
    return (
        PaperEntryDecision(
            eligible=True,
            reason="OPEN",
            side=candidate.side,
            selected_probability=candidate.selected_probability,
            entry_bid=(
                float(candidate.depth.bid)
                if candidate.depth.bid is not None
                else None
            ),
            entry_ask=float(candidate.depth.ask),
            fill_price=float(candidate.depth.fill_price),
            forecast_edge=float(candidate.edge),
            stake_usdc=float(policy.stake_usdc),
            shares=float(candidate.depth.required_shares),
            slippage=float(policy.slippage),
            fee_usdc=float(candidate.depth.fee_usdc),
        ),
        diag,
    )
