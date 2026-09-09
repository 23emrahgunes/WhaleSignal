"""Stateful per-asset DUAL40 maker recovery for P3.

PAPER lanes are independent for BTC/ETH/SOL/XRP and simulate ordinary 40-cent
limits without the balanced-mid, confirmation or post-only-cross entry controls.
Price-history, opening-window and read-only forecast gates remain observable in
SHADOW. PAPER price-history regime checks do not block entry. LIVE keeps balanced,
confirmed, post-only semantics and defaults to one concurrent asset.
At PAPER market expiry, unmatched virtual exposure waits for the official outcome
and settles from the recorded virtual fill prices.
"""
from __future__ import annotations

import json
import logging
import math
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import replace
from typing import Any, Callable

from p25_discovery import authoritative_official_result
from p3_config import P3Settings
from p3_dual40_analytics import build_dual40_summary, p26_paper_decision_summary
from p3_dual40_core import (
    DUAL40_STRATEGY,
    Dual40Policy,
    MidPoint,
    OpeningGateProfile,
    evaluate_balanced_regime,
    evaluate_opening_stability,
    matched_pair_pnl,
    next_ladder_state,
    realized_cycle_pnl,
)
from p3_dual40_gateway import Dual40Gateway
from p3_dual40_store import (
    active_cycle,
    active_cycles,
    asset_from_combo_key,
    connect_dual40,
    create_cycle,
    cycle_for_condition,
    DUAL40_ASSETS,
    ladder_state,
    set_ladder_state,
    update_cycle,
    upsert_market_decision,
    write_scan_status,
)
from p3_dual40_forecast import ForecastGateDecision, provider_from_settings
from p3_live_preflight import run_live_preflight
from p3_live_state import LiveState
from p3_schema import open_p26_read_only


log = logging.getLogger("direction_engine.p3.dual40")


TERMINAL_STATUSES = {
    "PAPER_MATCHED",
    "PAPER_MATCHED_FILLED",
    "PAPER_SINGLE_LEG_LOSS",
    "MATCHED_FILLED",
    "LIVE_MATCHED_MERGED",
    "NO_FILL",
    "RESOLVED_UP",
    "RESOLVED_DOWN",
    "SUBMIT_FAILED",
    "CANCEL_FAILED_HALT",
    "MERGE_FAILED_HALT",
    "BALANCE_UNCERTAIN_HALT",
}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _levels(raw: object) -> list[tuple[float, float]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if not isinstance(raw, list):
        return []
    out: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            price = float(item[0])
            size = float(item[1])
        except (TypeError, ValueError):
            continue
        if 0 < price < 1 and size > 0:
            out.append((price, size))
    return out


def _book_view(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    bids = _levels(row["bids_json"])
    asks = _levels(row["asks_json"])
    if not bids or not asks:
        return None
    best_bid = max(price for price, _ in bids)
    best_ask = min(price for price, _ in asks)
    if best_bid >= best_ask:
        return None
    near_ask_limit = min(1.0, best_ask + 0.03)
    near_bid_limit = max(0.0, best_bid - 0.03)
    return {
        "id": int(row["id"]),
        "token_id": str(row["token_id"]),
        "side": str(row["side"]),
        "source_ts_ms": int(row["source_ts_ms"]),
        "recv_ts_ms": int(row["recv_ts_ms"]),
        "inserted_at_ms": int(row["inserted_at_ms"]),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": (best_bid + best_ask) / 2.0,
        "spread": best_ask - best_bid,
        "bid_at_40": sum(size for price, size in bids if abs(price - 0.40) <= 1e-9),
        "ask_at_40": sum(size for price, size in asks if abs(price - 0.40) <= 1e-9),
        "near_ask_depth": sum(size for price, size in asks if price <= near_ask_limit + 1e-12),
        "near_bid_depth": sum(size for price, size in bids if price + 1e-12 >= near_bid_limit),
    }


def _cycle_side_fill_price(cycle: dict[str, Any], side: str) -> float:
    key = str(side).strip().lower()
    stored = cycle.get(f"{key}_fill_price")
    if stored is not None:
        return float(stored)
    details = cycle.get("details") if isinstance(cycle.get("details"), dict) else {}
    evidence = details.get(f"paper_{key}_fill_evidence")
    if isinstance(evidence, dict) and evidence.get("best_ask") is not None:
        return float(evidence["best_ask"])
    return float(cycle["maker_price"])


def policy_from_settings(settings: P3Settings) -> Dual40Policy:
    return Dual40Policy(
        price=float(settings.dual40_price),
        ladder=tuple(settings.dual40_ladder()),
        min_market_age_sec=float(settings.dual40_market_age_sec),
        min_tte_sec=float(settings.dual40_min_tte_sec),
        lookback_sec=float(settings.dual40_lookback_sec),
        confirm_sec=float(settings.dual40_confirm_sec),
        balanced_mid_low=float(settings.dual40_balanced_mid_low),
        balanced_mid_high=float(settings.dual40_balanced_mid_high),
        max_mid_range=float(settings.dual40_max_mid_range),
        max_net_drift=float(settings.dual40_max_net_drift),
        max_abs_slope_per_sec=float(settings.dual40_max_abs_slope_per_sec),
        max_one_way_ratio=float(settings.dual40_max_one_way_ratio),
        max_single_jump=float(settings.dual40_max_single_jump),
        max_complement_residual=float(settings.dual40_max_complement_residual),
        max_spread_each=float(settings.dual40_max_spread_each),
        cancel_tte_sec=float(settings.dual40_cancel_tte_sec),
    )


class Dual40MakerEngine:
    def __init__(
        self,
        settings: P3Settings,
        state: LiveState,
        *,
        gateway_factory: Callable[[P3Settings], Any] = Dual40Gateway,
        preflight_fn: Callable[..., dict[str, Any]] = run_live_preflight,
        forecast_provider: Any | None = None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.policy = policy_from_settings(settings)
        self.policy.validate()
        self.gateway_factory = gateway_factory
        self.preflight_fn = preflight_fn
        self.forecast_provider = forecast_provider or provider_from_settings(settings)
        self._gateway: Any | None = None
        self._gate_since: dict[str, float] = {}
        self._last_scan_write_ms = 0
        self._last_balance_poll_ms: dict[int, int] = {}
        self._last_resolution_poll_ms: dict[int, int] = {}
        self._last_status: dict[str, Any] = {"status": "STARTING"}

        conn = connect_dual40(settings.p3_db_path)
        conn.close()

    def _gateway_client(self) -> Any:
        if self._gateway is None:
            self._gateway = self.gateway_factory(self.settings)
        return self._gateway

    @staticmethod
    def _transport_status(p26) -> dict[str, Any]:  # noqa: ANN001
        row = p26.execute(
            "SELECT value FROM p26_meta WHERE key='book_transport_status_json'"
        ).fetchone()
        if row is None:
            return {}
        try:
            payload = json.loads(str(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _active_markets(self, p26, now_ms: int) -> list[dict[str, Any]]:  # noqa: ANN001
        assets = set(self.settings.dual40_assets())
        rows = p26.execute(
            """
            SELECT condition_id,combo_key,MAX(market_end_ts_ms) AS market_end_ts_ms,
                   MAX(CASE WHEN side='UP' THEN token_id END) AS up_token_id,
                   MAX(CASE WHEN side='DOWN' THEN token_id END) AS down_token_id,
                   COUNT(DISTINCT side) AS sides
            FROM p26_market_tokens
            WHERE active=1 AND market_end_ts_ms>? AND combo_key LIKE '%:5m'
            GROUP BY condition_id,combo_key
            HAVING sides=2
            ORDER BY market_end_ts_ms,combo_key
            """,
            (int(now_ms),),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            combo = str(row["combo_key"])
            asset = combo.partition(":")[0].upper()
            if asset not in assets:
                continue
            if not row["up_token_id"] or not row["down_token_id"]:
                continue
            item = dict(row)
            item["asset"] = asset
            out.append(item)
        return out

    @staticmethod
    def _latest_book(p26, condition_id: str, side: str) -> dict[str, Any] | None:  # noqa: ANN001
        row = p26.execute(
            """
            SELECT id,condition_id,token_id,side,source_ts_ms,recv_ts_ms,
                   inserted_at_ms,bids_json,asks_json
            FROM p26_clob_books
            WHERE condition_id=? AND side=?
            ORDER BY recv_ts_ms DESC,id DESC LIMIT 1
            """,
            (str(condition_id), str(side)),
        ).fetchone()
        return _book_view(row)

    def _mid_history(
        self,
        p26,
        *,
        condition_id: str,
        side: str,
        now_ms: int,
    ) -> list[MidPoint]:  # noqa: ANN001
        lookback_ms = int(float(self.policy.lookback_sec) * 1000.0)
        cutoff = int(now_ms) - lookback_ms
        rows = p26.execute(
            """
            SELECT id,condition_id,token_id,side,source_ts_ms,recv_ts_ms,
                   inserted_at_ms,bids_json,asks_json
            FROM p26_clob_books
            WHERE condition_id=? AND side=?
            ORDER BY id DESC LIMIT 256
            """,
            (str(condition_id), str(side)),
        ).fetchall()
        views = [view for view in (_book_view(row) for row in reversed(rows)) if view]
        if not views:
            return []

        points: list[MidPoint] = []
        for view in views:
            effective_ts = max(int(view["source_ts_ms"]), int(view["inserted_at_ms"]))
            if effective_ts >= cutoff:
                points.append(MidPoint(effective_ts, float(view["mid"])))

        latest = views[-1]
        # If the latest unchanged book state was first observed before the cutoff and
        # was still received recently, synthesize the beginning and end of the flat
        # interval. Stable markets must not fail merely because no level changed.
        if int(latest["inserted_at_ms"]) <= cutoff <= int(latest["recv_ts_ms"]):
            points.insert(0, MidPoint(cutoff, float(latest["mid"])))
        if int(latest["recv_ts_ms"]) >= cutoff:
            points.append(MidPoint(min(int(now_ms), int(latest["recv_ts_ms"])), float(latest["mid"])))

        dedup: dict[int, float] = {}
        for point in points:
            dedup[int(point.ts_ms)] = float(point.mid)
        return [MidPoint(ts, dedup[ts]) for ts in sorted(dedup)]

    def _opening_profile(self, level_index: int) -> OpeningGateProfile:
        values = self.settings.dual40_gate_profile(level_index)
        return OpeningGateProfile(
            **values,
            max_abs_slope_per_sec=float(self.settings.dual40_max_abs_slope_per_sec),
            max_single_jump=float(self.settings.dual40_max_single_jump),
            max_complement_residual=float(self.settings.dual40_max_complement_residual),
        )

    def _opening_history(
        self,
        p26,
        *,
        condition_id: str,
        side: str,
        market_start_ts_ms: int,
        observation_sec: float,
        now_ms: int,
    ) -> list[MidPoint]:  # noqa: ANN001
        window_start = int(market_start_ts_ms)
        window_end = min(
            int(now_ms),
            window_start + int(float(observation_sec) * 1000.0),
        )
        rows = p26.execute(
            """
            SELECT id,condition_id,token_id,side,source_ts_ms,recv_ts_ms,
                   inserted_at_ms,bids_json,asks_json
            FROM p26_clob_books
            WHERE condition_id=? AND side=?
            ORDER BY id DESC LIMIT 4096
            """,
            (str(condition_id), str(side)),
        ).fetchall()
        views = [view for view in (_book_view(row) for row in reversed(rows)) if view]
        if not views or window_end <= window_start:
            return []

        timed = [
            (max(int(view["source_ts_ms"]), int(view["inserted_at_ms"])), view)
            for view in views
        ]
        before_start = [item for item in timed if item[0] <= window_start]
        within = [item for item in timed if window_start < item[0] < window_end]
        after_end = any(item[0] >= window_end for item in timed)
        points: list[MidPoint] = []
        if before_start:
            points.append(MidPoint(window_start, float(before_start[-1][1]["mid"])))
        points.extend(MidPoint(ts, float(view["mid"])) for ts, view in within)
        before_end = [item for item in timed if item[0] <= window_end]
        if before_end and (after_end or int(now_ms) <= window_end + 1000):
            points.append(MidPoint(window_end, float(before_end[-1][1]["mid"])))
        dedup = {int(point.ts_ms): float(point.mid) for point in points}
        return [MidPoint(ts, dedup[ts]) for ts in sorted(dedup)]

    def _forecast_decision(
        self,
        *,
        now_ms: int,
        combo_key: str,
        condition_id: str,
        tte_sec: float,
        profile: OpeningGateProfile,
    ) -> ForecastGateDecision:
        if self.settings.dual40_forecast_mode() == "OFF":
            return ForecastGateDecision(True, "FORECAST_GATE_OFF", False)
        try:
            decision = self.forecast_provider.evaluate(
                now_ms=int(now_ms),
                combo_key=str(combo_key),
                condition_id=str(condition_id),
                tte_sec=float(tte_sec),
                minimum_p_up=float(profile.forecast_min_p_up),
                maximum_p_up=float(profile.forecast_max_p_up),
            )
            if (
                self.settings.dual40_forecast_mode() == "ENFORCE"
                and decision.source_kind == "P25_RESEARCH_FORECAST"
            ):
                return replace(
                    decision,
                    eligible=False,
                    reason="FORECAST_RESEARCH_ONLY",
                )
            return decision
        except Exception as exc:  # noqa: BLE001
            log.exception("DUAL40 forecast adapter failed combo=%s", combo_key)
            return ForecastGateDecision(
                False,
                f"FORECAST_PROVIDER_ERROR:{type(exc).__name__}",
                False,
            )

    def _global_risk_decision(
        self,
        conn,
        *,
        scope: str,
        level_index: int,
        now_ms: int,
    ) -> dict[str, Any]:  # noqa: ANN001
        cutoff = int(now_ms) - 86_400_000
        row = conn.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN realized_pnl_usdc<0
                                    THEN -realized_pnl_usdc ELSE 0 END),0) AS gross_loss
            FROM p3_dual40_cycles
            WHERE scope=? AND resolved_at_ms>=?
            """,
            (scope.upper(), cutoff),
        ).fetchone()
        gross_loss = float(row["gross_loss"] if row is not None else 0.0)
        active_recovery_exposure = sum(
            2.0 * self.policy.price * float(cycle["target_shares"])
            for cycle in active_cycles(conn, scope=scope)
            if int(cycle.get("level_index") or 0) > 0
        )
        candidate_exposure = (
            2.0 * self.policy.price * float(self.policy.ladder[level_index])
            if int(level_index) > 0
            else 0.0
        )
        projected = active_recovery_exposure + candidate_exposure
        daily_ok = gross_loss + 1e-12 < float(self.settings.dual40_daily_loss_limit_usdc)
        exposure_ok = projected <= float(self.settings.dual40_max_recovery_exposure_usdc) + 1e-12
        reason = "GLOBAL_RISK_OK"
        if not daily_ok:
            reason = "GLOBAL_DAILY_LOSS_LIMIT"
        elif not exposure_ok:
            reason = "GLOBAL_RECOVERY_EXPOSURE_LIMIT"
        return {
            "eligible": bool(daily_ok and exposure_ok),
            "reason": reason,
            "mode": self.settings.dual40_risk_mode(),
            "rolling_24h_gross_loss_usdc": round(gross_loss, 6),
            "active_recovery_exposure_usdc": round(active_recovery_exposure, 6),
            "candidate_recovery_exposure_usdc": round(candidate_exposure, 6),
            "projected_recovery_exposure_usdc": round(projected, 6),
            "daily_loss_limit_usdc": float(self.settings.dual40_daily_loss_limit_usdc),
            "max_recovery_exposure_usdc": float(self.settings.dual40_max_recovery_exposure_usdc),
        }

    @staticmethod
    def _maker_fee_ready(p26, condition_id: str, tokens: tuple[str, str]) -> tuple[bool, str]:  # noqa: ANN001
        rows = p26.execute(
            """
            SELECT token_id,taker_only,source_ts_ms
            FROM p26_fee_schedules
            WHERE condition_id=? AND token_id IN (?,?)
            """,
            (str(condition_id), str(tokens[0]), str(tokens[1])),
        ).fetchall()
        if len(rows) != 2:
            return False, "FEE_SCHEDULE_MISSING"
        if not all(bool(row["taker_only"]) for row in rows):
            return False, "MAKER_ZERO_FEE_NOT_CONFIRMED"
        return True, "MAKER_ZERO_FEE_CONFIRMED"

    def _compose_candidate_gates(
        self,
        *,
        base: dict[str, Any],
        base_gate: dict[str, Any],
        opening_gate: dict[str, Any],
        forecast_gate: dict[str, Any],
        risk_gate: dict[str, Any],
        profile: OpeningGateProfile,
    ) -> dict[str, Any]:
        eligible = bool(base_gate.get("eligible"))
        reason = str(base_gate.get("reason") or "UNKNOWN")
        opening_mode = self.settings.dual40_opening_mode()
        forecast_mode = self.settings.dual40_forecast_mode()
        risk_mode = self.settings.dual40_risk_mode()
        if eligible and opening_mode == "ENFORCE" and not opening_gate.get("eligible"):
            eligible = False
            reason = str(opening_gate.get("reason") or "OPENING_GATE_REJECTED")
        if eligible and forecast_mode == "ENFORCE" and not forecast_gate.get("eligible"):
            eligible = False
            reason = str(forecast_gate.get("reason") or "FORECAST_GATE_REJECTED")
        if eligible and risk_mode == "ENFORCE" and not risk_gate.get("eligible"):
            eligible = False
            reason = str(risk_gate.get("reason") or "GLOBAL_RISK_REJECTED")

        base_allowed = bool(base_gate.get("eligible"))
        opening_allowed = bool(opening_gate.get("eligible"))
        forecast_allowed = bool(forecast_gate.get("eligible"))
        risk_allowed = bool(risk_gate.get("eligible"))
        base_reason = str(base_gate.get("reason") or "UNKNOWN")
        opening_reason = str(opening_gate.get("reason") or "NOT_EVALUATED")
        history_wait_reasons = {
            "MARKET_WARMUP",
            "REGIME_HISTORY_INSUFFICIENT",
            "WAITING_OPENING_WINDOW",
            "OPENING_HISTORY_INSUFFICIENT",
        }
        if base_reason in history_wait_reasons:
            history_ready = False
            history_reason = base_reason
        elif opening_reason in history_wait_reasons:
            history_ready = False
            history_reason = opening_reason
        elif opening_reason == "NOT_EVALUATED":
            history_ready = False
            history_reason = "NOT_EVALUATED"
        else:
            history_ready = True
            history_reason = "HISTORY_READY"
        stages = [
            {"stage": "SEEN", "eligible": True, "reason": "MARKET_DISCOVERED"},
            {
                "stage": "WAITING_HISTORY",
                "eligible": history_ready,
                "reason": history_reason,
            },
            {
                "stage": "OPENING_GATE",
                "eligible": opening_allowed,
                "reason": opening_reason,
                "mode": opening_mode,
            },
            {
                "stage": "BOOK_FEE_GATE",
                "eligible": base_allowed,
                "reason": base_reason,
            },
            {
                "stage": "FORECAST_GATE",
                "eligible": forecast_allowed,
                "reason": str(forecast_gate.get("reason") or "NOT_EVALUATED"),
                "mode": forecast_mode,
            },
            {
                "stage": "RISK_GATE",
                "eligible": risk_allowed,
                "reason": str(risk_gate.get("reason") or "NOT_EVALUATED"),
                "mode": risk_mode,
            },
        ]
        return {
            **base,
            **base_gate,
            "eligible": eligible,
            "reason": reason,
            "base_gate": base_gate,
            "opening_gate": opening_gate,
            "forecast_gate": forecast_gate,
            "risk_gate": risk_gate,
            "gate_profile": {
                "observation_sec": profile.observation_sec,
                "max_mid_range": profile.max_mid_range,
                "max_net_drift": profile.max_net_drift,
                "max_one_way_ratio": profile.max_one_way_ratio,
                "max_spread_each": profile.max_spread_each,
                "max_queue_imbalance": profile.max_queue_imbalance,
                "min_depth_balance_ratio": profile.min_depth_balance_ratio,
                "forecast_min_p_up": profile.forecast_min_p_up,
                "forecast_max_p_up": profile.forecast_max_p_up,
            },
            "gate_modes": {
                "opening": opening_mode,
                "forecast": forecast_mode,
                "global_risk": risk_mode,
            },
            "stages": stages,
            "would_open_base": base_allowed,
            "would_open_opening": base_allowed and opening_allowed,
            "would_open_opening_forecast": base_allowed and opening_allowed and forecast_allowed,
            "would_open_strict_recovery": (
                base_allowed and opening_allowed and forecast_allowed and risk_allowed
            ),
        }

    def _candidate(
        self,
        p26,
        conn,
        market: dict[str, Any],
        now_ms: int,
        *,
        scope: str,
        state_row: dict[str, Any],
    ) -> dict[str, Any]:  # noqa: ANN001
        condition = str(market["condition_id"])
        combo = str(market["combo_key"])
        end_ms = int(market["market_end_ts_ms"])
        start_ms = end_ms - 300_000
        market_age = max(0.0, (int(now_ms) - start_ms) / 1000.0)
        tte = max(0.0, (end_ms - int(now_ms)) / 1000.0)
        level_index = int(state_row["level_index"])
        profile = self._opening_profile(level_index)
        up = self._latest_book(p26, condition, "UP")
        down = self._latest_book(p26, condition, "DOWN")

        base = {
            "asset": asset_from_combo_key(combo),
            "condition_id": condition,
            "combo_key": combo,
            "market_end_ts_ms": end_ms,
            "market_age_sec": round(market_age, 3),
            "tte_sec": round(tte, 3),
            "up_token_id": str(market["up_token_id"]),
            "down_token_id": str(market["down_token_id"]),
            "level_index": level_index,
            "target_shares": float(self.policy.ladder[level_index]),
            "recovery_pending": level_index > 0 or float(state_row["loss_pool_usdc"]) > 1e-9,
            "recovery_debt_usdc": float(state_row["loss_pool_usdc"]),
            "entry_profile": (
                "PAPER_RELAXED_LIMIT"
                if str(scope).upper() == "PAPER"
                else "LIVE_BALANCED_POST_ONLY"
            ),
            "balanced_mid_gate_enabled": str(scope).upper() != "PAPER",
            "price_history_regime_gate_enabled": str(scope).upper() != "PAPER",
            "post_only_cross_gate_enabled": str(scope).upper() != "PAPER",
            "confirmation_required_sec": (
                0.0
                if str(scope).upper() == "PAPER"
                else float(self.policy.confirm_sec)
            ),
        }
        forecast = self._forecast_decision(
            now_ms=int(now_ms),
            combo_key=combo,
            condition_id=condition,
            tte_sec=tte,
            profile=profile,
        ).to_dict()
        risk = self._global_risk_decision(
            conn,
            scope=scope,
            level_index=level_index,
            now_ms=int(now_ms),
        )
        not_evaluated = {"eligible": False, "reason": "NOT_EVALUATED"}
        if up is None or down is None:
            return self._compose_candidate_gates(
                base=base,
                base_gate={"eligible": False, "reason": "BOOK_PAIR_MISSING", "score": 0.0},
                opening_gate=not_evaluated,
                forecast_gate=forecast,
                risk_gate=risk,
                profile=profile,
            )

        up_age = max(0, int(now_ms) - int(up["recv_ts_ms"]))
        down_age = max(0, int(now_ms) - int(down["recv_ts_ms"]))
        base.update(
            {
                "up_book": up,
                "down_book": down,
                "max_book_age_ms": max(up_age, down_age),
                "queue_ahead_up_at_40": float(up["bid_at_40"]),
                "queue_ahead_down_at_40": float(down["bid_at_40"]),
                "near_depth_up": float(up["near_ask_depth"]),
                "near_depth_down": float(down["near_ask_depth"]),
            }
        )
        if max(up_age, down_age) > int(self.settings.dual40_book_fresh_ms):
            return self._compose_candidate_gates(
                base=base,
                base_gate={"eligible": False, "reason": "BOOK_STALE", "score": 0.0},
                opening_gate=not_evaluated,
                forecast_gate=forecast,
                risk_gate=risk,
                profile=profile,
            )

        fee_ok, fee_reason = self._maker_fee_ready(
            p26,
            condition,
            (str(market["up_token_id"]), str(market["down_token_id"])),
        )
        base["fee_gate"] = fee_reason
        if not fee_ok:
            return self._compose_candidate_gates(
                base=base,
                base_gate={"eligible": False, "reason": fee_reason, "score": 0.0},
                opening_gate=not_evaluated,
                forecast_gate=forecast,
                risk_gate=risk,
                profile=profile,
            )

        history = self._mid_history(
            p26,
            condition_id=condition,
            side="UP",
            now_ms=int(now_ms),
        )
        gate = evaluate_balanced_regime(
            policy=self.policy,
            up_points=history,
            current_down_mid=float(down["mid"]),
            current_up_spread=float(up["spread"]),
            current_down_spread=float(down["spread"]),
            current_up_ask=float(up["best_ask"]),
            current_down_ask=float(down["best_ask"]),
            market_age_sec=market_age,
            tte_sec=tte,
            require_balanced_mid=str(scope).upper() != "PAPER",
            require_post_only_safe=str(scope).upper() != "PAPER",
        )
        gate_payload = gate.to_dict()
        if str(scope).upper() == "PAPER":
            base["research_regime_gate"] = gate_payload
            if market_age + 1e-9 < self.policy.min_market_age_sec:
                paper_ready = False
                paper_reason = "MARKET_WARMUP"
            elif tte + 1e-9 < self.policy.min_tte_sec:
                paper_ready = False
                paper_reason = "TTE_TOO_LOW"
            elif min(float(up["best_ask"]), float(down["best_ask"])) + 1e-12 < float(
                self.settings.dual40_paper_min_entry_ask
            ):
                paper_ready = False
                paper_reason = "PAPER_ENTRY_ASK_TOO_LOW"
            else:
                paper_ready = True
                paper_reason = "PAPER_RELAXED_LIMIT_READY"
            gate_payload = {
                **gate_payload,
                "eligible": paper_ready,
                "reason": paper_reason,
                "up_mid": gate_payload.get("up_mid") or float(up["mid"]),
                "down_mid": gate_payload.get("down_mid") or float(down["mid"]),
                "paper_min_entry_ask": float(self.settings.dual40_paper_min_entry_ask),
                "research_eligible": bool(gate.eligible),
                "research_reason": str(gate.reason),
            }
        opening_up = self._opening_history(
            p26,
            condition_id=condition,
            side="UP",
            market_start_ts_ms=start_ms,
            observation_sec=profile.observation_sec,
            now_ms=int(now_ms),
        )
        opening_down = self._opening_history(
            p26,
            condition_id=condition,
            side="DOWN",
            market_start_ts_ms=start_ms,
            observation_sec=profile.observation_sec,
            now_ms=int(now_ms),
        )
        opening = evaluate_opening_stability(
            profile=profile,
            up_points=opening_up,
            down_points=opening_down,
            market_age_sec=market_age,
            current_up_spread=float(up["spread"]),
            current_down_spread=float(down["spread"]),
            queue_up_at_40=float(up["bid_at_40"]),
            queue_down_at_40=float(down["bid_at_40"]),
            near_depth_up=float(up["near_ask_depth"]),
            near_depth_down=float(down["near_ask_depth"]),
        ).to_dict()
        base["history_points"] = len(history)
        base["opening_history_points_up"] = len(opening_up)
        base["opening_history_points_down"] = len(opening_down)
        return self._compose_candidate_gates(
            base=base,
            base_gate=gate_payload,
            opening_gate=opening,
            forecast_gate=forecast,
            risk_gate=risk,
            profile=profile,
        )

    def _decision_for_candidate(self, candidate: dict[str, Any], *, active: bool, hard_stop: bool) -> str:
        if hard_stop:
            return "SKIPPED_HARD_STOP"
        if active:
            return "SKIPPED_ASSET_ACTIVE"
        if candidate.get("eligible"):
            stable = float(candidate.get("stable_for_sec") or 0.0)
            required = float(
                candidate.get("confirmation_required_sec", self.policy.confirm_sec)
            )
            return "WOULD_OPEN" if stable + 1e-9 >= required else "WAITING_CONFIRMATION"
        reason = str(candidate.get("reason") or "UNKNOWN")
        if reason in {
            "REGIME_HISTORY_INSUFFICIENT",
            "MARKET_WARMUP",
            "PAPER_ENTRY_ASK_TOO_LOW",
            "WAITING_OPENING_WINDOW",
            "OPENING_HISTORY_INSUFFICIENT",
        }:
            return "WAITING_HISTORY"
        if reason == "BOOK_PAIR_MISSING":
            return "REJECTED_BOOK_MISSING"
        if reason == "BOOK_STALE":
            return "REJECTED_BOOK_STALE"
        if reason == "POST_ONLY_WOULD_CROSS":
            return "REJECTED_POST_ONLY_CROSS"
        if reason.startswith("FEE_") or reason == "MAKER_ZERO_FEE_NOT_CONFIRMED":
            return "REJECTED_FEE_LINEAGE"
        if reason in {"ONE_WAY_SEQUENCE", "ONE_WAY_SLOPE"}:
            return "REJECTED_ONE_WAY"
        if reason.startswith("REJECTED_"):
            return reason
        return f"REJECTED_{reason}"

    def _scan(
        self,
        p26,
        conn,
        *,
        scope: str,
        now_ms: int,
        active_by_asset: dict[str, dict[str, Any]],
        state_by_asset: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:  # noqa: ANN001
        transport = self._transport_status(p26)
        transport_connected = bool(transport.get("connected"))
        transport_recv = int(transport.get("last_receive_ms") or 0)
        transport_age = max(0, int(now_ms) - transport_recv) if transport_recv else None
        transport_ok = bool(
            transport_connected
            and transport_age is not None
            and transport_age <= max(5000, int(self.settings.dual40_book_fresh_ms) * 3)
        )

        markets = self._active_markets(p26, int(now_ms))
        candidates: list[dict[str, Any]] = []
        reasons = Counter()
        active_gate_keys = {
            f"{scope}:{market['asset']}:{market['condition_id']}"
            for market in markets
        }
        for key in list(self._gate_since):
            if key.split(":", 1)[0] == scope and key not in active_gate_keys:
                self._gate_since.pop(key, None)

        if transport_ok:
            for market in markets:
                asset = str(market["asset"])
                item = self._candidate(
                    p26,
                    conn,
                    market,
                    int(now_ms),
                    scope=scope,
                    state_row=state_by_asset[asset],
                )
                candidates.append(item)
                reasons[str(item.get("reason") or "UNKNOWN")] += 1
                asset = str(item["asset"])
                condition = str(item["condition_id"])
                gate_key = f"{scope}:{asset}:{condition}"
                if item.get("eligible"):
                    self._gate_since.setdefault(gate_key, time.monotonic())
                    item["stable_for_sec"] = round(
                        max(0.0, time.monotonic() - self._gate_since[gate_key]),
                        3,
                    )
                else:
                    self._gate_since.pop(gate_key, None)
                    item["stable_for_sec"] = 0.0
                active = asset in active_by_asset
                hard_stop = bool((state_by_asset.get(asset) or {}).get("hard_stopped"))
                decision = self._decision_for_candidate(item, active=active, hard_stop=hard_stop)
                item["lane_status"] = "ACTIVE" if active else ("HARD_STOP" if hard_stop else "AVAILABLE")
                item["decision"] = decision
                item["active_cycle_id"] = (active_by_asset.get(asset) or {}).get("id")
                final_stage = (
                    "WOULD_OPEN"
                    if decision == "WOULD_OPEN"
                    else (
                        "REJECTED"
                        if decision.startswith(("REJECTED", "SKIPPED"))
                        else decision
                    )
                )
                item["stages"] = [
                    *item.get("stages", []),
                    {
                        "stage": final_stage,
                        "eligible": decision == "WOULD_OPEN",
                        "reason": str(item.get("reason") or decision),
                        "decision": decision,
                    },
                ]
                upsert_market_decision(
                    conn,
                    scope=scope,
                    asset=asset,
                    combo_key=str(item["combo_key"]),
                    condition_id=str(item["condition_id"]),
                    market_start_ts_ms=int(item["market_end_ts_ms"]) - 300_000,
                    market_end_ts_ms=int(item["market_end_ts_ms"]),
                    decision=decision,
                    reason=str(item.get("reason") or ""),
                    score=float(item.get("score") or 0.0),
                    eligible=bool(item.get("eligible")),
                    final_gate=item,
                    now_ms=int(now_ms),
                )
        else:
            reasons["BOOK_TRANSPORT_NOT_LIVE"] += max(1, len(markets))
            for market in markets:
                asset = str(market["asset"])
                active = asset in active_by_asset
                hard_stop = bool((state_by_asset.get(asset) or {}).get("hard_stopped"))
                item = {
                    "asset": asset,
                    "condition_id": str(market["condition_id"]),
                    "combo_key": str(market["combo_key"]),
                    "market_end_ts_ms": int(market["market_end_ts_ms"]),
                    "eligible": False,
                    "reason": "BOOK_TRANSPORT_NOT_LIVE",
                    "score": 0.0,
                    "stable_for_sec": 0.0,
                    "lane_status": "ACTIVE" if active else ("HARD_STOP" if hard_stop else "AVAILABLE"),
                    "active_cycle_id": (active_by_asset.get(asset) or {}).get("id"),
                    "decision": "REJECTED_BOOK_TRANSPORT_NOT_LIVE",
                    "stages": [
                        {"stage": "SEEN", "eligible": True, "reason": "MARKET_DISCOVERED"},
                        {
                            "stage": "BOOK_FEE_GATE",
                            "eligible": False,
                            "reason": "BOOK_TRANSPORT_NOT_LIVE",
                        },
                        {
                            "stage": "REJECTED",
                            "eligible": False,
                            "reason": "BOOK_TRANSPORT_NOT_LIVE",
                            "decision": "REJECTED_BOOK_TRANSPORT_NOT_LIVE",
                        },
                    ],
                }
                candidates.append(item)
                upsert_market_decision(
                    conn,
                    scope=scope,
                    asset=asset,
                    combo_key=str(item["combo_key"]),
                    condition_id=str(item["condition_id"]),
                    market_start_ts_ms=int(item["market_end_ts_ms"]) - 300_000,
                    market_end_ts_ms=int(item["market_end_ts_ms"]),
                    decision=str(item["decision"]),
                    reason=str(item["reason"]),
                    score=0.0,
                    eligible=False,
                    final_gate=item,
                    now_ms=int(now_ms),
                )

        scan = {
            "strategy": DUAL40_STRATEGY,
            "scope": scope,
            "now_ms": int(now_ms),
            "transport": {
                "connected": transport_connected,
                "age_ms": transport_age,
                "ok": transport_ok,
            },
            "active_markets": len(markets),
            "eligible_markets": sum(1 for item in candidates if item.get("eligible")),
            "would_open_base": sum(1 for item in candidates if item.get("would_open_base")),
            "would_open_opening": sum(1 for item in candidates if item.get("would_open_opening")),
            "would_open_opening_forecast": sum(
                1 for item in candidates if item.get("would_open_opening_forecast")
            ),
            "would_open_strict_recovery": sum(
                1 for item in candidates if item.get("would_open_strict_recovery")
            ),
            "reason_counts": dict(reasons),
            "candidates": candidates,
            "gate_modes": {
                "opening": self.settings.dual40_opening_mode(),
                "forecast": self.settings.dual40_forecast_mode(),
                "global_risk": self.settings.dual40_risk_mode(),
            },
            "one_global_market_only": False,
            "paper_max_concurrent_assets": int(self.settings.dual40_paper_max_concurrent_assets),
            "live_max_concurrent_assets": int(self.settings.dual40_live_max_concurrent_assets),
        }
        if int(now_ms) - self._last_scan_write_ms >= 1000:
            write_scan_status(conn, scan)
            self._last_scan_write_ms = int(now_ms)

        ready = [
            item
            for item in candidates
            if item.get("eligible")
            and float(item.get("stable_for_sec") or 0.0) + 1e-9
            >= float(item.get("confirmation_required_sec", self.policy.confirm_sec))
            and cycle_for_condition(
                conn,
                scope=scope,
                asset=str(item["asset"]),
                condition_id=str(item["condition_id"]),
            )
            is None
            and str(item["asset"]) not in active_by_asset
            and not bool((state_by_asset.get(str(item["asset"])) or {}).get("hard_stopped"))
        ]
        ready.sort(
            key=lambda item: (
                str(item.get("asset") or ""),
                -float(item.get("score") or 0.0),
                -float(item.get("tte_sec") or 0.0),
            )
        )
        return ready

    def _open_paper(self, conn, candidate: dict[str, Any], state_row: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN001
        asset = str(candidate.get("asset") or asset_from_combo_key(str(candidate["combo_key"])))
        level = int(state_row["level_index"])
        quantity = float(self.policy.ladder[level])
        opened_at_ms = int(time.time() * 1000)
        immediate_fills: dict[str, dict[str, Any]] = {}
        for side in ("UP", "DOWN"):
            book = candidate.get(f"{side.lower()}_book")
            if not isinstance(book, dict) or book.get("best_ask") is None:
                continue
            best_ask = float(book["best_ask"])
            if best_ask <= self.policy.price + 1e-12:
                immediate_fills[side] = {
                    "book_id": int(book["id"]),
                    "best_ask": best_ask,
                    "observed_at_ms": int(book.get("recv_ts_ms") or opened_at_ms),
                    "touch_ts_ms": opened_at_ms,
                    "source_ts_ms": int(book.get("source_ts_ms") or 0),
                    "recv_ts_ms": int(book.get("recv_ts_ms") or 0),
                    "fill_kind": "ENTRY_MARKETABLE_LIMIT",
                }
        opened_gate = {
            **candidate,
            "stages": [
                *candidate.get("stages", []),
                {
                    "stage": "OPENED",
                    "eligible": True,
                    "reason": "PAPER_CYCLE_CREATED",
                },
            ],
        }
        cycle_id = create_cycle(
            conn,
            scope="PAPER",
            asset=asset,
            session_id=None,
            condition_id=str(candidate["condition_id"]),
            combo_key=str(candidate["combo_key"]),
            market_end_ts_ms=int(candidate["market_end_ts_ms"]),
            level_index=level,
            target_shares=quantity,
            maker_price=self.policy.price,
            status="PAPER_RESTING",
            gate=opened_gate,
            up_token_id=str(candidate["up_token_id"]),
            down_token_id=str(candidate["down_token_id"]),
            loss_pool_before_usdc=float(state_row["loss_pool_usdc"]),
            details={
                "paper_fill_rule": "ENTRY_OR_RECORDED_BEST_ASK_LE_MAKER_FULL_SIDE",
                "near_touch_41_is_diagnostic_only": True,
                "post_only": False,
                "order_type": "GTC_SIMULATED_LIMIT",
                "entry_cross_policy": "MARKETABLE_LIMIT_FULL_SIDE",
            },
        )
        up_filled = quantity if "UP" in immediate_fills else 0.0
        down_filled = quantity if "DOWN" in immediate_fills else 0.0
        matched = min(up_filled, down_filled)
        residual = abs(up_filled - down_filled)
        residual_side = (
            "UP"
            if up_filled > down_filled
            else ("DOWN" if down_filled > up_filled else None)
        )
        fill_details: dict[str, Any] = {}
        for side in ("UP", "DOWN"):
            book = candidate.get(f"{side.lower()}_book")
            key = side.lower()
            if isinstance(book, dict) and book.get("id") is not None:
                fill_details[f"paper_last_scanned_{key}_book_id"] = int(book["id"])
            if side in immediate_fills:
                fill_details[f"paper_{key}_fill_evidence"] = immediate_fills[side]
                fill_details[f"paper_lowest_{key}_best_ask"] = float(
                    immediate_fills[side]["best_ask"]
                )
        update_cycle(
            conn,
            cycle_id,
            orders_posted_at_ms=opened_at_ms,
            up_filled_shares=up_filled,
            down_filled_shares=down_filled,
            up_fill_price=(
                float(immediate_fills["UP"]["best_ask"])
                if "UP" in immediate_fills
                else None
            ),
            down_fill_price=(
                float(immediate_fills["DOWN"]["best_ask"])
                if "DOWN" in immediate_fills
                else None
            ),
            matched_shares=matched,
            residual_side=residual_side,
            residual_shares=residual,
            near_touch_up_41=int(
                isinstance(candidate.get("up_book"), dict)
                and float(candidate["up_book"]["best_ask"])
                <= float(self.settings.dual40_near_touch_price) + 1e-12
            ),
            near_touch_down_41=int(
                isinstance(candidate.get("down_book"), dict)
                and float(candidate["down_book"]["best_ask"])
                <= float(self.settings.dual40_near_touch_price) + 1e-12
            ),
            details_merge=fill_details,
        )
        log.info(
            "DUAL40 PAPER OPEN id=%s asset=%s combo=%s q=%.3f price=%.2f score=%.3f immediate=%s",
            cycle_id,
            asset,
            candidate["combo_key"],
            quantity,
            self.policy.price,
            float(candidate.get("score") or 0.0),
            ",".join(sorted(immediate_fills)) or "none",
        )
        upsert_market_decision(
            conn,
            scope="PAPER",
            asset=asset,
            combo_key=str(candidate["combo_key"]),
            condition_id=str(candidate["condition_id"]),
            market_start_ts_ms=int(candidate["market_end_ts_ms"]) - 300_000,
            market_end_ts_ms=int(candidate["market_end_ts_ms"]),
            decision="OPENED",
            reason=str(candidate.get("reason") or ""),
            score=float(candidate.get("score") or 0.0),
            eligible=True,
            opened_cycle_id=cycle_id,
            final_gate=opened_gate,
        )
        return {"status": "PAPER_OPENED", "asset": asset, "cycle_id": cycle_id}

    def _fresh_preflight(self) -> bool:
        snap = self.state.snapshot()
        prior = snap.last_preflight or {}
        checked = int(prior.get("checked_at_ms") or 0)
        if prior.get("ok") and int(time.time() * 1000) - checked <= 60_000:
            return True
        result = self.preflight_fn(self.settings, for_arming=True)
        self.state.remember_preflight(result)
        if not result.get("ok"):
            self.state.halt("DUAL40_PREFLIGHT_REFRESH_FAILED")
            return False
        return True

    def _open_live(self, conn, candidate: dict[str, Any], state_row: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN001
        asset = str(candidate.get("asset") or asset_from_combo_key(str(candidate["combo_key"])))
        if not self.state.can_auto_execute() or not self._fresh_preflight():
            return {"status": "LIVE_NOT_READY"}
        snap = self.state.snapshot()
        level = int(state_row["level_index"])
        quantity = float(self.policy.ladder[level])
        gateway = self._gateway_client()
        collateral = float(gateway.collateral_balance_usdc(refresh=True))
        required = max(
            float(self.settings.dual40_min_collateral_to_arm_usdc),
            2.0 * self.policy.price * quantity + 1.0,
        )
        if collateral + 1e-9 < required:
            self.state.halt("DUAL40_INSUFFICIENT_COLLATERAL")
            return {
                "status": "HALTED_INSUFFICIENT_COLLATERAL",
                "collateral_usdc": collateral,
                "required_usdc": required,
            }

        before = gateway.pair_balances(
            up_token_id=str(candidate["up_token_id"]),
            down_token_id=str(candidate["down_token_id"]),
            refresh=True,
        )
        cycle_id = create_cycle(
            conn,
            scope="LIVE",
            asset=asset,
            session_id=snap.session_id,
            condition_id=str(candidate["condition_id"]),
            combo_key=str(candidate["combo_key"]),
            market_end_ts_ms=int(candidate["market_end_ts_ms"]),
            level_index=level,
            target_shares=quantity,
            maker_price=self.policy.price,
            status="LIVE_SUBMITTING",
            gate=candidate,
            up_token_id=str(candidate["up_token_id"]),
            down_token_id=str(candidate["down_token_id"]),
            loss_pool_before_usdc=float(state_row["loss_pool_usdc"]),
            before_up_shares=float(before["up"]),
            before_down_shares=float(before["down"]),
            details={
                "collateral_before_usdc": collateral,
                "required_collateral_usdc": required,
                "post_only": True,
                "order_type": "GTC",
                "batch_not_atomic": True,
            },
        )
        posted = gateway.post_pair_post_only_gtc(
            up_token_id=str(candidate["up_token_id"]),
            down_token_id=str(candidate["down_token_id"]),
            quantity_shares=quantity,
            price=self.policy.price,
        )
        if not posted.get("ok"):
            code = str(posted.get("error_code") or "DUAL40_SUBMIT_FAILED")
            update_cycle(
                conn,
                cycle_id,
                status="SUBMIT_FAILED",
                error_code=code,
                details_merge={"submit": posted},
            )
            self.state.halt(code)
            return {"status": "HALTED_SUBMIT_FAILED", "asset": asset, "cycle_id": cycle_id, "submit": posted}

        opened_gate = {
            **candidate,
            "stages": [
                *candidate.get("stages", []),
                {
                    "stage": "OPENED",
                    "eligible": True,
                    "reason": "LIVE_ORDERS_POSTED",
                },
            ],
        }
        update_cycle(
            conn,
            cycle_id,
            status="LIVE_RESTING",
            gate_json=_json(opened_gate),
            up_order_id=posted.get("up_order_id"),
            down_order_id=posted.get("down_order_id"),
            heartbeat_id=posted.get("heartbeat_id"),
            last_heartbeat_ms=int(time.time() * 1000),
            orders_posted_at_ms=int(posted.get("submitted_at_ms") or time.time() * 1000),
            details_merge={"submit": posted},
        )
        log.warning(
            "DUAL40 LIVE POSTED id=%s combo=%s q=%.3f UP@%.2f DOWN@%.2f",
            cycle_id,
            candidate["combo_key"],
            quantity,
            self.policy.price,
            self.policy.price,
        )
        upsert_market_decision(
            conn,
            scope="LIVE",
            asset=asset,
            combo_key=str(candidate["combo_key"]),
            condition_id=str(candidate["condition_id"]),
            market_start_ts_ms=int(candidate["market_end_ts_ms"]) - 300_000,
            market_end_ts_ms=int(candidate["market_end_ts_ms"]),
            decision="OPENED",
            reason=str(candidate.get("reason") or ""),
            score=float(candidate.get("score") or 0.0),
            eligible=True,
            opened_cycle_id=cycle_id,
            final_gate=opened_gate,
        )
        return {"status": "LIVE_POSTED", "asset": asset, "cycle_id": cycle_id}

    def _book_for_cycle(self, p26, cycle: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:  # noqa: ANN001
        condition = str(cycle["condition_id"])
        return (
            self._latest_book(p26, condition, "UP"),
            self._latest_book(p26, condition, "DOWN"),
        )

    def _apply_ladder_and_finalize(
        self,
        conn,
        *,
        cycle: dict[str, Any],
        status: str,
        pnl: float,
        official_result: str | None = None,
        merge_tx_hash: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:  # noqa: ANN001
        scope = str(cycle["scope"])
        asset = str(cycle.get("asset") or asset_from_combo_key(str(cycle["combo_key"])))
        current_state = ladder_state(conn, scope, asset)
        before = max(
            float(cycle["loss_pool_before_usdc"]),
            float(current_state["loss_pool_usdc"]),
        )
        transition = next_ladder_state(
            policy=self.policy,
            loss_pool_before=before,
            cycle_pnl=float(pnl),
        )
        state_level_index = transition.level_index
        state_loss_pool = transition.loss_pool
        state_hard_stopped = transition.hard_stopped
        state_hard_stop_reason = transition.reason if transition.hard_stopped else None
        settlement_extra: dict[str, Any] = {}
        if scope == "PAPER" and transition.hard_stopped:
            state_level_index = 0
            state_loss_pool = 0.0
            state_hard_stopped = False
            state_hard_stop_reason = None
            settlement_extra = {
                "paper_hard_stop_action": "RESET_TO_BASE_NEXT_SERIES",
                "paper_unrecovered_loss_pool_usdc": round(transition.loss_pool, 6),
                "paper_hard_stop_reason": transition.reason,
            }
        terminal_decision = "NO_FILL" if status == "NO_FILL" else "SETTLED"
        prior_gate = cycle.get("gate") if isinstance(cycle.get("gate"), dict) else {}
        settled_gate = {
            **prior_gate,
            "stages": [
                *prior_gate.get("stages", []),
                {
                    "stage": terminal_decision,
                    "eligible": True,
                    "reason": status,
                },
            ],
            "settlement": {
                "status": status,
                "pnl_usdc": round(float(pnl), 6),
                "official_result": official_result,
                "ladder_transition": transition.to_dict(),
                **settlement_extra,
            },
        }
        owns_transaction = not conn.in_transaction
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            set_ladder_state(
                conn,
                scope=scope,
                asset=asset,
                level_index=state_level_index,
                loss_pool_usdc=state_loss_pool,
                hard_stopped=state_hard_stopped,
                hard_stop_reason=state_hard_stop_reason,
                last_cycle_id=int(cycle["id"]),
                commit=False,
            )
            update_cycle(
                conn,
                int(cycle["id"]),
                status=status,
                official_result=official_result,
                realized_pnl_usdc=round(float(pnl), 6),
                loss_pool_after_usdc=state_loss_pool,
                merge_tx_hash=merge_tx_hash,
                resolved_at_ms=int(time.time() * 1000),
                details_merge={
                    "ladder_transition": transition.to_dict(),
                    **settlement_extra,
                    **(details or {}),
                },
                commit=False,
            )
            upsert_market_decision(
                conn,
                scope=scope,
                asset=asset,
                combo_key=str(cycle["combo_key"]),
                condition_id=str(cycle["condition_id"]),
                market_start_ts_ms=int(cycle["market_end_ts_ms"]) - 300_000,
                market_end_ts_ms=int(cycle["market_end_ts_ms"]),
                decision=terminal_decision,
                reason=status,
                score=None,
                eligible=True,
                opened_cycle_id=int(cycle["id"]),
                final_gate=settled_gate,
                commit=False,
            )
            if owns_transaction:
                conn.commit()
        except Exception:
            if owns_transaction:
                conn.rollback()
            raise
        if transition.hard_stopped and scope == "LIVE":
            self.state.halt(transition.reason)
        log.warning(
            "DUAL40 FINAL scope=%s asset=%s id=%s combo=%s status=%s pnl=%.4f pool=%.4f next=%.0f hard=%s",
            scope,
            asset,
            cycle["id"],
            cycle["combo_key"],
            status,
            pnl,
            state_loss_pool,
            float(self.policy.ladder[state_level_index]),
            state_hard_stopped,
        )
        return {
            "status": status,
            "asset": asset,
            "cycle_id": cycle["id"],
            "pnl_usdc": round(float(pnl), 6),
            "ladder": {
                **transition.to_dict(),
                "state_level_index": state_level_index,
                "state_loss_pool": round(state_loss_pool, 6),
                "state_hard_stopped": state_hard_stopped,
            },
        }

    def _paper_tick(self, conn, p26, cycle: dict[str, Any], now_ms: int) -> dict[str, Any]:  # noqa: ANN001
        up, down = self._book_for_cycle(p26, cycle)
        up_filled = float(cycle["up_filled_shares"])
        down_filled = float(cycle["down_filled_shares"])
        quantity = float(cycle["target_shares"])
        maker_price = float(cycle["maker_price"])
        up_fill_price = _cycle_side_fill_price(cycle, "UP")
        down_fill_price = _cycle_side_fill_price(cycle, "DOWN")
        near_up = int(cycle["near_touch_up_41"])
        near_down = int(cycle["near_touch_down_41"])

        if up is not None:
            if float(up["best_ask"]) <= float(self.settings.dual40_near_touch_price) + 1e-12:
                near_up = 1
            if (
                up_filled + float(self.settings.dual40_fill_epsilon) < quantity
                and float(up["best_ask"]) <= maker_price + 1e-12
            ):
                up_filled = quantity
                up_fill_price = float(up["best_ask"])
        if down is not None:
            if float(down["best_ask"]) <= float(self.settings.dual40_near_touch_price) + 1e-12:
                near_down = 1
            if (
                down_filled + float(self.settings.dual40_fill_epsilon) < quantity
                and float(down["best_ask"]) <= maker_price + 1e-12
            ):
                down_filled = quantity
                down_fill_price = float(down["best_ask"])

        matched = min(up_filled, down_filled)
        residual = abs(up_filled - down_filled)
        residual_side = "UP" if up_filled > down_filled else ("DOWN" if down_filled > up_filled else None)
        update_cycle(
            conn,
            int(cycle["id"]),
            up_filled_shares=up_filled,
            down_filled_shares=down_filled,
            up_fill_price=(up_fill_price if up_filled > 0 else None),
            down_fill_price=(down_fill_price if down_filled > 0 else None),
            matched_shares=matched,
            residual_side=residual_side,
            residual_shares=residual,
            near_touch_up_41=near_up,
            near_touch_down_41=near_down,
        )
        cycle.update(
            {
                "up_filled_shares": up_filled,
                "down_filled_shares": down_filled,
                "up_fill_price": up_fill_price if up_filled > 0 else None,
                "down_fill_price": down_fill_price if down_filled > 0 else None,
                "matched_shares": matched,
                "residual_side": residual_side,
                "residual_shares": residual,
            }
        )

        epsilon = float(self.settings.dual40_fill_epsilon)
        if up_filled + epsilon >= quantity and down_filled + epsilon >= quantity:
            pnl = matched_pair_pnl(
                price=maker_price,
                matched_shares=quantity,
                up_fill_price=up_fill_price,
                down_fill_price=down_fill_price,
            )
            return self._apply_ladder_and_finalize(
                conn,
                cycle=cycle,
                status="PAPER_MATCHED_FILLED",
                pnl=pnl,
                official_result=None,
                details={
                    "paper_settlement_rule": "PAIR_COMPLETE",
                    "paper_waits_for_official_result": False,
                },
            )

        tte = (int(cycle["market_end_ts_ms"]) - int(now_ms)) / 1000.0
        if int(now_ms) < int(cycle["market_end_ts_ms"]) + 2_000:
            return {
                "status": "PAPER_RESTING",
                "cycle_id": cycle["id"],
                "tte_sec": round(tte, 3),
                "up_filled": up_filled,
                "down_filled": down_filled,
            }
        if up_filled <= epsilon and down_filled <= epsilon:
            return self._apply_ladder_and_finalize(
                conn,
                cycle=cycle,
                status="NO_FILL",
                pnl=0.0,
                details={
                    "paper_settlement_rule": "NO_FILL_AT_MARKET_EXPIRY",
                    "paper_waits_for_official_result": False,
                },
            )
        update_cycle(
            conn,
            int(cycle["id"]),
            status="WAIT_RESOLUTION",
            orders_cancelled_at_ms=int(now_ms),
            details_merge={
                "paper_settlement_rule": "OFFICIAL_RESULT_ACTUAL_FILL_PRICE",
                "paper_waits_for_official_result": True,
                "paper_expiry_up_filled": up_filled,
                "paper_expiry_down_filled": down_filled,
            },
        )
        return {
            "status": "WAIT_RESOLUTION",
            "cycle_id": cycle["id"],
            "residual_side": residual_side,
            "residual_shares": residual,
        }

    def _balance_fill(self, gateway: Any, cycle: dict[str, Any]) -> tuple[float, float, dict[str, float]]:
        balances = gateway.pair_balances(
            up_token_id=str(cycle["up_token_id"]),
            down_token_id=str(cycle["down_token_id"]),
            refresh=True,
        )
        up_delta = max(0.0, float(balances["up"]) - float(cycle["before_up_shares"]))
        down_delta = max(0.0, float(balances["down"]) - float(cycle["before_down_shares"]))
        return up_delta, down_delta, balances

    def _live_heartbeat(self, conn, cycle: dict[str, Any], now_ms: int) -> bool:  # noqa: ANN001
        last = int(cycle.get("last_heartbeat_ms") or 0)
        if int(now_ms) - last < int(float(self.settings.dual40_heartbeat_sec) * 1000.0):
            return True
        gateway = self._gateway_client()
        heartbeat = gateway.start_heartbeat(str(cycle.get("heartbeat_id") or ""))
        if not heartbeat.get("ok"):
            # One immediate retry still leaves ample room inside the heartbeat window.
            heartbeat = gateway.start_heartbeat(str(cycle.get("heartbeat_id") or ""))
        if heartbeat.get("ok"):
            update_cycle(
                conn,
                int(cycle["id"]),
                heartbeat_id=heartbeat.get("heartbeat_id"),
                last_heartbeat_ms=int(now_ms),
                details_merge={"last_heartbeat": heartbeat},
            )
            return True
        update_cycle(
            conn,
            int(cycle["id"]),
            details_merge={"heartbeat_failure": heartbeat},
        )
        return False

    def _cancel_and_classify(self, conn, cycle: dict[str, Any], *, reason: str) -> dict[str, Any]:  # noqa: ANN001
        gateway = self._gateway_client()
        cancel = gateway.cancel_pair(cycle.get("up_order_id"), cycle.get("down_order_id"))
        if not cancel.get("ok"):
            update_cycle(
                conn,
                int(cycle["id"]),
                status="CANCEL_FAILED_HALT",
                error_code="DUAL40_CANCEL_FAILED",
                details_merge={"cancel": cancel, "cancel_reason": reason},
            )
            self.state.halt("DUAL40_CANCEL_FAILED")
            return {"status": "CANCEL_FAILED_HALT", "cycle_id": cycle["id"]}

        time.sleep(min(1.0, float(self.settings.dual40_balance_poll_sec)))
        try:
            up_delta, down_delta, balances = self._balance_fill(gateway, cycle)
        except Exception as exc:  # noqa: BLE001
            update_cycle(
                conn,
                int(cycle["id"]),
                status="BALANCE_UNCERTAIN_HALT",
                error_code="DUAL40_BALANCE_RECONCILIATION_FAILED",
                orders_cancelled_at_ms=int(time.time() * 1000),
                details_merge={
                    "cancel": cancel,
                    "balance_error": {"type": type(exc).__name__, "message": str(exc)[:240]},
                },
            )
            self.state.halt("DUAL40_BALANCE_RECONCILIATION_FAILED")
            return {"status": "BALANCE_UNCERTAIN_HALT", "cycle_id": cycle["id"]}

        up_filled = max(float(cycle["up_filled_shares"]), up_delta)
        down_filled = max(float(cycle["down_filled_shares"]), down_delta)
        matched = min(up_filled, down_filled)
        residual = abs(up_filled - down_filled)
        residual_side = "UP" if up_filled > down_filled else ("DOWN" if down_filled > up_filled else None)
        update_cycle(
            conn,
            int(cycle["id"]),
            status="CANCELLING",
            up_filled_shares=up_filled,
            down_filled_shares=down_filled,
            matched_shares=matched,
            residual_side=residual_side,
            residual_shares=residual,
            orders_cancelled_at_ms=int(time.time() * 1000),
            details_merge={
                "cancel": cancel,
                "cancel_reason": reason,
                "post_cancel_balances": balances,
            },
        )
        cycle.update(
            {
                "up_filled_shares": up_filled,
                "down_filled_shares": down_filled,
                "matched_shares": matched,
                "residual_side": residual_side,
                "residual_shares": residual,
            }
        )

        epsilon = float(self.settings.dual40_fill_epsilon)
        if matched > epsilon:
            merged = gateway.merge_matched(
                condition_id=str(cycle["condition_id"]),
                up_token_id=str(cycle["up_token_id"]),
                down_token_id=str(cycle["down_token_id"]),
                matched_shares=matched,
                before_up=float(cycle["before_up_shares"]),
                before_down=float(cycle["before_down_shares"]),
                acquired_up=up_filled,
                acquired_down=down_filled,
            )
            if not merged.get("verified"):
                update_cycle(
                    conn,
                    int(cycle["id"]),
                    status="MERGE_FAILED_HALT",
                    error_code="DUAL40_MATCHED_MERGE_FAILED",
                    details_merge={"merge": merged},
                )
                self.state.halt("DUAL40_MATCHED_MERGE_FAILED")
                return {"status": "MERGE_FAILED_HALT", "cycle_id": cycle["id"]}
            merge_tx = ((merged.get("merge") or {}).get("transaction_hash"))
        else:
            merged = {"verified": True, "skipped": True}
            merge_tx = None

        if up_filled <= epsilon and down_filled <= epsilon:
            return self._apply_ladder_and_finalize(
                conn,
                cycle=cycle,
                status="NO_FILL",
                pnl=0.0,
                merge_tx_hash=merge_tx,
                details={"merge": merged},
            )
        if residual <= epsilon:
            pnl = matched_pair_pnl(
                price=float(cycle["maker_price"]),
                matched_shares=matched,
            )
            return self._apply_ladder_and_finalize(
                conn,
                cycle=cycle,
                status="LIVE_MATCHED_MERGED",
                pnl=pnl,
                merge_tx_hash=merge_tx,
                details={"merge": merged},
            )

        update_cycle(
            conn,
            int(cycle["id"]),
            status="WAIT_RESOLUTION",
            merge_tx_hash=merge_tx,
            details_merge={"merge": merged},
        )
        return {
            "status": "WAIT_RESOLUTION",
            "cycle_id": cycle["id"],
            "residual_side": residual_side,
            "residual_shares": residual,
        }

    def _live_tick(self, conn, cycle: dict[str, Any], now_ms: int) -> dict[str, Any]:  # noqa: ANN001
        status = str(cycle["status"])
        if status == "LIVE_SUBMITTING":
            # Process restarted or crashed during an ambiguous submit. Do not guess.
            self.state.halt("DUAL40_RESTART_DURING_SUBMIT")
            update_cycle(
                conn,
                int(cycle["id"]),
                status="BALANCE_UNCERTAIN_HALT",
                error_code="DUAL40_RESTART_DURING_SUBMIT",
            )
            return {"status": "BALANCE_UNCERTAIN_HALT", "cycle_id": cycle["id"]}

        if not self.state.is_armed():
            return self._cancel_and_classify(conn, cycle, reason="LIVE_DISARMED_OR_RESTARTED")
        if not self._live_heartbeat(conn, cycle, int(now_ms)):
            return self._cancel_and_classify(conn, cycle, reason="HEARTBEAT_FAILED")

        last_poll = self._last_balance_poll_ms.get(int(cycle["id"]), 0)
        if int(now_ms) - last_poll >= int(float(self.settings.dual40_balance_poll_sec) * 1000.0):
            self._last_balance_poll_ms[int(cycle["id"])] = int(now_ms)
            try:
                up_delta, down_delta, balances = self._balance_fill(self._gateway_client(), cycle)
                up_filled = max(float(cycle["up_filled_shares"]), up_delta)
                down_filled = max(float(cycle["down_filled_shares"]), down_delta)
                matched = min(up_filled, down_filled)
                residual = abs(up_filled - down_filled)
                residual_side = "UP" if up_filled > down_filled else ("DOWN" if down_filled > up_filled else None)
                update_cycle(
                    conn,
                    int(cycle["id"]),
                    up_filled_shares=up_filled,
                    down_filled_shares=down_filled,
                    matched_shares=matched,
                    residual_side=residual_side,
                    residual_shares=residual,
                    details_merge={"last_live_balances": balances},
                )
                cycle.update(
                    {
                        "up_filled_shares": up_filled,
                        "down_filled_shares": down_filled,
                        "matched_shares": matched,
                        "residual_side": residual_side,
                        "residual_shares": residual,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("DUAL40 balance poll failed id=%s error=%s", cycle["id"], exc)

        quantity = float(cycle["target_shares"])
        epsilon = float(self.settings.dual40_fill_epsilon)
        if (
            float(cycle["up_filled_shares"]) + epsilon >= quantity
            and float(cycle["down_filled_shares"]) + epsilon >= quantity
        ):
            return self._cancel_and_classify(conn, cycle, reason="PAIR_FULLY_FILLED")

        tte = (int(cycle["market_end_ts_ms"]) - int(now_ms)) / 1000.0
        if tte <= self.policy.cancel_tte_sec:
            return self._cancel_and_classify(conn, cycle, reason="CANCEL_TTE_REACHED")
        return {
            "status": "LIVE_RESTING",
            "cycle_id": cycle["id"],
            "tte_sec": round(tte, 3),
            "up_filled": cycle["up_filled_shares"],
            "down_filled": cycle["down_filled_shares"],
        }

    def _fetch_official_result(self, cycle: dict[str, Any]) -> tuple[str | None, str]:
        params = urllib.parse.urlencode({"condition_ids": str(cycle["condition_id"])})
        url = f"{self.settings.dual40_gamma_host.rstrip('/')}/markets?{params}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "WhaleSignal-DUAL40-Reconcile/1.0"},
        )
        with urllib.request.urlopen(request, timeout=6.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        markets = payload if isinstance(payload, list) else [payload]
        for market in markets:
            if not isinstance(market, dict):
                continue
            if str(market.get("conditionId") or "") != str(cycle["condition_id"]):
                continue
            result, source = authoritative_official_result(
                market,
                str(cycle["up_token_id"]),
                str(cycle["down_token_id"]),
            )
            return (result.value if result is not None else None), source
        return None, "CONDITION_NOT_FOUND"

    def _resolution_tick(
        self,
        conn,
        cycle: dict[str, Any],
        now_ms: int,
        p26=None,
    ) -> dict[str, Any]:  # noqa: ANN001
        del p26
        if str(cycle.get("scope")) == "PAPER":
            if int(now_ms) < int(cycle["market_end_ts_ms"]) + 2_000:
                return {"status": "WAIT_RESOLUTION", "cycle_id": cycle["id"]}
            up_filled = float(cycle["up_filled_shares"])
            down_filled = float(cycle["down_filled_shares"])
            quantity = float(cycle["target_shares"])
            epsilon = float(self.settings.dual40_fill_epsilon)
            if (
                up_filled + epsilon >= quantity
                and down_filled + epsilon >= quantity
            ):
                return self._apply_ladder_and_finalize(
                    conn,
                    cycle=cycle,
                    status="PAPER_MATCHED_FILLED",
                    pnl=matched_pair_pnl(
                        price=float(cycle["maker_price"]),
                        matched_shares=quantity,
                        up_fill_price=_cycle_side_fill_price(cycle, "UP"),
                        down_fill_price=_cycle_side_fill_price(cycle, "DOWN"),
                    ),
                    official_result=None,
                    details={
                        "paper_settlement_rule": "PAIR_COMPLETE_AT_MARKET_EXPIRY",
                        "paper_waits_for_official_result": False,
                    },
                )
            if up_filled <= epsilon and down_filled <= epsilon:
                return self._apply_ladder_and_finalize(
                    conn,
                    cycle=cycle,
                    status="NO_FILL",
                    pnl=0.0,
                    official_result=None,
                    details={
                        "paper_settlement_rule": "NO_FILL_AT_MARKET_EXPIRY",
                        "paper_waits_for_official_result": False,
                    },
                )
        if int(now_ms) < int(cycle["market_end_ts_ms"]) + 2_000:
            return {"status": "WAIT_RESOLUTION", "cycle_id": cycle["id"]}
        last = self._last_resolution_poll_ms.get(int(cycle["id"]), 0)
        if int(now_ms) - last < int(float(self.settings.dual40_resolution_poll_sec) * 1000.0):
            return {"status": "WAIT_RESOLUTION", "cycle_id": cycle["id"]}
        self._last_resolution_poll_ms[int(cycle["id"])] = int(now_ms)

        try:
            official, source = self._fetch_official_result(cycle)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "WAIT_RESOLUTION",
                "cycle_id": cycle["id"],
                "resolution_error": type(exc).__name__,
            }
        if official not in {"UP", "DOWN"}:
            update_cycle(
                conn,
                int(cycle["id"]),
                details_merge={"last_resolution_source": source},
            )
            return {"status": "WAIT_RESOLUTION", "cycle_id": cycle["id"], "source": source}

        pnl = realized_cycle_pnl(
            price=float(cycle["maker_price"]),
            up_filled=float(cycle["up_filled_shares"]),
            down_filled=float(cycle["down_filled_shares"]),
            official_result=official,
            up_fill_price=(
                _cycle_side_fill_price(cycle, "UP")
                if str(cycle.get("scope")) == "PAPER"
                else None
            ),
            down_fill_price=(
                _cycle_side_fill_price(cycle, "DOWN")
                if str(cycle.get("scope")) == "PAPER"
                else None
            ),
        )
        settlement_details = {"official_result_source": source}
        if str(cycle.get("scope")) == "PAPER":
            settlement_details.update(
                {
                    "paper_settlement_rule": "OFFICIAL_RESULT_ACTUAL_FILL_PRICE",
                    "paper_waits_for_official_result": True,
                }
            )
        return self._apply_ladder_and_finalize(
            conn,
            cycle=cycle,
            status=f"RESOLVED_{official}",
            pnl=pnl,
            official_result=official,
            merge_tx_hash=cycle.get("merge_tx_hash"),
            details=settlement_details,
        )

    def tick(self) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        conn = connect_dual40(self.settings.p3_db_path)
        p26 = open_p26_read_only(self.settings.p26_db_path)
        try:
            scope = "LIVE" if self.state.can_auto_execute() else "PAPER"
            active = active_cycles(conn, scope=scope)
            asset_results: dict[str, Any] = {}
            for cycle in sorted(active, key=lambda item: str(item.get("asset") or "")):
                if str(cycle["status"]) == "WAIT_RESOLUTION":
                    result = self._resolution_tick(conn, cycle, now_ms, p26=p26)
                elif str(cycle["scope"]) == "PAPER":
                    result = self._paper_tick(conn, p26, cycle, now_ms)
                else:
                    result = self._live_tick(conn, cycle, now_ms)
                asset_results[str(result.get("asset") or cycle.get("asset") or asset_from_combo_key(str(cycle["combo_key"])))] = result

            if scope == "PAPER" and not bool(self.settings.dual40_paper_enabled):
                result = {"status": "IDLE_PAPER_DISABLED"}
                self._last_status = result
                return result

            configured_assets = tuple(asset for asset in self.settings.dual40_assets() if asset in DUAL40_ASSETS)
            state_by_asset = {asset: ladder_state(conn, scope, asset) for asset in configured_assets}
            if scope == "PAPER":
                for asset, state_row in list(state_by_asset.items()):
                    if bool(state_row["hard_stopped"]):
                        set_ladder_state(
                            conn,
                            scope=scope,
                            asset=asset,
                            level_index=0,
                            loss_pool_usdc=0.0,
                            hard_stopped=False,
                            hard_stop_reason=None,
                            commit=True,
                        )
                        state_by_asset[asset] = ladder_state(conn, scope, asset)
            active = active_cycles(conn, scope=scope)
            active_by_asset = {str(cycle.get("asset") or asset_from_combo_key(str(cycle["combo_key"]))): cycle for cycle in active}
            ready = self._scan(
                p26,
                conn,
                scope=scope,
                now_ms=now_ms,
                active_by_asset=active_by_asset,
                state_by_asset=state_by_asset,
            )
            limit = (
                int(self.settings.dual40_live_max_concurrent_assets)
                if scope == "LIVE"
                else int(self.settings.dual40_paper_max_concurrent_assets)
            )
            slots = max(0, limit - len(active_by_asset))
            opened = 0
            for candidate in ready:
                if opened >= slots:
                    break
                asset = str(candidate["asset"])
                if asset in asset_results or asset in active_by_asset:
                    continue
                state_row = state_by_asset[asset]
                if bool(state_row["hard_stopped"]):
                    reason = str(state_row.get("hard_stop_reason") or "DUAL40_HARD_STOP")
                    if scope == "LIVE":
                        self.state.halt(reason)
                    asset_results[asset] = {
                        "status": "HARD_STOPPED",
                        "scope": scope,
                        "asset": asset,
                        "reason": reason,
                        "loss_pool_usdc": float(state_row["loss_pool_usdc"]),
                    }
                    continue
                if scope == "LIVE":
                    result = self._open_live(conn, candidate, state_row)
                else:
                    result = self._open_paper(conn, candidate, state_row)
                asset_results[asset] = result
                if result.get("cycle_id"):
                    opened += 1
                    active_by_asset[asset] = {"id": result["cycle_id"], "asset": asset}
                if scope == "LIVE":
                    break
            for asset in configured_assets:
                asset_results.setdefault(
                    asset,
                    {
                        "status": (
                            "ACTIVE"
                            if asset in active_by_asset
                            else (
                                "HARD_STOPPED"
                                if bool(state_by_asset[asset]["hard_stopped"])
                                else "WAITING_FOR_BALANCED_MARKET"
                            )
                        ),
                        "scope": scope,
                        "asset": asset,
                    },
                )
            result = {
                "status": "MULTI_ASSET_TICK",
                "scope": scope,
                "assets": asset_results,
                "active_cycle_count": len(active_by_asset),
                "max_concurrent_assets": limit,
            }
            self._last_status = result
            return result
        finally:
            p26.close()
            conn.close()

    def public_status(self) -> dict[str, Any]:
        payload = build_dual40_summary(self.settings.p3_db_path, limit=100)
        payload["p26_paper"] = p26_paper_decision_summary(
            self.settings.p26_db_path,
            limit=50,
        )
        payload.update(
            {
                "policy": {
                    "price": self.policy.price,
                    "ladder": list(self.policy.ladder),
                    "pair_edge_per_share": self.policy.pair_edge_per_share,
                    "full_ladder_capital_usdc": self.policy.full_ladder_capital,
                    "hard_stop_after_30": True,
                    "one_global_market_only": False,
                    "paper_max_concurrent_assets": int(self.settings.dual40_paper_max_concurrent_assets),
                    "live_max_concurrent_assets": int(self.settings.dual40_live_max_concurrent_assets),
                    "paper_fill_rule": "ENTRY_OR_RECORDED_BEST_ASK_LE_MAKER_FULL_SIDE",
                    "paper_settlement_rule": "OFFICIAL_RESULT_ACTUAL_FILL_PRICE",
                    "paper_waits_for_official_result": True,
                    "paper_settlement_grace_ms": 2000,
                    "paper_entry_profile": "RELAXED_LIMIT_NO_PRICE_REGIME_NO_CONFIRM_NO_CROSS_REJECT",
                    "near_touch_41_diagnostic_only": True,
                    "entry": "PAPER_RELAXED_LIMIT",
                    "paper_entry": "RELAXED_LIMIT",
                    "live_entry": "BALANCED_STABLE_POST_ONLY",
                    "opening_gate_mode": self.settings.dual40_opening_mode(),
                    "forecast_gate_mode": self.settings.dual40_forecast_mode(),
                    "global_risk_mode": self.settings.dual40_risk_mode(),
                    "gate_profiles": [
                        self.settings.dual40_gate_profile(level)
                        for level in range(3)
                    ],
                    "forecast_max_age_ms": int(self.settings.dual40_forecast_max_age_ms),
                    "daily_loss_limit_usdc": float(self.settings.dual40_daily_loss_limit_usdc),
                    "max_recovery_exposure_usdc": float(
                        self.settings.dual40_max_recovery_exposure_usdc
                    ),
                    "cancel_tte_sec": self.policy.cancel_tte_sec,
                    "live_cancel_tte_sec": self.policy.cancel_tte_sec,
                    "paper_cancel_tte_sec": None,
                },
                "runtime": self._last_status,
            }
        )
        return payload

    def shutdown(self) -> None:
        conn = connect_dual40(self.settings.p3_db_path)
        try:
            cycle = active_cycle(conn, scope="LIVE")
            if cycle is not None and str(cycle["scope"]) == "LIVE" and str(cycle["status"]) == "LIVE_RESTING":
                try:
                    self._cancel_and_classify(conn, cycle, reason="DAEMON_SHUTDOWN")
                except Exception as exc:  # noqa: BLE001
                    self.state.halt("DUAL40_SHUTDOWN_CANCEL_FAILED")
                    log.exception("DUAL40 shutdown cancel failed: %s", exc)
        finally:
            conn.close()
