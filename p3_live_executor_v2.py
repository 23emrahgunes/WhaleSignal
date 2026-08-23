"""Equal-share guarded P3 LIVE v2 executor.

Key properties:
- no proportional dollar scaling;
- UP and DOWN always use the same exact share quantity;
- one CLOB pair-book snapshot drives entry depth and pre-submit unwind checks;
- both possible single-leg outcomes must be immediately unwindable within risk caps;
- single-leg exposure uses bounded FOK exits, then an optional emergency FAK reducer;
- any residual exposure or verification ambiguity halts LIVE fail-closed;
- realized collateral delta is persisted for live PnL and rolling-loss controls.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any, Callable

from p3_config import P3Settings
from p3_confirmation import CONFIRMED, select_confirmed_observation
from p3_live_gateway_v2 import RiskAwarePolymarketLiveGateway
from p3_live_ledger import (
    create_live_ledger_row,
    ensure_live_ledger_schema,
    finalize_live_ledger_row,
    rolling_24h_gross_loss_usdc,
)
from p3_live_preflight import run_live_preflight
from p3_live_sizing import (
    DepthQuote,
    buy_merge_metrics,
    edge_to_unwind_loss_ratio,
    fee_for_fills,
    projected_unwind_loss,
    select_equal_share_quantity,
)
from p3_live_state import LiveState
from p3_models import ARB_BUY_MERGE
from p3_schema import connect_p3, ensure_p3_schema, open_p26_read_only


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _quote_dict(quote: DepthQuote) -> dict[str, Any]:
    return {
        "requested_shares": quote.requested_shares,
        "filled_shares": quote.filled_shares,
        "complete": quote.complete,
        "notional_usdc": quote.notional_usdc,
        "vwap": quote.vwap,
        "worst_price": quote.worst_price,
        "capacity_shares": quote.capacity_shares,
        "min_order_size": quote.min_order_size,
        "fills": [{"price": x.price, "shares": x.shares} for x in quote.fills],
    }


class P3LiveExecutorV2:
    def __init__(
        self,
        settings: P3Settings,
        state: LiveState,
        *,
        gateway_factory: Callable[[P3Settings], Any] = RiskAwarePolymarketLiveGateway,
        preflight_fn: Callable[..., dict[str, Any]] = run_live_preflight,
    ) -> None:
        self.settings = settings
        self.state = state
        self.gateway_factory = gateway_factory
        self.preflight_fn = preflight_fn

    def _refresh_preflight_if_needed(self) -> bool:
        snap = self.state.snapshot()
        last = snap.last_preflight or {}
        checked = int(last.get("checked_at_ms") or 0)
        if last.get("ok") and int(time.time() * 1000) - checked <= 60_000:
            return True
        result = self.preflight_fn(self.settings, for_arming=True)
        self.state.remember_preflight(result)
        if not result.get("ok"):
            self.state.halt("LIVE_PREFLIGHT_REFRESH_FAILED")
            return False
        return True

    @staticmethod
    def _cycle_exists(conn, *, session_id: str, window_id: int) -> bool:  # noqa: ANN001
        return conn.execute(
            "SELECT 1 FROM p3_live_cycles WHERE session_id=? AND window_id=?",
            (session_id, int(window_id)),
        ).fetchone() is not None

    def _next_candidate(
        self,
        conn,
        *,
        session_id: str,
        armed_at_ms: int,
    ) -> dict[str, Any] | None:  # noqa: ANN001
        rows = conn.execute(
            """
            SELECT id,strategy,condition_id,combo_key,opened_ts_ms
            FROM p3_windows
            WHERE opened_ts_ms>=?
            ORDER BY opened_ts_ms,id
            LIMIT 200
            """,
            (int(armed_at_ms),),
        ).fetchall()
        for window in rows:
            window_id = int(window["id"])
            if self._cycle_exists(conn, session_id=session_id, window_id=window_id):
                continue
            selection = select_confirmed_observation(
                conn,
                window_id=window_id,
                confirm_ms=int(self.settings.dry_entry_confirm_ms),
                max_gap_ms=int(self.settings.dry_confirm_max_gap_ms),
            )
            if selection.status != CONFIRMED or selection.entry_ts_ms is None:
                continue
            if int(selection.entry_ts_ms) < int(armed_at_ms):
                continue
            opp = conn.execute(
                "SELECT * FROM p3_opportunities WHERE id=?",
                (int(selection.opportunity_id),),
            ).fetchone()
            if opp is None:
                continue
            return {
                "window": dict(window),
                "selection": selection,
                "opportunity": dict(opp),
            }
        return None

    @staticmethod
    def _tokens(p26, condition_id: str) -> tuple[str, str]:  # noqa: ANN001
        rows = p26.execute(
            """
            SELECT side,token_id,id
            FROM p26_clob_books
            WHERE condition_id=? AND side IN ('UP','DOWN')
            ORDER BY id DESC
            """,
            (str(condition_id),),
        ).fetchall()
        values: dict[str, str] = {}
        for row in rows:
            side = str(row["side"])
            if side not in values:
                values[side] = str(row["token_id"])
            if "UP" in values and "DOWN" in values:
                break
        if not values.get("UP") or not values.get("DOWN"):
            raise RuntimeError("latest P2.6 UP/DOWN token mapping missing")
        return values["UP"], values["DOWN"]

    @staticmethod
    def _fee_config(p26, condition_id: str, token_id: str) -> dict[str, Any]:  # noqa: ANN001
        row = p26.execute(
            """
            SELECT enabled,rate,exponent,taker_only,source,source_ts_ms
            FROM p26_fee_schedules
            WHERE condition_id=? AND token_id=?
            """,
            (str(condition_id), str(token_id)),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"fee schedule missing for token {token_id}")
        return {
            "enabled": bool(row["enabled"]),
            "rate": float(row["rate"]),
            "exponent": float(row["exponent"]),
            "taker_only": bool(row["taker_only"]),
            "source": str(row["source"]),
            "source_ts_ms": int(row["source_ts_ms"]),
        }

    @staticmethod
    def _fee(quote: DepthQuote, cfg: dict[str, Any]) -> float:
        return fee_for_fills(
            quote.fills,
            enabled=bool(cfg["enabled"]),
            rate=float(cfg["rate"]),
            exponent=float(cfg["exponent"]),
        )

    def _insert_cycle(
        self,
        conn,
        *,
        session_id: str,
        candidate: dict[str, Any],
        up_token: str,
        down_token: str,
        quantity_shares: float,
        capital_usdc: float,
        up_limit_price: float,
        down_limit_price: float,
        status: str,
        details: dict[str, Any],
    ) -> int:  # noqa: ANN001
        opp = candidate["opportunity"]
        sel = candidate["selection"]
        now = int(time.time() * 1000)
        cur = conn.execute(
            """
            INSERT INTO p3_live_cycles(
                session_id,window_id,observation_id,opportunity_id,strategy,
                condition_id,combo_key,entry_ts_ms,quantity_shares,capital_usdc,
                up_token_id,down_token_id,up_limit_price,down_limit_price,status,
                details_json,created_at_ms,updated_at_ms
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(session_id), int(candidate["window"]["id"]), int(sel.observation_id),
                int(sel.opportunity_id), str(opp["strategy"]), str(opp["condition_id"]),
                str(opp["combo_key"]), int(sel.entry_ts_ms), float(quantity_shares),
                float(capital_usdc), str(up_token), str(down_token),
                float(up_limit_price), float(down_limit_price), str(status),
                _json(details), now, now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    @staticmethod
    def _update_cycle(conn, cycle_id: int, *, status: str, **fields: Any) -> None:  # noqa: ANN001
        allowed = {
            "up_order_id", "down_order_id", "up_fill_verified", "down_fill_verified",
            "merge_tx_hash", "unwind_side", "unwind_order_id", "error_code", "details_json",
        }
        pairs = ["status=?", "updated_at_ms=?"]
        values: list[Any] = [str(status), int(time.time() * 1000)]
        for key, value in fields.items():
            if key in allowed:
                pairs.append(f"{key}=?")
                values.append(value)
        values.append(int(cycle_id))
        conn.execute(f"UPDATE p3_live_cycles SET {','.join(pairs)} WHERE id=?", values)
        conn.commit()

    def _skip(
        self,
        conn,
        *,
        snap,
        candidate: dict[str, Any],
        up_token: str,
        down_token: str,
        status: str,
        details: dict[str, Any],
        quantity_shares: float = 0.000001,
        capital_usdc: float = 0.0,
        up_limit_price: float | None = None,
        down_limit_price: float | None = None,
    ) -> dict[str, Any]:  # noqa: ANN001
        opp = candidate["opportunity"]
        cycle_id = self._insert_cycle(
            conn,
            session_id=snap.session_id,
            candidate=candidate,
            up_token=up_token,
            down_token=down_token,
            quantity_shares=max(0.000001, float(quantity_shares)),
            capital_usdc=max(0.0, float(capital_usdc)),
            up_limit_price=float(up_limit_price if up_limit_price is not None else opp["up_limit_price"]),
            down_limit_price=float(down_limit_price if down_limit_price is not None else opp["down_limit_price"]),
            status=status,
            details=details,
        )
        return {"status": status, "cycle_id": cycle_id}

    @staticmethod
    def _entry_cost_per_share(quote: DepthQuote, fee_usdc: float) -> float:
        q = max(1e-12, float(quote.requested_shares))
        return (float(quote.notional_usdc) + float(fee_usdc)) / q

    def _unwind_exposure(
        self,
        *,
        gateway: Any,
        token_id: str,
        shares: float,
        before_entry_balance: float,
        pre_sell_quote: DepthQuote,
        buy_quote: DepthQuote,
        buy_fee_usdc: float,
        fee_cfg: dict[str, Any],
    ) -> dict[str, Any]:
        """Three-stage flatness chain: bounded FOK -> refreshed bounded FOK -> FAK."""
        remaining = max(0.0, float(shares))
        attempts: list[dict[str, Any]] = []
        if remaining <= 1e-6:
            return {"verified": True, "attempts": attempts, "residual": 0.0}

        # Stage 1: use the price that was verified before entry.
        if pre_sell_quote.complete and pre_sell_quote.worst_price is not None:
            first = gateway.unwind_limit_fok(
                token_id=token_id,
                shares=remaining,
                min_price=float(pre_sell_quote.worst_price),
            )
            verify = gateway.wait_for_unwind(
                token_id=token_id,
                before_entry_balance=before_entry_balance,
            )
            attempts.append({"stage": "PRECHECK_LIMIT_FOK", "order": first, "verify": verify})
            remaining = max(0.0, float(verify.get("residual") or 0.0))
            if bool(verify.get("verified")) or remaining <= 1e-5:
                return {"verified": True, "attempts": attempts, "residual": remaining}

        # Stage 2: refresh book, but only accept a bounded emergency loss.
        fresh = gateway.quote_sell(token_id=token_id, shares=remaining)
        fresh_fee = self._fee(fresh, fee_cfg) if fresh.complete else math.inf
        entry_value = self._entry_cost_per_share(buy_quote, buy_fee_usdc) * remaining
        projected_loss = math.inf
        if fresh.complete:
            projected_loss = max(
                0.0,
                entry_value - (float(fresh.notional_usdc) - float(fresh_fee)),
            )
        if (
            fresh.complete
            and fresh.worst_price is not None
            and projected_loss <= float(self.settings.live_emergency_unwind_loss_usdc) + 1e-9
        ):
            second = gateway.unwind_limit_fok(
                token_id=token_id,
                shares=remaining,
                min_price=float(fresh.worst_price),
            )
            verify2 = gateway.wait_for_unwind(
                token_id=token_id,
                before_entry_balance=before_entry_balance,
            )
            attempts.append({
                "stage": "REFRESHED_LIMIT_FOK",
                "projected_loss_usdc": projected_loss,
                "order": second,
                "verify": verify2,
            })
            remaining = max(0.0, float(verify2.get("residual") or 0.0))
            if bool(verify2.get("verified")) or remaining <= 1e-5:
                return {"verified": True, "attempts": attempts, "residual": remaining}

        # Stage 3: reduce any remaining exposure immediately. FAK never rests.
        if self.settings.live_emergency_fak_enabled and remaining > 1e-5:
            emergency = gateway.emergency_unwind_fak(token_id=token_id, shares=remaining)
            verify3 = gateway.wait_for_unwind(
                token_id=token_id,
                before_entry_balance=before_entry_balance,
            )
            attempts.append({"stage": "EMERGENCY_MARKET_FAK", "order": emergency, "verify": verify3})
            remaining = max(0.0, float(verify3.get("residual") or 0.0))
            if bool(verify3.get("verified")) or remaining <= 1e-5:
                return {"verified": True, "attempts": attempts, "residual": remaining}

        return {"verified": False, "attempts": attempts, "residual": remaining}

    def process_once(self) -> dict[str, Any]:
        if not self.state.can_auto_execute():
            return {"status": "IDLE_NOT_AUTO_ARMED"}
        if not self._refresh_preflight_if_needed():
            return {"status": "HALTED_PREFLIGHT"}

        snap = self.state.snapshot()
        if not self.state.can_auto_execute() or snap.armed_at_ms is None:
            return {"status": "IDLE_NOT_ARMED"}

        conn = connect_p3(self.settings.p3_db_path)
        ensure_p3_schema(conn)
        ensure_live_ledger_schema(conn)
        p26 = open_p26_read_only(self.settings.p26_db_path)
        try:
            rolling_loss = rolling_24h_gross_loss_usdc(conn)
            if rolling_loss >= float(self.settings.live_rolling_24h_gross_loss_limit_usdc) - 1e-9:
                self.state.halt("ROLLING_24H_GROSS_LOSS_LIMIT")
                return {
                    "status": "HALTED_ROLLING_24H_LOSS_LIMIT",
                    "rolling_24h_gross_loss_usdc": rolling_loss,
                }

            candidate = self._next_candidate(
                conn,
                session_id=snap.session_id,
                armed_at_ms=int(snap.armed_at_ms),
            )
            if candidate is None:
                return {"status": "NO_CONFIRMED_WINDOW"}

            opp = candidate["opportunity"]
            sel = candidate["selection"]
            window_id = int(candidate["window"]["id"])
            up_token, down_token = self._tokens(p26, str(opp["condition_id"]))

            if str(opp["strategy"]) != ARB_BUY_MERGE:
                return self._skip(
                    conn,
                    snap=snap,
                    candidate=candidate,
                    up_token=up_token,
                    down_token=down_token,
                    status="SKIPPED_UNSUPPORTED_STRATEGY",
                    details={},
                )

            max_submit_age_ms = max(
                500,
                int(self.settings.scan_interval_ms) + 3 * int(self.settings.live_poll_interval_ms),
            )
            entry_age_ms = int(time.time() * 1000) - int(sel.entry_ts_ms)
            if entry_age_ms > max_submit_age_ms:
                return self._skip(
                    conn,
                    snap=snap,
                    candidate=candidate,
                    up_token=up_token,
                    down_token=down_token,
                    status="SKIPPED_STALE_CONFIRMATION",
                    details={"entry_age_ms": entry_age_ms, "max_submit_age_ms": max_submit_age_ms},
                )

            gateway = self.gateway_factory(self.settings)
            up_book, down_book = gateway.fetch_pair_books(
                up_token_id=up_token,
                down_token_id=down_token,
            )
            up_cap = gateway.buy_capacity_from_book(
                up_book, max_price=float(opp["up_limit_price"])
            )
            down_cap = gateway.buy_capacity_from_book(
                down_book, max_price=float(opp["down_limit_price"])
            )
            min_size = max(
                float(up_cap.get("min_order_size") or 0.0),
                float(down_cap.get("min_order_size") or 0.0),
            )
            q = select_equal_share_quantity(
                strict_optimal_shares=float(opp["quantity_shares"]),
                target_shares=float(self.settings.live_target_quantity_shares),
                hard_max_shares=float(self.settings.live_max_quantity_shares),
                up_capacity_shares=float(up_cap.get("capacity_shares") or 0.0),
                down_capacity_shares=float(down_cap.get("capacity_shares") or 0.0),
                min_order_size=min_size,
            )
            if q <= 0:
                return self._skip(
                    conn,
                    snap=snap,
                    candidate=candidate,
                    up_token=up_token,
                    down_token=down_token,
                    status="SKIPPED_FRESH_DEPTH_OR_MIN_SIZE",
                    details={"up_capacity": up_cap, "down_capacity": down_cap, "min_size": min_size},
                )

            # Recompute entry economics for this exact same Q from the same pair-book snapshot.
            up_buy = gateway.quote_buy_from_book(
                up_book, shares=q, max_price=float(opp["up_limit_price"])
            )
            down_buy = gateway.quote_buy_from_book(
                down_book, shares=q, max_price=float(opp["down_limit_price"])
            )
            if not up_buy.complete or not down_buy.complete:
                return self._skip(
                    conn,
                    snap=snap,
                    candidate=candidate,
                    up_token=up_token,
                    down_token=down_token,
                    status="SKIPPED_FRESH_DEPTH_RACE",
                    details={"up_buy": _quote_dict(up_buy), "down_buy": _quote_dict(down_buy)},
                    quantity_shares=q,
                )

            up_fee_cfg = self._fee_config(p26, str(opp["condition_id"]), up_token)
            down_fee_cfg = self._fee_config(p26, str(opp["condition_id"]), down_token)
            up_buy_fee = self._fee(up_buy, up_fee_cfg)
            down_buy_fee = self._fee(down_buy, down_fee_cfg)
            metrics = buy_merge_metrics(
                quantity_shares=q,
                up_buy=up_buy,
                down_buy=down_buy,
                up_fee_usdc=up_buy_fee,
                down_fee_usdc=down_buy_fee,
                execution_buffer_per_share=float(self.settings.execution_buffer_per_share),
            )
            up_submit_price = float(up_buy.worst_price or opp["up_limit_price"])
            down_submit_price = float(down_buy.worst_price or opp["down_limit_price"])

            if (
                metrics["net_profit_usdc"] < float(self.settings.live_min_net_profit_usdc)
                or metrics["net_roi"] < float(self.settings.live_min_net_roi)
            ):
                return self._skip(
                    conn,
                    snap=snap,
                    candidate=candidate,
                    up_token=up_token,
                    down_token=down_token,
                    status="SKIPPED_LIVE_EDGE_GATE",
                    details={"fresh_metrics": metrics},
                    quantity_shares=q,
                    capital_usdc=metrics["capital_usdc"],
                    up_limit_price=up_submit_price,
                    down_limit_price=down_submit_price,
                )

            up_entry_cost = float(up_buy.notional_usdc) + up_buy_fee
            down_entry_cost = float(down_buy.notional_usdc) + down_buy_fee
            if max(up_entry_cost, down_entry_cost) > float(
                self.settings.live_max_single_leg_notional_usdc
            ) + 1e-9:
                return self._skip(
                    conn,
                    snap=snap,
                    candidate=candidate,
                    up_token=up_token,
                    down_token=down_token,
                    status="SKIPPED_SINGLE_LEG_NOTIONAL_CAP",
                    details={"up_entry_cost": up_entry_cost, "down_entry_cost": down_entry_cost},
                    quantity_shares=q,
                    capital_usdc=metrics["capital_usdc"],
                    up_limit_price=up_submit_price,
                    down_limit_price=down_submit_price,
                )

            # Pre-submit one-leg escape plan from the same book snapshot.
            up_sell = gateway.quote_sell_from_book(up_book, shares=q)
            down_sell = gateway.quote_sell_from_book(down_book, shares=q)
            if not up_sell.complete or not down_sell.complete:
                return self._skip(
                    conn,
                    snap=snap,
                    candidate=candidate,
                    up_token=up_token,
                    down_token=down_token,
                    status="SKIPPED_UNWIND_DEPTH",
                    details={"up_sell": _quote_dict(up_sell), "down_sell": _quote_dict(down_sell)},
                    quantity_shares=q,
                    capital_usdc=metrics["capital_usdc"],
                    up_limit_price=up_submit_price,
                    down_limit_price=down_submit_price,
                )

            up_sell_fee = self._fee(up_sell, up_fee_cfg)
            down_sell_fee = self._fee(down_sell, down_fee_cfg)
            up_unwind_loss = projected_unwind_loss(
                buy_quote=up_buy,
                buy_fee_usdc=up_buy_fee,
                sell_quote=up_sell,
                sell_fee_usdc=up_sell_fee,
            )
            down_unwind_loss = projected_unwind_loss(
                buy_quote=down_buy,
                buy_fee_usdc=down_buy_fee,
                sell_quote=down_sell,
                sell_fee_usdc=down_sell_fee,
            )
            worst_unwind_loss = max(up_unwind_loss, down_unwind_loss)
            rr = edge_to_unwind_loss_ratio(metrics["net_profit_usdc"], worst_unwind_loss)
            if worst_unwind_loss > float(self.settings.live_max_projected_unwind_loss_usdc) + 1e-9:
                return self._skip(
                    conn,
                    snap=snap,
                    candidate=candidate,
                    up_token=up_token,
                    down_token=down_token,
                    status="SKIPPED_PROJECTED_UNWIND_LOSS",
                    details={
                        "up_projected_unwind_loss_usdc": up_unwind_loss,
                        "down_projected_unwind_loss_usdc": down_unwind_loss,
                        "max_allowed": self.settings.live_max_projected_unwind_loss_usdc,
                    },
                    quantity_shares=q,
                    capital_usdc=metrics["capital_usdc"],
                    up_limit_price=up_submit_price,
                    down_limit_price=down_submit_price,
                )
            if rr + 1e-12 < float(self.settings.live_min_edge_to_unwind_loss_ratio):
                return self._skip(
                    conn,
                    snap=snap,
                    candidate=candidate,
                    up_token=up_token,
                    down_token=down_token,
                    status="SKIPPED_EDGE_TO_UNWIND_RISK",
                    details={"edge_to_unwind_loss_ratio": rr, "worst_unwind_loss_usdc": worst_unwind_loss},
                    quantity_shares=q,
                    capital_usdc=metrics["capital_usdc"],
                    up_limit_price=up_submit_price,
                    down_limit_price=down_submit_price,
                )

            collateral = float(gateway.collateral_balance_usdc(refresh=True))
            if collateral + 1e-9 < float(metrics["capital_usdc"]):
                return self._skip(
                    conn,
                    snap=snap,
                    candidate=candidate,
                    up_token=up_token,
                    down_token=down_token,
                    status="SKIPPED_INSUFFICIENT_BALANCE",
                    details={"collateral_usdc": collateral, "required_usdc": metrics["capital_usdc"]},
                    quantity_shares=q,
                    capital_usdc=metrics["capital_usdc"],
                    up_limit_price=up_submit_price,
                    down_limit_price=down_submit_price,
                )

            before_up = float(gateway.conditional_balance_shares(up_token, refresh=True))
            before_down = float(gateway.conditional_balance_shares(down_token, refresh=True))
            plan = {
                "sizing_mode": "EQUAL_SHARES_FRESH_DEPTH",
                "quantity_shares_each_leg": q,
                "fresh_metrics": metrics,
                "up_buy": _quote_dict(up_buy),
                "down_buy": _quote_dict(down_buy),
                "up_projected_unwind": _quote_dict(up_sell),
                "down_projected_unwind": _quote_dict(down_sell),
                "up_projected_unwind_loss_usdc": up_unwind_loss,
                "down_projected_unwind_loss_usdc": down_unwind_loss,
                "worst_projected_unwind_loss_usdc": worst_unwind_loss,
                "edge_to_unwind_loss_ratio": rr,
                "collateral_before_usdc": collateral,
            }
            cycle_id = self._insert_cycle(
                conn,
                session_id=snap.session_id,
                candidate=candidate,
                up_token=up_token,
                down_token=down_token,
                quantity_shares=q,
                capital_usdc=float(metrics["capital_usdc"]),
                up_limit_price=up_submit_price,
                down_limit_price=down_submit_price,
                status="PRE_SUBMIT_CLAIMED",
                details=plan,
            )
            create_live_ledger_row(
                conn,
                cycle_id=cycle_id,
                session_id=snap.session_id,
                window_id=window_id,
                combo_key=str(opp["combo_key"]),
                quantity_shares=q,
                planned_capital_usdc=float(metrics["capital_usdc"]),
                planned_net_profit_usdc=float(metrics["net_profit_usdc"]),
                planned_net_roi=float(metrics["net_roi"]),
                projected_worst_unwind_loss_usdc=float(worst_unwind_loss),
                collateral_before_usdc=collateral,
            )

            if not self.state.can_auto_execute():
                self._update_cycle(conn, cycle_id, status="ABORTED_DISARMED_BEFORE_SUBMIT")
                finalize_live_ledger_row(
                    conn,
                    cycle_id=cycle_id,
                    outcome="ABORTED_DISARMED_BEFORE_SUBMIT",
                    collateral_after_usdc=collateral,
                )
                return {"status": "ABORTED_DISARMED_BEFORE_SUBMIT", "cycle_id": cycle_id}

            posted = gateway.post_two_leg_fok(
                up_token_id=up_token,
                down_token_id=down_token,
                quantity_shares=q,
                up_limit_price=up_submit_price,
                down_limit_price=down_submit_price,
            )
            self._update_cycle(
                conn,
                cycle_id,
                status="ORDERS_POSTED",
                up_order_id=posted.get("up_order_id"),
                down_order_id=posted.get("down_order_id"),
                details_json=_json({**plan, "posted": posted}),
            )

            settled = gateway.wait_for_leg_deltas(
                up_token_id=up_token,
                down_token_id=down_token,
                before_up=before_up,
                before_down=before_down,
                quantity_shares=q,
            )
            up_delta = min(q, max(0.0, float(settled.get("up_delta") or 0.0)))
            down_delta = min(q, max(0.0, float(settled.get("down_delta") or 0.0)))
            up_ok = bool(settled.get("up_verified"))
            down_ok = bool(settled.get("down_verified"))
            self._update_cycle(
                conn,
                cycle_id,
                status="SETTLEMENT_CHECKED",
                up_fill_verified=int(up_ok),
                down_fill_verified=int(down_ok),
                details_json=_json({**plan, "posted": posted, "settled": settled}),
            )

            if up_ok and down_ok:
                merge = gateway.merge_positions(
                    condition_id=str(opp["condition_id"]), quantity_shares=q
                )
                merge_wait = gateway.wait_for_merge(
                    up_token_id=up_token,
                    down_token_id=down_token,
                    before_up=before_up,
                    before_down=before_down,
                )
                if not merge.get("verified") or not merge_wait.get("verified"):
                    self._update_cycle(
                        conn,
                        cycle_id,
                        status="HALTED_MERGE_NOT_VERIFIED",
                        merge_tx_hash=merge.get("transaction_hash"),
                        error_code="MERGE_NOT_VERIFIED",
                        details_json=_json({**plan, "merge": merge, "merge_wait": merge_wait, "settled": settled}),
                    )
                    self.state.halt("MERGE_NOT_VERIFIED")
                    return {"status": "HALTED_MERGE_NOT_VERIFIED", "cycle_id": cycle_id}
                collateral_after = float(gateway.wait_for_collateral_stable())
                realized = finalize_live_ledger_row(
                    conn,
                    cycle_id=cycle_id,
                    outcome="MERGED_VERIFIED",
                    collateral_after_usdc=collateral_after,
                )
                self._update_cycle(
                    conn,
                    cycle_id,
                    status="MERGED_VERIFIED",
                    merge_tx_hash=merge.get("transaction_hash"),
                    details_json=_json({
                        **plan,
                        "merge": merge,
                        "merge_wait": merge_wait,
                        "settled": settled,
                        "collateral_after_usdc": collateral_after,
                        **realized,
                    }),
                )
                return {
                    "status": "MERGED_VERIFIED",
                    "cycle_id": cycle_id,
                    "window_id": window_id,
                    "quantity_shares_each_leg": q,
                    **realized,
                }

            exposures: list[dict[str, Any]] = []
            if up_delta > 1e-6:
                exposures.append({
                    "side": "UP", "token": up_token, "shares": up_delta,
                    "before": before_up, "sell": up_sell, "buy": up_buy,
                    "buy_fee": up_buy_fee, "fee_cfg": up_fee_cfg,
                })
            if down_delta > 1e-6:
                exposures.append({
                    "side": "DOWN", "token": down_token, "shares": down_delta,
                    "before": before_down, "sell": down_sell, "buy": down_buy,
                    "buy_fee": down_buy_fee, "fee_cfg": down_fee_cfg,
                })

            if not exposures:
                collateral_after = float(gateway.wait_for_collateral_stable())
                realized = finalize_live_ledger_row(
                    conn,
                    cycle_id=cycle_id,
                    outcome="NO_FILL_VERIFIED",
                    collateral_after_usdc=collateral_after,
                )
                self._update_cycle(
                    conn,
                    cycle_id,
                    status="NO_FILL_VERIFIED",
                    details_json=_json({**plan, "settled": settled, **realized}),
                )
                return {"status": "NO_FILL_VERIFIED", "cycle_id": cycle_id, **realized}

            all_attempts: list[dict[str, Any]] = []
            unwind_order_ids: list[str] = []
            residuals: list[dict[str, Any]] = []
            for exposure in exposures:
                result = self._unwind_exposure(
                    gateway=gateway,
                    token_id=str(exposure["token"]),
                    shares=float(exposure["shares"]),
                    before_entry_balance=float(exposure["before"]),
                    pre_sell_quote=exposure["sell"],
                    buy_quote=exposure["buy"],
                    buy_fee_usdc=float(exposure["buy_fee"]),
                    fee_cfg=exposure["fee_cfg"],
                )
                for attempt in result["attempts"]:
                    attempt["side"] = exposure["side"]
                    all_attempts.append(attempt)
                    order_id = ((attempt.get("order") or {}).get("order_id"))
                    if order_id:
                        unwind_order_ids.append(str(order_id))
                if not result.get("verified"):
                    residuals.append({
                        "side": exposure["side"],
                        "token": exposure["token"],
                        "residual": result.get("residual"),
                    })

            collateral_after = float(gateway.wait_for_collateral_stable())
            one_leg_label = "+".join(str(x["side"]) for x in exposures)
            if residuals:
                realized = finalize_live_ledger_row(
                    conn,
                    cycle_id=cycle_id,
                    outcome="HALTED_RESIDUAL_EXPOSURE",
                    collateral_after_usdc=collateral_after,
                    one_leg_event=True,
                    unwind_attempts=len(all_attempts),
                )
                self._update_cycle(
                    conn,
                    cycle_id,
                    status="HALTED_RESIDUAL_EXPOSURE",
                    unwind_side=one_leg_label,
                    unwind_order_id=",".join(unwind_order_ids) or None,
                    error_code="RESIDUAL_EXPOSURE",
                    details_json=_json({
                        **plan,
                        "settled": settled,
                        "unwind_attempts": all_attempts,
                        "residuals": residuals,
                        "collateral_after_usdc": collateral_after,
                        **realized,
                    }),
                )
                self.state.halt("RESIDUAL_EXPOSURE_REQUIRES_OPERATOR")
                return {
                    "status": "HALTED_RESIDUAL_EXPOSURE",
                    "cycle_id": cycle_id,
                    "residuals": residuals,
                    **realized,
                }

            realized = finalize_live_ledger_row(
                conn,
                cycle_id=cycle_id,
                outcome="ONE_LEG_UNWOUND_VERIFIED",
                collateral_after_usdc=collateral_after,
                one_leg_event=True,
                unwind_attempts=len(all_attempts),
            )
            self._update_cycle(
                conn,
                cycle_id,
                status="ONE_LEG_UNWOUND_VERIFIED",
                unwind_side=one_leg_label,
                unwind_order_id=",".join(unwind_order_ids) or None,
                details_json=_json({
                    **plan,
                    "settled": settled,
                    "unwind_attempts": all_attempts,
                    "collateral_after_usdc": collateral_after,
                    **realized,
                }),
            )
            if self.settings.live_halt_after_one_leg:
                self.state.halt("ONE_LEG_RISK_EVENT_REVIEW_REQUIRED")
                return {
                    "status": "ONE_LEG_UNWOUND_VERIFIED_HALTED",
                    "cycle_id": cycle_id,
                    "quantity_shares": q,
                    **realized,
                }
            return {
                "status": "ONE_LEG_UNWOUND_VERIFIED",
                "cycle_id": cycle_id,
                "quantity_shares": q,
                **realized,
            }
        except Exception as exc:  # noqa: BLE001
            self.state.halt(f"LIVE_EXECUTOR_EXCEPTION:{type(exc).__name__}")
            return {
                "status": "HALTED_EXCEPTION",
                "error": type(exc).__name__,
                "message": str(exc)[:240],
            }
        finally:
            p26.close()
            conn.close()
