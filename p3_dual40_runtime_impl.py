"""Production runtime wrapper for the DUAL40 state machine."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from p3_dual40_analytics import build_dual40_summary
from p3_dual40_capital import required_live_collateral
from p3_dual40_core import matched_pair_pnl
from p3_dual40_engine import Dual40MakerEngine, _book_view, _levels
from p3_dual40_paper import (
    observed_fill_from_visible_depth,
    visible_ask_capacity,
)
from p3_dual40_preflight import run_dual40_preflight
from p3_dual40_store import create_cycle, update_cycle
from p3_schema import open_p26_read_only


log = logging.getLogger("direction_engine.p3.dual40.runtime")


class ProductionDual40MakerEngine(Dual40MakerEngine):
    """Use real collector health, conservative paper fills and fail-closed LIVE I/O."""

    def __init__(self, settings, state, **kwargs):  # noqa: ANN001,ANN003
        kwargs.setdefault("preflight_fn", run_dual40_preflight)
        super().__init__(settings, state, **kwargs)

    @staticmethod
    def _transport_status(p26) -> dict[str, Any]:  # noqa: ANN001
        row = p26.execute(
            "SELECT value FROM p26_meta WHERE key='book_collector_health_json'"
        ).fetchone()
        if row is None:
            return {}
        try:
            raw = json.loads(str(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        heartbeat = int(raw.get("heartbeat_ts_ms") or 0)
        last_message = int(raw.get("last_message_recv_ms") or 0)
        return {
            **raw,
            "connected": bool(raw.get("connected")),
            "last_receive_ms": max(heartbeat, last_message),
        }

    def _latest_book(
        self,
        p26,
        condition_id: str,
        side: str,
    ) -> dict[str, Any] | None:  # noqa: ANN001
        """Return the base book view plus executable ask depth at the maker price."""
        row = p26.execute(
            """
            SELECT * FROM p26_book_snapshots
            WHERE condition_id=? AND side=?
            ORDER BY ts_ms DESC,id DESC LIMIT 1
            """,
            (str(condition_id), str(side).upper()),
        ).fetchone()
        view = _book_view(row)
        if view is None:
            return None
        asks = _levels(row["asks_json"])
        view["visible_ask_capacity_at_maker"] = visible_ask_capacity(
            asks,
            max_price=self.policy.price,
        )
        return view

    def _paper_tick(
        self,
        conn,
        p26,
        cycle: dict[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:  # noqa: ANN001
        """Count at most the maximum visible executable depth, never repeated polls."""
        up, down = self._book_for_cycle(p26, cycle)
        if up is None or down is None:
            return {"status": "PAPER_WAIT_BOOK", "cycle_id": cycle["id"]}

        quantity = float(cycle["target_shares"])
        maker_price = float(cycle["maker_price"])
        epsilon = float(self.settings.dual40_fill_epsilon)
        up_filled = float(cycle["up_filled_shares"])
        down_filled = float(cycle["down_filled_shares"])
        near_up = int(cycle["near_touch_up_41"])
        near_down = int(cycle["near_touch_down_41"])

        if up["best_ask"] <= self.policy.near_touch_price + 1e-12:
            near_up = 1
        if down["best_ask"] <= self.policy.near_touch_price + 1e-12:
            near_down = 1

        up_capacity = float(up.get("visible_ask_capacity_at_maker") or 0.0)
        down_capacity = float(down.get("visible_ask_capacity_at_maker") or 0.0)
        up_filled = observed_fill_from_visible_depth(
            previous_filled=up_filled,
            target_shares=quantity,
            visible_capacity=up_capacity,
        )
        down_filled = observed_fill_from_visible_depth(
            previous_filled=down_filled,
            target_shares=quantity,
            visible_capacity=down_capacity,
        )

        matched = min(up_filled, down_filled)
        residual = abs(up_filled - down_filled)
        residual_side = (
            "UP"
            if up_filled > down_filled
            else ("DOWN" if down_filled > up_filled else None)
        )
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
            details_merge={
                "paper_fill_rule": "MAX_VISIBLE_ASK_DEPTH_AT_OR_BELOW_MAKER",
                "last_up_book_id": up["id"],
                "last_down_book_id": down["id"],
                "last_up_best_ask": up["best_ask"],
                "last_down_best_ask": down["best_ask"],
                "last_visible_up_capacity_at_maker": up_capacity,
                "last_visible_down_capacity_at_maker": down_capacity,
                "max_observed_up_fill": up_filled,
                "max_observed_down_fill": down_filled,
            },
        )
        cycle.update(
            {
                "up_filled_shares": up_filled,
                "down_filled_shares": down_filled,
                "matched_shares": matched,
                "residual_side": residual_side,
                "residual_shares": residual,
                "near_touch_up_41": near_up,
                "near_touch_down_41": near_down,
            }
        )

        if up_filled + epsilon >= quantity and down_filled + epsilon >= quantity:
            return self._apply_ladder_and_finalize(
                conn,
                cycle=cycle,
                status="PAPER_MATCHED_FILLED",
                pnl=matched_pair_pnl(price=maker_price, matched_shares=quantity),
                official_result=None,
            )

        tte = (int(cycle["market_end_ts_ms"]) - int(now_ms)) / 1000.0
        if tte <= self.policy.cancel_tte_sec:
            if up_filled <= epsilon and down_filled <= epsilon:
                return self._apply_ladder_and_finalize(
                    conn,
                    cycle=cycle,
                    status="NO_FILL",
                    pnl=0.0,
                    official_result=None,
                )
            update_cycle(
                conn,
                int(cycle["id"]),
                status="WAIT_RESOLUTION",
                orders_cancelled_at_ms=int(now_ms),
                details_merge={"cancel_reason": "CANCEL_TTE_REACHED"},
            )
            return {
                "status": "WAIT_RESOLUTION",
                "cycle_id": cycle["id"],
                "up_filled": up_filled,
                "down_filled": down_filled,
            }

        return {
            "status": "PAPER_RESTING",
            "cycle_id": cycle["id"],
            "up_filled": up_filled,
            "down_filled": down_filled,
            "up_visible_capacity": up_capacity,
            "down_visible_capacity": down_capacity,
            "tte_sec": round(tte, 3),
        }

    def _cancel_and_classify(
        self,
        conn,
        cycle: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:  # noqa: ANN001
        """Retry cancellation/reconciliation until exposure is observed or ruled out."""
        gateway = self._gateway_client()
        cancel = gateway.cancel_pair(
            cycle.get("up_order_id"),
            cycle.get("down_order_id"),
            up_token_id=str(cycle["up_token_id"]),
            down_token_id=str(cycle["down_token_id"]),
        )
        if not cancel.get("ok"):
            code = "DUAL40_CANCEL_RETRY_REQUIRED"
            update_cycle(
                conn,
                int(cycle["id"]),
                status="CANCELLING",
                error_code=code,
                details_merge={
                    "cancel_reason": reason,
                    "cancel": cancel,
                    "retry_required": True,
                },
            )
            self.state.halt(code)
            return {
                "status": "CANCELLING_RETRY_HALT",
                "cycle_id": cycle["id"],
                "cancel": cancel,
            }

        time.sleep(min(1.0, float(self.settings.dual40_balance_poll_sec)))
        try:
            up_delta, down_delta, balances = self._balance_fill(gateway, cycle)
        except Exception as exc:  # noqa: BLE001
            code = "DUAL40_BALANCE_RECONCILIATION_RETRY"
            update_cycle(
                conn,
                int(cycle["id"]),
                status="CANCELLING",
                orders_cancelled_at_ms=int(time.time() * 1000),
                error_code=code,
                details_merge={
                    "cancel_reason": reason,
                    "cancel": cancel,
                    "balance_error": {
                        "type": type(exc).__name__,
                        "message": str(exc)[:240],
                    },
                    "retry_required": True,
                },
            )
            self.state.halt(code)
            return {
                "status": "BALANCE_RECONCILIATION_RETRY_HALT",
                "cycle_id": cycle["id"],
            }

        up_filled = max(float(cycle["up_filled_shares"]), up_delta)
        down_filled = max(float(cycle["down_filled_shares"]), down_delta)
        matched = min(up_filled, down_filled)
        residual = abs(up_filled - down_filled)
        residual_side = (
            "UP"
            if up_filled > down_filled
            else ("DOWN" if down_filled > up_filled else None)
        )
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
                "cancel_reason": reason,
                "cancel": cancel,
                "balances_after_cancel": balances,
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

        merge_hash = cycle.get("merge_tx_hash")
        if matched > float(self.settings.dual40_fill_epsilon):
            try:
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
            except Exception as exc:  # noqa: BLE001
                merged = {
                    "verified": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc)[:240],
                    },
                }
            merge_hash = (merged.get("merge") or {}).get("transaction_hash")
            if not merged.get("verified"):
                # Holding both sides to official resolution has the same gross payout
                # as a merge. Keep the cycle active rather than losing its accounting.
                code = "DUAL40_MATCHED_MERGE_UNCERTAIN_WAIT_RESOLUTION"
                update_cycle(
                    conn,
                    int(cycle["id"]),
                    status="WAIT_RESOLUTION",
                    merge_tx_hash=merge_hash,
                    error_code=code,
                    details_merge={"merge": merged},
                )
                self.state.halt(code)
                return {
                    "status": "WAIT_RESOLUTION_MERGE_UNCERTAIN",
                    "cycle_id": cycle["id"],
                    "up_filled": up_filled,
                    "down_filled": down_filled,
                }
            update_cycle(
                conn,
                int(cycle["id"]),
                merge_tx_hash=merge_hash,
                details_merge={"merge": merged},
            )

        epsilon = float(self.settings.dual40_fill_epsilon)
        if up_filled <= epsilon and down_filled <= epsilon:
            return self._apply_ladder_and_finalize(
                conn,
                cycle=cycle,
                status="NO_FILL",
                pnl=0.0,
                official_result=None,
                merge_tx_hash=merge_hash,
                details={"cancel_reason": reason},
            )
        if residual <= epsilon:
            return self._apply_ladder_and_finalize(
                conn,
                cycle=cycle,
                status="MATCHED_FILLED",
                pnl=matched_pair_pnl(
                    price=float(cycle["maker_price"]),
                    matched_shares=matched,
                ),
                official_result=None,
                merge_tx_hash=merge_hash,
                details={"cancel_reason": reason},
            )

        update_cycle(
            conn,
            int(cycle["id"]),
            status="WAIT_RESOLUTION",
            merge_tx_hash=merge_hash,
            details_merge={"cancel_reason": reason},
        )
        return {
            "status": "WAIT_RESOLUTION",
            "cycle_id": cycle["id"],
            "up_filled": up_filled,
            "down_filled": down_filled,
            "matched": matched,
            "residual_side": residual_side,
            "residual_shares": residual,
        }

    def _open_live(
        self,
        conn,
        candidate: dict[str, Any],
        state_row: dict[str, Any],
    ) -> dict[str, Any]:  # noqa: ANN001
        """Open one LIVE cycle only when the remaining capped path is funded."""
        if not self.state.can_auto_execute() or not self._fresh_preflight():
            return {"status": "LIVE_NOT_READY"}

        snap = self.state.snapshot()
        level = int(state_row["level_index"])
        quantity = float(self.policy.ladder[level])
        gateway = self._gateway_client()
        collateral = float(gateway.collateral_balance_usdc(refresh=True))
        required = required_live_collateral(
            policy=self.policy,
            level_index=level,
            initial_arm_floor_usdc=(
                self.settings.dual40_min_collateral_to_arm_usdc
            ),
        )
        if collateral + 1e-9 < required:
            self.state.halt("DUAL40_INSUFFICIENT_COLLATERAL")
            return {
                "status": "HALTED_INSUFFICIENT_COLLATERAL",
                "collateral_usdc": collateral,
                "required_usdc": required,
                "level_index": level,
                "target_shares": quantity,
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
                "required_remaining_collateral_usdc": required,
                "initial_arm_floor_usdc": float(
                    self.settings.dual40_min_collateral_to_arm_usdc
                ),
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
            reconciliation_required = bool(
                posted.get("reconciliation_required")
                or posted.get("response_uncertain")
                or posted.get("accepted_order_ids")
                or posted.get("up_order_id")
                or posted.get("down_order_id")
            )
            self.state.halt(code)
            if not reconciliation_required:
                update_cycle(
                    conn,
                    cycle_id,
                    status="SUBMIT_FAILED",
                    error_code=code,
                    details_merge={"submit": posted},
                )
                return {
                    "status": "HALTED_SUBMIT_FAILED",
                    "cycle_id": cycle_id,
                    "submit": posted,
                }

            update_cycle(
                conn,
                cycle_id,
                status="CANCELLING",
                up_order_id=posted.get("up_order_id"),
                down_order_id=posted.get("down_order_id"),
                heartbeat_id=posted.get("heartbeat_id"),
                error_code=code,
                details_merge={
                    "submit": posted,
                    "submit_response_uncertain": bool(
                        posted.get("response_uncertain")
                    ),
                    "reconciliation_required": True,
                },
            )
            row = conn.execute(
                "SELECT * FROM p3_dual40_cycles WHERE id=?",
                (int(cycle_id),),
            ).fetchone()
            if row is None:
                return {
                    "status": "HALTED_RECONCILIATION_ROW_MISSING",
                    "cycle_id": cycle_id,
                }
            return self._cancel_and_classify(
                conn,
                dict(row),
                reason="SUBMIT_RESPONSE_UNCERTAIN",
            )

        update_cycle(
            conn,
            cycle_id,
            status="LIVE_RESTING",
            up_order_id=posted.get("up_order_id"),
            down_order_id=posted.get("down_order_id"),
            heartbeat_id=posted.get("heartbeat_id"),
            last_heartbeat_ms=int(time.time() * 1000),
            orders_posted_at_ms=int(
                posted.get("submitted_at_ms") or time.time() * 1000
            ),
            details_merge={"submit": posted},
        )
        log.warning(
            "DUAL40 LIVE POSTED id=%s combo=%s level=%s q=%.3f "
            "required=%.2f collateral=%.2f UP@%.2f DOWN@%.2f",
            cycle_id,
            candidate["combo_key"],
            level,
            quantity,
            required,
            collateral,
            self.policy.price,
            self.policy.price,
        )
        return {
            "status": "LIVE_POSTED",
            "cycle_id": cycle_id,
            "level_index": level,
            "target_shares": quantity,
            "required_usdc": required,
            "collateral_usdc": collateral,
        }

    def _fetch_official_result(self, cycle: dict[str, Any]) -> tuple[str | None, str]:
        p26 = open_p26_read_only(self.settings.p26_db_path)
        try:
            row = p26.execute(
                """
                SELECT official_label,official_result_source,official_resolved_at_ms
                FROM p26_labels WHERE condition_id=?
                """,
                (str(cycle["condition_id"]),),
            ).fetchone()
        finally:
            p26.close()
        if row is not None and row["official_label"] in (0, 1):
            side = "UP" if int(row["official_label"]) == 1 else "DOWN"
            source = str(row["official_result_source"] or "P26_OFFICIAL_LABEL")
            return side, f"P26:{source}"
        return super()._fetch_official_result(cycle)

    def public_status(self) -> dict[str, Any]:
        payload = build_dual40_summary(self.settings.p3_db_path, limit=100)
        live_ladder = ((payload.get("state") or {}).get("LIVE") or {})
        try:
            live_level = int(live_ladder.get("level_index") or 0)
            required_now = required_live_collateral(
                policy=self.policy,
                level_index=live_level,
                initial_arm_floor_usdc=(
                    self.settings.dual40_min_collateral_to_arm_usdc
                ),
            )
        except (TypeError, ValueError):
            live_level = None
            required_now = None

        payload.update(
            {
                "policy": {
                    "price": self.policy.price,
                    "ladder": list(self.policy.ladder),
                    "pair_edge_per_share": self.policy.pair_edge_per_share,
                    "full_ladder_capital_usdc": self.policy.full_ladder_capital,
                    "minimum_live_collateral_usdc": required_now,
                    "initial_live_collateral_floor_usdc": float(
                        self.settings.dual40_min_collateral_to_arm_usdc
                    ),
                    "live_collateral_level_index": live_level,
                    "hard_stop_after_30": True,
                    "one_global_market_only": True,
                    "paper_fill_rule": "MAX_VISIBLE_ASK_DEPTH_AT_OR_BELOW_40",
                    "paper_repeated_snapshot_reuse": False,
                    "near_touch_41_diagnostic_only": True,
                    "entry": "BALANCED_STABLE_TWO_WAY",
                    "market_age_sec": self.policy.min_market_age_sec,
                    "lookback_sec": self.policy.lookback_sec,
                    "confirm_sec": self.policy.confirm_sec,
                    "balanced_mid": [
                        self.policy.balanced_mid_low,
                        self.policy.balanced_mid_high,
                    ],
                    "cancel_tte_sec": self.policy.cancel_tte_sec,
                },
                "runtime": self._last_status,
                "generated_at_ms": int(time.time() * 1000),
            }
        )
        return payload
