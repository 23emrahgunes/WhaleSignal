"""Stateful DUAL40 post-only maker strategy for P3.

The engine selects at most one BTC/ETH/SOL/XRP 5m market whose CLOB has remained
balanced and non-directional. It then rests equal 40-cent UP/DOWN bids. PAPER fills
are conservative (best ask must actually reach 40 cents); LIVE fills are determined
only from verified conditional-token balance deltas.

Recovery sizing is global and realized-PnL based: 5 -> 10 -> 30 shares, then a
persistent hard stop. No 90/270 continuation exists. A no-fill cycle does not advance
the ladder. A restart always begins DRY; any surviving LIVE resting cycle is cancelled
and reconciled before another market can be considered.
"""
from __future__ import annotations

import json
import logging
import math
import time
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any, Callable

from p25_discovery import authoritative_official_result
from p3_config import P3Settings
from p3_dual40_core import (
    DUAL40_STRATEGY,
    Dual40Policy,
    MidPoint,
    evaluate_balanced_regime,
    matched_pair_pnl,
    next_ladder_state,
    realized_cycle_pnl,
)
from p3_dual40_gateway import Dual40Gateway
from p3_dual40_store import (
    active_cycle,
    connect_dual40,
    create_cycle,
    cycle_for_condition,
    ladder_state,
    set_ladder_state,
    summary as store_summary,
    update_cycle,
    write_scan_status,
)
from p3_live_preflight import run_live_preflight
from p3_live_state import LiveState
from p3_schema import open_p26_read_only


log = logging.getLogger("direction_engine.p3.dual40")


TERMINAL_STATUSES = {
    "PAPER_MATCHED",
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
    }


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
    ) -> None:
        self.settings = settings
        self.state = state
        self.policy = policy_from_settings(settings)
        self.policy.validate()
        self.gateway_factory = gateway_factory
        self.preflight_fn = preflight_fn
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
            out.append(dict(row))
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

    def _candidate(self, p26, market: dict[str, Any], now_ms: int) -> dict[str, Any]:  # noqa: ANN001
        condition = str(market["condition_id"])
        combo = str(market["combo_key"])
        end_ms = int(market["market_end_ts_ms"])
        start_ms = end_ms - 300_000
        market_age = max(0.0, (int(now_ms) - start_ms) / 1000.0)
        tte = max(0.0, (end_ms - int(now_ms)) / 1000.0)
        up = self._latest_book(p26, condition, "UP")
        down = self._latest_book(p26, condition, "DOWN")

        base = {
            "condition_id": condition,
            "combo_key": combo,
            "market_end_ts_ms": end_ms,
            "market_age_sec": round(market_age, 3),
            "tte_sec": round(tte, 3),
            "up_token_id": str(market["up_token_id"]),
            "down_token_id": str(market["down_token_id"]),
        }
        if up is None or down is None:
            return {**base, "eligible": False, "reason": "BOOK_PAIR_MISSING", "score": 0.0}

        up_age = max(0, int(now_ms) - int(up["recv_ts_ms"]))
        down_age = max(0, int(now_ms) - int(down["recv_ts_ms"]))
        base.update(
            {
                "up_book": up,
                "down_book": down,
                "max_book_age_ms": max(up_age, down_age),
                "queue_ahead_up_at_40": float(up["bid_at_40"]),
                "queue_ahead_down_at_40": float(down["bid_at_40"]),
            }
        )
        if max(up_age, down_age) > int(self.settings.dual40_book_fresh_ms):
            return {**base, "eligible": False, "reason": "BOOK_STALE", "score": 0.0}

        fee_ok, fee_reason = self._maker_fee_ready(
            p26,
            condition,
            (str(market["up_token_id"]), str(market["down_token_id"])),
        )
        base["fee_gate"] = fee_reason
        if not fee_ok:
            return {**base, "eligible": False, "reason": fee_reason, "score": 0.0}

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
        )
        return {**base, **gate.to_dict(), "history_points": len(history)}

    def _scan(self, p26, conn, *, scope: str, now_ms: int) -> dict[str, Any] | None:  # noqa: ANN001
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
        active_conditions = {str(market["condition_id"]) for market in markets}
        for condition in list(self._gate_since):
            if condition not in active_conditions:
                self._gate_since.pop(condition, None)

        if transport_ok:
            for market in markets:
                item = self._candidate(p26, market, int(now_ms))
                candidates.append(item)
                reasons[str(item.get("reason") or "UNKNOWN")] += 1
                condition = str(item["condition_id"])
                if item.get("eligible"):
                    self._gate_since.setdefault(condition, time.monotonic())
                    item["stable_for_sec"] = round(
                        max(0.0, time.monotonic() - self._gate_since[condition]),
                        3,
                    )
                else:
                    self._gate_since.pop(condition, None)
                    item["stable_for_sec"] = 0.0
        else:
            reasons["BOOK_TRANSPORT_NOT_LIVE"] += max(1, len(markets))

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
            "reason_counts": dict(reasons),
            "candidates": candidates,
            "one_global_market_only": True,
        }
        if int(now_ms) - self._last_scan_write_ms >= 1000:
            write_scan_status(conn, scan)
            self._last_scan_write_ms = int(now_ms)

        ready = [
            item
            for item in candidates
            if item.get("eligible")
            and float(item.get("stable_for_sec") or 0.0) + 1e-9 >= self.policy.confirm_sec
            and cycle_for_condition(
                conn,
                scope=scope,
                condition_id=str(item["condition_id"]),
            )
            is None
        ]
        if not ready:
            return None
        return max(
            ready,
            key=lambda item: (
                float(item.get("score") or 0.0),
                float(item.get("tte_sec") or 0.0),
            ),
        )

    def _open_paper(self, conn, candidate: dict[str, Any], state_row: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN001
        level = int(state_row["level_index"])
        quantity = float(self.policy.ladder[level])
        cycle_id = create_cycle(
            conn,
            scope="PAPER",
            session_id=None,
            condition_id=str(candidate["condition_id"]),
            combo_key=str(candidate["combo_key"]),
            market_end_ts_ms=int(candidate["market_end_ts_ms"]),
            level_index=level,
            target_shares=quantity,
            maker_price=self.policy.price,
            status="PAPER_RESTING",
            gate=candidate,
            up_token_id=str(candidate["up_token_id"]),
            down_token_id=str(candidate["down_token_id"]),
            loss_pool_before_usdc=float(state_row["loss_pool_usdc"]),
            details={
                "paper_fill_rule": "CONSERVATIVE_BEST_ASK_LE_40",
                "near_touch_41_is_diagnostic_only": True,
                "post_only": True,
                "order_type": "GTC_SIMULATED",
            },
        )
        log.info(
            "DUAL40 PAPER OPEN id=%s combo=%s q=%.3f price=%.2f score=%.3f",
            cycle_id,
            candidate["combo_key"],
            quantity,
            self.policy.price,
            float(candidate.get("score") or 0.0),
        )
        return {"status": "PAPER_OPENED", "cycle_id": cycle_id}

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
            return {"status": "HALTED_SUBMIT_FAILED", "cycle_id": cycle_id, "submit": posted}

        update_cycle(
            conn,
            cycle_id,
            status="LIVE_RESTING",
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
        return {"status": "LIVE_POSTED", "cycle_id": cycle_id}

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
        before = float(cycle["loss_pool_before_usdc"])
        transition = next_ladder_state(
            policy=self.policy,
            loss_pool_before=before,
            cycle_pnl=float(pnl),
        )
        set_ladder_state(
            conn,
            scope=scope,
            level_index=transition.level_index,
            loss_pool_usdc=transition.loss_pool,
            hard_stopped=transition.hard_stopped,
            hard_stop_reason=(transition.reason if transition.hard_stopped else None),
        )
        update_cycle(
            conn,
            int(cycle["id"]),
            status=status,
            official_result=official_result,
            realized_pnl_usdc=round(float(pnl), 6),
            loss_pool_after_usdc=transition.loss_pool,
            merge_tx_hash=merge_tx_hash,
            resolved_at_ms=int(time.time() * 1000),
            details_merge={
                "ladder_transition": transition.to_dict(),
                **(details or {}),
            },
        )
        if transition.hard_stopped and scope == "LIVE":
            self.state.halt(transition.reason)
        log.warning(
            "DUAL40 FINAL scope=%s id=%s combo=%s status=%s pnl=%.4f pool=%.4f next=%.0f hard=%s",
            scope,
            cycle["id"],
            cycle["combo_key"],
            status,
            pnl,
            transition.loss_pool,
            transition.target_shares,
            transition.hard_stopped,
        )
        return {
            "status": status,
            "cycle_id": cycle["id"],
            "pnl_usdc": round(float(pnl), 6),
            "ladder": transition.to_dict(),
        }

    def _paper_tick(self, conn, p26, cycle: dict[str, Any], now_ms: int) -> dict[str, Any]:  # noqa: ANN001
        up, down = self._book_for_cycle(p26, cycle)
        up_filled = float(cycle["up_filled_shares"])
        down_filled = float(cycle["down_filled_shares"])
        quantity = float(cycle["target_shares"])
        maker_price = float(cycle["maker_price"])
        near_up = int(cycle["near_touch_up_41"])
        near_down = int(cycle["near_touch_down_41"])

        if up is not None:
            if float(up["best_ask"]) <= float(self.settings.dual40_near_touch_price) + 1e-12:
                near_up = 1
            if float(up["best_ask"]) <= maker_price + 1e-12:
                up_filled = quantity
        if down is not None:
            if float(down["best_ask"]) <= float(self.settings.dual40_near_touch_price) + 1e-12:
                near_down = 1
            if float(down["best_ask"]) <= maker_price + 1e-12:
                down_filled = quantity

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
            near_touch_up_41=near_up,
            near_touch_down_41=near_down,
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
        if up_filled + epsilon >= quantity and down_filled + epsilon >= quantity:
            pnl = matched_pair_pnl(price=maker_price, matched_shares=quantity)
            return self._apply_ladder_and_finalize(
                conn,
                cycle=cycle,
                status="PAPER_MATCHED",
                pnl=pnl,
                details={"paper_pair_complete_before_cancel": True},
            )

        tte = (int(cycle["market_end_ts_ms"]) - int(now_ms)) / 1000.0
        if tte > self.policy.cancel_tte_sec:
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
                details={"paper_cancel_tte_sec": self.policy.cancel_tte_sec},
            )
        update_cycle(
            conn,
            int(cycle["id"]),
            status="WAIT_RESOLUTION",
            orders_cancelled_at_ms=int(now_ms),
        )
        return {"status": "WAIT_RESOLUTION", "cycle_id": cycle["id"]}

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

    def _resolution_tick(self, conn, cycle: dict[str, Any], now_ms: int) -> dict[str, Any]:  # noqa: ANN001
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
        )
        return self._apply_ladder_and_finalize(
            conn,
            cycle=cycle,
            status=f"RESOLVED_{official}",
            pnl=pnl,
            official_result=official,
            merge_tx_hash=cycle.get("merge_tx_hash"),
            details={"official_result_source": source},
        )

    def tick(self) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        conn = connect_dual40(self.settings.p3_db_path)
        p26 = open_p26_read_only(self.settings.p26_db_path)
        try:
            cycle = active_cycle(conn)
            if cycle is not None:
                if str(cycle["status"]) == "WAIT_RESOLUTION":
                    result = self._resolution_tick(conn, cycle, now_ms)
                elif str(cycle["scope"]) == "PAPER":
                    result = self._paper_tick(conn, p26, cycle, now_ms)
                else:
                    result = self._live_tick(conn, cycle, now_ms)
                self._last_status = result
                return result

            scope = "LIVE" if self.state.can_auto_execute() else "PAPER"
            if scope == "PAPER" and not bool(self.settings.dual40_paper_enabled):
                result = {"status": "IDLE_PAPER_DISABLED"}
                self._last_status = result
                return result

            state_row = ladder_state(conn, scope)
            if bool(state_row["hard_stopped"]):
                reason = str(state_row.get("hard_stop_reason") or "DUAL40_HARD_STOP")
                if scope == "LIVE":
                    self.state.halt(reason)
                result = {
                    "status": "HARD_STOPPED",
                    "scope": scope,
                    "reason": reason,
                    "loss_pool_usdc": float(state_row["loss_pool_usdc"]),
                }
                self._last_status = result
                return result

            candidate = self._scan(p26, conn, scope=scope, now_ms=now_ms)
            if candidate is None:
                result = {"status": "WAITING_FOR_BALANCED_MARKET", "scope": scope}
            elif scope == "LIVE":
                result = self._open_live(conn, candidate, state_row)
            else:
                result = self._open_paper(conn, candidate, state_row)
            self._last_status = result
            return result
        finally:
            p26.close()
            conn.close()

    def public_status(self) -> dict[str, Any]:
        payload = store_summary(self.settings.p3_db_path, limit=50)
        payload.update(
            {
                "policy": {
                    "price": self.policy.price,
                    "ladder": list(self.policy.ladder),
                    "pair_edge_per_share": self.policy.pair_edge_per_share,
                    "full_ladder_capital_usdc": self.policy.full_ladder_capital,
                    "hard_stop_after_30": True,
                    "one_global_market_only": True,
                    "paper_fill_rule": "BEST_ASK_LE_40",
                    "near_touch_41_diagnostic_only": True,
                    "entry": "BALANCED_STABLE_TWO_WAY",
                    "cancel_tte_sec": self.policy.cancel_tte_sec,
                },
                "runtime": self._last_status,
            }
        )
        return payload

    def shutdown(self) -> None:
        conn = connect_dual40(self.settings.p3_db_path)
        try:
            cycle = active_cycle(conn)
            if cycle is not None and str(cycle["scope"]) == "LIVE" and str(cycle["status"]) == "LIVE_RESTING":
                try:
                    self._cancel_and_classify(conn, cycle, reason="DAEMON_SHUTDOWN")
                except Exception as exc:  # noqa: BLE001
                    self.state.halt("DUAL40_SHUTDOWN_CANCEL_FAILED")
                    log.exception("DUAL40 shutdown cancel failed: %s", exc)
        finally:
            conn.close()
