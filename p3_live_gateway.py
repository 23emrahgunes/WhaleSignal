"""Polymarket LIVE gateway for P3 BUY+MERGE v1.

Two exact-share limit orders are posted as FOK in one CLOB batch request. The batch
is not assumed atomic: on-chain/conditional balances are verified afterwards. Both
verified legs are merged back to collateral; a verified single leg is unwound with
a FOK market sell and the caller is expected to halt if that unwind fails.
"""
from __future__ import annotations

import time
from typing import Any

from p3_config import P3Settings
from p3_live_clients import (
    make_clob_client,
    make_secure_sdk_client,
    parse_clob_balance_usdc,
    parse_conditional_balance_shares,
)


def _response_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item if isinstance(item, dict) else {"value": str(item)} for item in payload]
    if isinstance(payload, dict):
        for key in ("orders", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"value": str(item)} for item in value]
        return [payload]
    return [{"value": str(payload)}]


def _sanitize_response(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "success", "errorMsg", "error", "orderID", "orderId", "id", "status",
        "takingAmount", "makingAmount", "tradeIDs", "transactionsHashes",
        "transactionHashes", "matched", "message",
    }
    return {key: value for key, value in item.items() if key in allowed}


def _order_id(item: dict[str, Any]) -> str | None:
    for key in ("orderID", "orderId", "id"):
        value = item.get(key)
        if value:
            return str(value)
    return None


class PolymarketLiveGateway:
    def __init__(self, settings: P3Settings) -> None:
        self.settings = settings
        self.clob = make_clob_client(
            host=settings.live_clob_host,
            chain_id=settings.live_chain_id,
        )

    def collateral_balance_usdc(self) -> float:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams  # type: ignore

        payload = self.clob.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        return parse_clob_balance_usdc(payload)

    def conditional_balance_shares(self, token_id: str, *, refresh: bool = True) -> float:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams  # type: ignore

        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=str(token_id))
        if refresh:
            try:
                self.clob.update_balance_allowance(params)
            except Exception:
                # The subsequent authenticated GET is authoritative for this decision.
                pass
        payload = self.clob.get_balance_allowance(params)
        return parse_conditional_balance_shares(payload)

    def post_two_leg_fok(
        self,
        *,
        up_token_id: str,
        down_token_id: str,
        quantity_shares: float,
        up_limit_price: float,
        down_limit_price: float,
    ) -> dict[str, Any]:
        from py_clob_client_v2 import (  # type: ignore
            OrderArgs,
            OrderType,
            PostOrdersV2Args,
            Side,
        )

        q = float(quantity_shares)
        up_signed = self.clob.create_order(
            OrderArgs(
                token_id=str(up_token_id),
                price=float(up_limit_price),
                side=Side.BUY,
                size=q,
            )
        )
        down_signed = self.clob.create_order(
            OrderArgs(
                token_id=str(down_token_id),
                price=float(down_limit_price),
                side=Side.BUY,
                size=q,
            )
        )
        raw = self.clob.post_orders(
            [
                PostOrdersV2Args(order=up_signed, orderType=OrderType.FOK),
                PostOrdersV2Args(order=down_signed, orderType=OrderType.FOK),
            ]
        )
        items = _response_list(raw)
        while len(items) < 2:
            items.append({})
        up, down = items[0], items[1]
        return {
            "up": _sanitize_response(up),
            "down": _sanitize_response(down),
            "up_order_id": _order_id(up),
            "down_order_id": _order_id(down),
        }

    def wait_for_leg_deltas(
        self,
        *,
        up_token_id: str,
        down_token_id: str,
        before_up: float,
        before_down: float,
        quantity_shares: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + float(self.settings.live_settlement_wait_sec)
        q = float(quantity_shares)
        epsilon = max(1e-6, q * 1e-6)
        last_up = float(before_up)
        last_down = float(before_down)
        while time.monotonic() <= deadline:
            last_up = self.conditional_balance_shares(str(up_token_id), refresh=True)
            last_down = self.conditional_balance_shares(str(down_token_id), refresh=True)
            up_delta = max(0.0, last_up - float(before_up))
            down_delta = max(0.0, last_down - float(before_down))
            up_ok = up_delta + epsilon >= q
            down_ok = down_delta + epsilon >= q
            if up_ok or down_ok:
                # If only one is visible, keep polling through the settlement window
                # in case the other leg settles a moment later.
                if up_ok and down_ok:
                    return {
                        "up_verified": True,
                        "down_verified": True,
                        "up_after": last_up,
                        "down_after": last_down,
                        "up_delta": up_delta,
                        "down_delta": down_delta,
                    }
            time.sleep(float(self.settings.live_settlement_poll_sec))
        up_delta = max(0.0, last_up - float(before_up))
        down_delta = max(0.0, last_down - float(before_down))
        return {
            "up_verified": up_delta + epsilon >= q,
            "down_verified": down_delta + epsilon >= q,
            "up_after": last_up,
            "down_after": last_down,
            "up_delta": up_delta,
            "down_delta": down_delta,
        }

    def unwind_fok(self, *, token_id: str, shares: float) -> dict[str, Any]:
        from py_clob_client_v2 import MarketOrderArgs, OrderType, Side  # type: ignore

        raw = self.clob.create_and_post_market_order(
            order_args=MarketOrderArgs(
                token_id=str(token_id),
                amount=float(shares),
                side=Side.SELL,
                order_type=OrderType.FOK,
            ),
            order_type=OrderType.FOK,
        )
        item = _response_list(raw)[0]
        return {"response": _sanitize_response(item), "order_id": _order_id(item)}

    def merge_positions(self, *, condition_id: str, quantity_shares: float) -> str | None:
        client = make_secure_sdk_client()
        try:
            amount = int(round(float(quantity_shares) * 1_000_000))
            handle = client.merge_positions(condition_id=str(condition_id), amount=amount)
            tx_hash = getattr(handle, "transaction_hash", None)
            return str(tx_hash) if tx_hash else None
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
