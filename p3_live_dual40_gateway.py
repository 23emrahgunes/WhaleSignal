"""Authenticated CLOB adapter for DUAL40 post-only resting orders.

Both 40-cent BUY orders are signed first and submitted in one CLOB batch as GTC with
``post_only=True``. The batch is not assumed atomic: both returned order IDs are
required, balances are reconciled separately, and any ambiguous submit result is a
hard-stop condition for the strategy layer.
"""
from __future__ import annotations

import time
from typing import Any

from p3_live_gateway import _order_id, _response_list, _sanitize_response
from p3_live_gateway_v2 import RiskAwarePolymarketLiveGateway


class Dual40Gateway(RiskAwarePolymarketLiveGateway):
    def start_heartbeat(self, heartbeat_id: str = "") -> dict[str, Any]:
        raw = self.clob.post_heartbeat(str(heartbeat_id or ""))
        value = raw if isinstance(raw, dict) else {"value": str(raw)}
        returned = (
            value.get("heartbeat_id")
            or value.get("heartbeatId")
            or value.get("id")
            or heartbeat_id
        )
        return {
            "ok": not bool(value.get("error")),
            "heartbeat_id": str(returned or ""),
            "response": _sanitize_response(value),
            "sent_at_ms": int(time.time() * 1000),
        }

    def post_pair_post_only_gtc(
        self,
        *,
        up_token_id: str,
        down_token_id: str,
        quantity_shares: float,
        price: float,
        heartbeat_id: str = "",
    ) -> dict[str, Any]:
        from py_clob_client_v2 import (  # type: ignore
            OrderArgs,
            OrderType,
            PostOrdersV2Args,
            Side,
        )

        q = float(quantity_shares)
        maker_price = float(price)
        if q <= 0:
            raise ValueError("quantity_shares must be positive")
        if not 0 < maker_price < 1:
            raise ValueError("maker price must be in (0,1)")

        hb = self.start_heartbeat(heartbeat_id)
        if not hb.get("ok"):
            return {
                "ok": False,
                "response_uncertain": False,
                "error_code": "HEARTBEAT_START_FAILED",
                "heartbeat": hb,
                "up_order_id": None,
                "down_order_id": None,
            }

        try:
            up_signed = self.clob.create_order(
                OrderArgs(
                    token_id=str(up_token_id),
                    price=maker_price,
                    side=Side.BUY,
                    size=q,
                )
            )
            down_signed = self.clob.create_order(
                OrderArgs(
                    token_id=str(down_token_id),
                    price=maker_price,
                    side=Side.BUY,
                    size=q,
                )
            )
            raw = self.clob.post_orders(
                [
                    PostOrdersV2Args(order=up_signed, orderType=OrderType.GTC),
                    PostOrdersV2Args(order=down_signed, orderType=OrderType.GTC),
                ],
                post_only=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "response_uncertain": True,
                "error_code": "POST_ONLY_BATCH_EXCEPTION",
                "error": {"type": type(exc).__name__, "message": str(exc)[:240]},
                "heartbeat": hb,
                "up_order_id": None,
                "down_order_id": None,
            }

        items = _response_list(raw)
        while len(items) < 2:
            items.append({})
        up_item, down_item = items[0], items[1]
        up_id = _order_id(up_item)
        down_id = _order_id(down_item)
        accepted = [order_id for order_id in (up_id, down_id) if order_id]

        result = {
            "ok": bool(up_id and down_id),
            "response_uncertain": False,
            "heartbeat": hb,
            "heartbeat_id": hb.get("heartbeat_id"),
            "up": _sanitize_response(up_item),
            "down": _sanitize_response(down_item),
            "up_order_id": up_id,
            "down_order_id": down_id,
            "submitted_at_ms": int(time.time() * 1000),
            "post_only": True,
            "order_type": "GTC",
        }
        if result["ok"]:
            return result

        # Batch posting is parallel, not atomic. If one order was accepted while the
        # other was rejected, remove the accepted resting order immediately.
        cancel = None
        if accepted:
            try:
                cancel = self.clob.cancel_orders(accepted)
            except Exception as exc:  # noqa: BLE001
                cancel = {"error": type(exc).__name__, "message": str(exc)[:200]}
        result["error_code"] = "POST_ONLY_PAIR_NOT_BOTH_ACCEPTED"
        result["accepted_order_ids"] = accepted
        result["compensating_cancel"] = cancel
        return result

    def cancel_pair(self, *order_ids: str | None) -> dict[str, Any]:
        values = [str(order_id) for order_id in order_ids if order_id]
        if not values:
            return {"ok": True, "order_ids": [], "response": {}}
        try:
            raw = self.clob.cancel_orders(values)
            return {
                "ok": True,
                "order_ids": values,
                "response": raw if isinstance(raw, dict) else {"value": str(raw)},
                "cancelled_at_ms": int(time.time() * 1000),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "order_ids": values,
                "error": {"type": type(exc).__name__, "message": str(exc)[:240]},
                "cancelled_at_ms": int(time.time() * 1000),
            }

    def pair_balances(
        self,
        *,
        up_token_id: str,
        down_token_id: str,
        refresh: bool = True,
    ) -> dict[str, float]:
        return {
            "up": float(
                self.conditional_balance_shares(
                    str(up_token_id),
                    refresh=refresh,
                )
            ),
            "down": float(
                self.conditional_balance_shares(
                    str(down_token_id),
                    refresh=refresh,
                )
            ),
        }

    def merge_matched(
        self,
        *,
        condition_id: str,
        up_token_id: str,
        down_token_id: str,
        matched_shares: float,
        before_up: float,
        before_down: float,
        acquired_up: float,
        acquired_down: float,
    ) -> dict[str, Any]:
        """Merge only the matched inventory and verify any directional remainder."""
        matched = max(0.0, float(matched_shares))
        if matched <= 1e-6:
            return {
                "verified": True,
                "skipped": True,
                "matched_shares": 0.0,
            }

        expected_up = float(before_up) + max(
            0.0,
            float(acquired_up) - matched,
        )
        expected_down = float(before_down) + max(
            0.0,
            float(acquired_down) - matched,
        )
        merge = self.merge_positions(
            condition_id=str(condition_id),
            quantity_shares=matched,
        )

        deadline = time.monotonic() + float(
            self.settings.live_settlement_wait_sec
        )
        last = {"up": float("nan"), "down": float("nan")}
        verified = False
        while time.monotonic() <= deadline:
            try:
                last = self.pair_balances(
                    up_token_id=str(up_token_id),
                    down_token_id=str(down_token_id),
                    refresh=True,
                )
                tolerance = max(1e-5, matched * 1e-5)
                if (
                    abs(float(last["up"]) - expected_up) <= tolerance
                    and abs(float(last["down"]) - expected_down) <= tolerance
                ):
                    verified = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(float(self.settings.live_settlement_poll_sec))

        return {
            "verified": bool(merge.get("verified")) and verified,
            "merge": merge,
            "after": last,
            "expected_after": {
                "up": expected_up,
                "down": expected_down,
            },
            "matched_shares": matched,
        }
