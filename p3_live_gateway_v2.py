"""Risk-aware Polymarket gateway for equal-share P3 LIVE v2.

Network-response uncertainty is handled by balance reconciliation. A lost HTTP
response after order submission is not treated as proof that nothing filled.
"""
from __future__ import annotations

import math
import time
from decimal import Decimal, ROUND_CEILING
from typing import Any

from p3_live_gateway import (
    PolymarketLiveGateway,
    _field,
    _order_id,
    _response_list,
    _sanitize_response,
)
from p3_live_sizing import DepthQuote, consume_depth


class RiskAwarePolymarketLiveGateway(PolymarketLiveGateway):
    """Same-snapshot quotes, bounded exits and response-loss reconciliation."""

    @staticmethod
    def _levels(book: Any, name: str) -> list[tuple[float, float]]:
        raw = _field(book, name, []) or []
        out: list[tuple[float, float]] = []
        for level in raw:
            try:
                out.append((float(_field(level, "price")), float(_field(level, "size"))))
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _min_order_size(book: Any) -> float:
        try:
            return max(0.0, float(_field(book, "min_order_size", 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _error_payload(exc: Exception) -> dict[str, Any]:
        return {"type": type(exc).__name__, "message": str(exc)[:200]}

    def fetch_pair_books(self, *, up_token_id: str, down_token_id: str) -> tuple[Any, Any]:
        from py_clob_client_v2 import BookParams  # type: ignore

        raw = self.clob.get_order_books(
            [BookParams(token_id=str(up_token_id)), BookParams(token_id=str(down_token_id))]
        )
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            raise RuntimeError("CLOB pair-book response incomplete")
        return raw[0], raw[1]

    def quote_buy_from_book(
        self,
        book: Any,
        *,
        shares: float,
        max_price: float,
    ) -> DepthQuote:
        return consume_depth(
            self._levels(book, "asks"),
            shares=float(shares),
            buy=True,
            price_limit=float(max_price),
            min_order_size=self._min_order_size(book),
        )

    def quote_sell_from_book(
        self,
        book: Any,
        *,
        shares: float,
        min_price: float | None = None,
    ) -> DepthQuote:
        return consume_depth(
            self._levels(book, "bids"),
            shares=float(shares),
            buy=False,
            price_limit=None if min_price is None else float(min_price),
            min_order_size=self._min_order_size(book),
        )

    def buy_capacity_from_book(self, book: Any, *, max_price: float) -> dict[str, float]:
        levels = self._levels(book, "asks")
        cap = sum(size for price, size in levels if price <= float(max_price) + 1e-12)
        return {
            "capacity_shares": max(0.0, float(cap)),
            "min_order_size": self._min_order_size(book),
        }

    def quote_buy(
        self,
        *,
        token_id: str,
        shares: float,
        max_price: float,
    ) -> DepthQuote:
        return self.quote_buy_from_book(
            self.clob.get_order_book(str(token_id)),
            shares=shares,
            max_price=max_price,
        )

    def quote_sell(
        self,
        *,
        token_id: str,
        shares: float,
        min_price: float | None = None,
    ) -> DepthQuote:
        # Used after exposure exists: a re-quote failure must advance to emergency
        # reduction, not abort the exit chain before it gets there.
        try:
            return self.quote_sell_from_book(
                self.clob.get_order_book(str(token_id)),
                shares=shares,
                min_price=min_price,
            )
        except Exception:  # noqa: BLE001
            return consume_depth(
                [], shares=shares, buy=False, price_limit=min_price, min_order_size=0.0
            )

    def collateral_balance_usdc(self, *, refresh: bool = False) -> float:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams  # type: ignore
        from p3_live_clients import parse_clob_balance_usdc

        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        if refresh:
            try:
                self.clob.update_balance_allowance(params)
            except Exception:
                pass
        payload = self.clob.get_balance_allowance(params)
        return parse_clob_balance_usdc(payload)

    def post_two_leg_fok(self, **kwargs: Any) -> dict[str, Any]:
        """Submit pair; never infer no-fill from a lost/failed HTTP response."""
        try:
            result = super().post_two_leg_fok(**kwargs)
            result["submit_response_uncertain"] = False
            return result
        except Exception as exc:  # noqa: BLE001
            return {
                "up": {},
                "down": {},
                "up_order_id": None,
                "down_order_id": None,
                "submit_response_uncertain": True,
                "submit_error": self._error_payload(exc),
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
        """Poll balances through transient API errors until the settlement deadline."""
        deadline = time.monotonic() + float(self.settings.live_settlement_wait_sec)
        q = float(quantity_shares)
        epsilon = max(1e-6, q * 1e-6)
        last_up = float(before_up)
        last_down = float(before_down)
        errors: list[dict[str, Any]] = []
        successful_reads = 0
        while time.monotonic() <= deadline:
            try:
                last_up = self.conditional_balance_shares(str(up_token_id), refresh=True)
                last_down = self.conditional_balance_shares(str(down_token_id), refresh=True)
                successful_reads += 1
                up_delta = max(0.0, last_up - float(before_up))
                down_delta = max(0.0, last_down - float(before_down))
                up_ok = up_delta + epsilon >= q
                down_ok = down_delta + epsilon >= q
                if up_ok and down_ok:
                    return {
                        "up_verified": True,
                        "down_verified": True,
                        "up_after": last_up,
                        "down_after": last_down,
                        "up_delta": up_delta,
                        "down_delta": down_delta,
                        "successful_reads": successful_reads,
                        "read_errors": errors,
                    }
            except Exception as exc:  # noqa: BLE001
                if len(errors) < 5:
                    errors.append(self._error_payload(exc))
            time.sleep(float(self.settings.live_settlement_poll_sec))
        if successful_reads == 0:
            # Orders may have reached CLOB. Without a single post-submit balance read
            # we cannot classify BOTH/NONE/ONE safely. The executor will halt.
            raise RuntimeError("post-submit conditional balances could not be observed")
        up_delta = max(0.0, last_up - float(before_up))
        down_delta = max(0.0, last_down - float(before_down))
        return {
            "up_verified": up_delta + epsilon >= q,
            "down_verified": down_delta + epsilon >= q,
            "up_after": last_up,
            "down_after": last_down,
            "up_delta": up_delta,
            "down_delta": down_delta,
            "successful_reads": successful_reads,
            "read_errors": errors,
        }

    @staticmethod
    def _ceil_tick(price: float, tick: str | float) -> float:
        p = Decimal(str(max(0.0001, min(0.9999, float(price)))))
        t = Decimal(str(tick))
        steps = (p / t).to_integral_value(rounding=ROUND_CEILING)
        return float(min(Decimal("0.9999"), steps * t))

    def unwind_limit_fok(
        self,
        *,
        token_id: str,
        shares: float,
        min_price: float,
    ) -> dict[str, Any]:
        """Price-bounded full exit; response loss is reconciled by token balance."""
        from py_clob_client_v2 import (  # type: ignore
            OrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )

        try:
            tick = self.clob.get_tick_size(str(token_id))
            price = self._ceil_tick(float(min_price), tick)
            raw = self.clob.create_and_post_order(
                order_args=OrderArgs(
                    token_id=str(token_id),
                    price=price,
                    side=Side.SELL,
                    size=float(shares),
                ),
                options=PartialCreateOrderOptions(tick_size=str(tick)),
                order_type=OrderType.FOK,
            )
            item = _response_list(raw)[0]
            return {
                "response": _sanitize_response(item),
                "order_id": _order_id(item),
                "limit_price": price,
                "kind": "LIMIT_FOK",
                "response_uncertain": False,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "response": {},
                "order_id": None,
                "limit_price": float(min_price),
                "kind": "LIMIT_FOK",
                "response_uncertain": True,
                "error": self._error_payload(exc),
            }

    def emergency_unwind_fak(self, *, token_id: str, shares: float) -> dict[str, Any]:
        """Last-resort reducer: immediately consume available bids, never rest."""
        from py_clob_client_v2 import (  # type: ignore
            MarketOrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )

        try:
            tick = self.clob.get_tick_size(str(token_id))
            raw = self.clob.create_and_post_market_order(
                order_args=MarketOrderArgs(
                    token_id=str(token_id),
                    amount=float(shares),
                    side=Side.SELL,
                    order_type=OrderType.FAK,
                ),
                options=PartialCreateOrderOptions(tick_size=str(tick)),
                order_type=OrderType.FAK,
            )
            item = _response_list(raw)[0]
            return {
                "response": _sanitize_response(item),
                "order_id": _order_id(item),
                "kind": "MARKET_FAK_EMERGENCY",
                "response_uncertain": False,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "response": {},
                "order_id": None,
                "kind": "MARKET_FAK_EMERGENCY",
                "response_uncertain": True,
                "error": self._error_payload(exc),
            }

    def wait_for_unwind(
        self,
        *,
        token_id: str,
        before_entry_balance: float,
        max_residual_shares: float = 1e-5,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + float(self.settings.live_settlement_wait_sec)
        last: float | None = None
        errors: list[dict[str, Any]] = []
        while time.monotonic() <= deadline:
            try:
                last = self.conditional_balance_shares(token_id, refresh=True)
                residual = max(0.0, last - float(before_entry_balance))
                if residual <= float(max_residual_shares):
                    return {
                        "verified": True,
                        "after": last,
                        "residual": residual,
                        "read_errors": errors,
                    }
            except Exception as exc:  # noqa: BLE001
                if len(errors) < 5:
                    errors.append(self._error_payload(exc))
            time.sleep(float(self.settings.live_settlement_poll_sec))
        if last is None:
            return {
                "verified": False,
                "after": None,
                "residual": math.inf,
                "balance_observation_uncertain": True,
                "read_errors": errors,
            }
        return {
            "verified": False,
            "after": last,
            "residual": max(0.0, last - float(before_entry_balance)),
            "read_errors": errors,
        }

    def wait_for_collateral_stable(
        self,
        *,
        timeout_sec: float = 3.0,
        stable_reads: int = 2,
    ) -> float:
        deadline = time.monotonic() + max(0.5, float(timeout_sec))
        previous: float | None = None
        stable = 0
        last = self.collateral_balance_usdc(refresh=True)
        while time.monotonic() <= deadline:
            try:
                last = self.collateral_balance_usdc(refresh=True)
            except Exception:  # noqa: BLE001
                time.sleep(min(0.25, float(self.settings.live_settlement_poll_sec)))
                continue
            if previous is not None and math.isclose(last, previous, abs_tol=1e-6):
                stable += 1
                if stable >= max(1, int(stable_reads)):
                    return last
            else:
                stable = 0
            previous = last
            time.sleep(min(0.25, float(self.settings.live_settlement_poll_sec)))
        return last
