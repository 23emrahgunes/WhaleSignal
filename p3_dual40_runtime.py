"""Production runtime wrapper for the DUAL40 state machine."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from p3_dual40_analytics import build_dual40_summary
from p3_dual40_capital import required_live_collateral
from p3_dual40_engine import Dual40MakerEngine
from p3_dual40_preflight import run_dual40_preflight
from p3_dual40_store import create_cycle, update_cycle
from p3_schema import open_p26_read_only


log = logging.getLogger("direction_engine.p3.dual40.runtime")


class ProductionDual40MakerEngine(Dual40MakerEngine):
    """Use the real P2.6 collector-health contract and local official labels."""

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
            update_cycle(
                conn,
                cycle_id,
                status="SUBMIT_FAILED",
                error_code=code,
                details_merge={"submit": posted},
            )
            self.state.halt(code)
            return {
                "status": "HALTED_SUBMIT_FAILED",
                "cycle_id": cycle_id,
                "submit": posted,
            }

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
                    "paper_fill_rule": "BEST_ASK_LE_40",
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
