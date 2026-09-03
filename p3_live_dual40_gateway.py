"""Authenticated CLOB adapter for DUAL40 post-only resting orders.

Both 40-cent BUY orders are signed first and submitted in one CLOB batch as GTC with
``post_only=True``. The batch is not assumed atomic: both returned order IDs are
required, balances are reconciled separately, and any ambiguous submit result is a
hard-stop condition for the strategy layer.
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from p3_live_gateway import _order_id, _response_list, _sanitize_response
from p3_live_gateway_v2 import RiskAwarePolymarketLiveGateway


def _field_nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return bool(value)
    return bool(value)


def _cancelled_id_set(value: Any) -> set[str]:
    """Normalize the CLOB cancellation acknowledgement into order IDs."""
    if value is None:
        return set()
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, dict):
        # The API normally returns a list, but accepting mapping keys keeps the
        # validator fail-safe across SDK serialization variants.
        return {str(key) for key in value if str(key)}
    if isinstance(value, Iterable):
        order_ids: set[str] = set()
        for item in value:
            if isinstance(item, dict):
                order_id = (
                    item.get("id")
                    or item.get("order_id")
                    or item.get("orderID")
                )
                if order_id:
                    order_ids.add(str(order_id))
            elif item is not None and str(item):
                order_ids.add(str(item))
        return order_ids
    return {str(value)} if str(value) else set()


def _cancel_response_ok(
    value: dict[str, Any],
    *,
    requested_order_ids: Iterable[str] = (),
) -> bool:
    """Accept a cancellation only when the server acknowledgement is conclusive.

    Known order IDs require every requested ID in ``canceled``/``cancelled``.
    Market-token scoped cancellation has no complete requested-ID list, so it is
    accepted only when the response contains a recognized positive acknowledgement
    and no non-empty ``not_canceled``/``notCanceled`` field.
    """
    if not isinstance(value, dict):
        return False
    if _field_nonempty(value.get("error")) or _field_nonempty(
        value.get("errorMsg")
    ):
        return False
    if value.get("success") is False or value.get("ok") is False:
        return False

    not_cancelled_present = False
    for key in ("not_canceled", "notCanceled", "not_cancelled", "notCancelled"):
        if key in value:
            not_cancelled_present = True
            if _field_nonempty(value.get(key)):
                return False

    cancelled_present = False
    cancelled_ids: set[str] = set()
    for key in ("canceled", "cancelled"):
        if key in value:
            cancelled_present = True
            cancelled_ids.update(_cancelled_id_set(value.get(key)))

    requested = {str(order_id) for order_id in requested_order_ids if order_id}
    if requested:
        # A generic success flag is insufficient for known IDs. We need explicit
        # proof that every requested order was actually cancelled.
        return cancelled_present and requested.issubset(cancelled_ids)

    # Token-scoped cancellation may legitimately cancel zero orders. In that case
    # ``canceled=[]`` plus ``not_canceled={}``, or an explicit success flag, is a
    # conclusive acknowledgement. An empty/unknown payload is deliberately rejected.
    return bool(
        cancelled_present
        or not_cancelled_present
        or value.get("success") is True
        or value.get("ok") is True
    )


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
            "ok": not bool(value.get("error") or value.get("errorMsg")),
            "heartbeat_id": str(returned or ""),
            "response": _sanitize_response(value),
            "sent_at_ms": int(time.time() * 1000),
        }

    def cancel_market_pair(
        self,
        *,
        up_token_id: str,
        down_token_id: str,
    ) -> dict[str, Any]:
        """Cancel only orders for the two DUAL40 outcome tokens, never the account."""
        from py_clob_client_v2 import OrderMarketCancelParams  # type: ignore

        responses: list[dict[str, Any]] = []
        ok = True
        for token_id in dict.fromkeys((str(up_token_id), str(down_token_id))):
            try:
                raw = self.clob.cancel_market_orders(
                    OrderMarketCancelParams(asset_id=token_id)
                )
                value = raw if isinstance(raw, dict) else {"value": str(raw)}
                item_ok = _cancel_response_ok(value)
                ok = ok and item_ok
                responses.append(
                    {
                        "token_id": token_id,
                        "ok": item_ok,
                        "response": _sanitize_response(value),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                ok = False
                responses.append(
                    {
                        "token_id": token_id,
                        "ok": False,
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc)[:240],
                        },
                    }
                )
        return {
            "ok": ok,
            "scope": "DUAL40_TOKEN_PAIR",
            "responses": responses,
            "cancelled_at_ms": int(time.time() * 1000),
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
            # The server may have accepted one or both orders before the response was
            # lost. Cancel only the two involved outcome-token scopes immediately;
            # the strategy layer then reconciles balances before classifying the cycle.
            emergency_cancel = self.cancel_market_pair(
                up_token_id=str(up_token_id),
                down_token_id=str(down_token_id),
            )
            return {
                "ok": False,
                "response_uncertain": True,
                "reconciliation_required": True,
                "error_code": "POST_ONLY_BATCH_EXCEPTION",
                "error": {"type": type(exc).__name__, "message": str(exc)[:240]},
                "heartbeat": hb,
                "emergency_cancel_pair": emergency_cancel,
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
        # other was rejected, remove the accepted resting order immediately and force
        # balance reconciliation before the cycle can be declared a no-fill.
        cancel = (
            self.cancel_pair(
                *accepted,
                up_token_id=str(up_token_id),
                down_token_id=str(down_token_id),
            )
            if accepted
            else None
        )
        scoped_fallback = None
        if accepted and not bool((cancel or {}).get("ok")):
            scoped_fallback = self.cancel_market_pair(
                up_token_id=str(up_token_id),
                down_token_id=str(down_token_id),
            )
        result["error_code"] = "POST_ONLY_PAIR_NOT_BOTH_ACCEPTED"
        result["accepted_order_ids"] = accepted
        result["compensating_cancel"] = cancel
        result["emergency_cancel_pair"] = scoped_fallback
        result["reconciliation_required"] = bool(accepted)
        return result

    def cancel_pair(
        self,
        *order_ids: str | None,
        up_token_id: str | None = None,
        down_token_id: str | None = None,
    ) -> dict[str, Any]:
        values = [str(order_id) for order_id in order_ids if order_id]
        if not values:
            if not up_token_id or not down_token_id:
                return {
                    "ok": False,
                    "order_ids": [],
                    "error": {
                        "type": "MissingCancellationScope",
                        "message": "unknown order IDs require both DUAL40 token IDs",
                    },
                    "cancelled_at_ms": int(time.time() * 1000),
                }
            result = self.cancel_market_pair(
                up_token_id=str(up_token_id),
                down_token_id=str(down_token_id),
            )
            result["fallback"] = "CANCEL_DUAL40_TOKEN_PAIR_NO_ORDER_IDS"
            result["order_ids"] = []
            return result
        try:
            raw = self.clob.cancel_orders(values)
            value = raw if isinstance(raw, dict) else {"value": str(raw)}
            return {
                "ok": _cancel_response_ok(
                    value,
                    requested_order_ids=values,
                ),
                "order_ids": values,
                "response": _sanitize_response(value),
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
