"""USDC-denominated market BUY executor for guarded ALL-5m LIVE.

Polymarket's official py-clob-client-v2 market BUY API accepts ``amount`` in USDC.
Directional Edge V2 therefore spends a fixed $1.00 per LIVE order instead of copying
paper share quantity into a limit order. The controller still pre-checks fresh depth
and enforces the existing paper-drift + hard price ceiling before posting an FOK
market BUY. No local share ``min_order_size`` gate is applied to this market-order
path; the authenticated CLOB response remains authoritative.
"""
from __future__ import annotations

import logging
from typing import Any

from p25_live_all5m import (
    All5mLiveController,
    All5mLiveTrigger,
    _field,
    _order_id,
    _sanitize_order_response,
)

log = logging.getLogger("direction_engine.p25.live_all5m_market")

_MARKET_BUY_FLOOR_USDC = 1.00
_FILL_VERIFY_RATIO = 0.90


class All5mMarketBuyController(All5mLiveController):
    """Persistent ALL-5m LIVE controller using USDC market BUY FOK orders."""

    def status(self) -> dict[str, Any]:
        payload = super().status()
        payload.update(
            {
                "order_mode": "MARKET_BUY_FOK_USDC",
                "market_buy_usdc": _MARKET_BUY_FLOOR_USDC,
                "local_share_min_gate": False,
            }
        )
        return payload

    @staticmethod
    def _fresh_market_quote_for_usdc(
        client,
        *,
        token_id: str,
        amount_usdc: float,
        max_live_limit_price: float,
    ) -> tuple[float | None, float, float]:  # noqa: ANN001
        """Return (worst_price, expected_shares, capacity_usdc) under price cap.

        The quote intentionally ignores the book's ``min_order_size`` metadata because
        the actual order is a USDC-denominated MARKET BUY, not a share-sized limit order.
        It only proves that the requested dollar amount can be fully consumed from fresh
        asks without crossing the configured live price ceiling.
        """
        book = client.get_order_book(str(token_id))
        levels: list[tuple[float, float]] = []
        for level in (_field(book, "asks", []) or []):
            try:
                price = float(_field(level, "price"))
                size = max(0.0, float(_field(level, "size")))
            except (TypeError, ValueError):
                continue
            if 0 < price <= float(max_live_limit_price) + 1e-12 and size > 0:
                levels.append((price, size))
        levels.sort(key=lambda item: item[0])

        remaining = max(0.0, float(amount_usdc))
        expected_shares = 0.0
        capacity_usdc = 0.0
        worst_price: float | None = None
        for price, size in levels:
            level_capacity = price * size
            capacity_usdc += level_capacity
            if remaining <= 1e-9:
                continue
            take_usdc = min(remaining, level_capacity)
            if take_usdc > 0:
                expected_shares += take_usdc / price
                remaining -= take_usdc
                worst_price = price

        if remaining > 1e-9:
            return None, expected_shares, capacity_usdc
        return worst_price, expected_shares, capacity_usdc

    @staticmethod
    def _post_market_buy(
        client,
        *,
        token_id: str,
        amount_usdc: float,
        protected_price: float,
    ):  # noqa: ANN001,ANN201
        """Create + post an official SDK MARKET BUY FOK with explicit price protection."""
        from py_clob_client_v2 import MarketOrderArgs, OrderType, Side

        return client.create_and_post_market_order(
            order_args=MarketOrderArgs(
                token_id=str(token_id),
                amount=float(amount_usdc),
                side=Side.BUY,
                order_type=OrderType.FOK,
                price=float(protected_price),
            ),
            order_type=OrderType.FOK,
        )

    def _submit_one(self, trigger: All5mLiveTrigger) -> None:
        reserved = False
        try:
            if not bool(getattr(self.cfg, "p25_live_armed", False)):
                self._last_reason = "DISARMED_BEFORE_PREFLIGHT"
                return
            current_nonce = str(getattr(self.cfg, "p25_live_arm_nonce", "") or "")
            if current_nonce != trigger.session_nonce:
                self._last_reason = "SESSION_CHANGED_BEFORE_SUBMIT"
                return

            try:
                geo = self._geoblock()
            except Exception as exc:  # noqa: BLE001
                if bool(getattr(self.cfg, "p25_live_require_geoblock_clear", True)):
                    self._last_reason = f"GEOBLOCK_CHECK_FAILED_{type(exc).__name__}"
                    return
                geo = {"blocked": False, "country": None, "region": None}
            if geo.get("blocked"):
                self._last_reason = "JURISDICTION_BLOCKED"
                self._halt("JURISDICTION_BLOCKED")
                return

            client = self._client_factory(
                host=str(self.cfg.p25_live_clob_host),
                chain_id=int(self.cfg.p25_live_chain_id),
            )
            drift = max(0.0, float(self.cfg.p25_live_max_price_drift_pct))
            max_live_limit = min(
                float(self.cfg.p25_live_max_limit_price),
                float(trigger.paper_fill_cap) * (1.0 + drift),
            )

            # Directional paper currently uses $1.00. Keep at least $1.00 for the
            # official market-order path while preserving the global LIVE hard cap.
            live_amount_usdc = max(_MARKET_BUY_FLOOR_USDC, float(trigger.paper_stake_usdc))
            max_stake = float(self.cfg.p25_live_max_stake_usdc)
            if live_amount_usdc > max_stake + 1e-9:
                self._last_reason = f"LIVE_NOTIONAL_CAP_{trigger.combo_key}"
                return

            protected_price, expected_shares, _capacity_usdc = self._fresh_market_quote_for_usdc(
                client,
                token_id=trigger.token_id,
                amount_usdc=live_amount_usdc,
                max_live_limit_price=max_live_limit,
            )
            if protected_price is None:
                self._last_reason = f"FRESH_DEPTH_OR_PRICE_MOVED_{trigger.combo_key}"
                return

            collateral = self._collateral_balance(client)
            if collateral + 1e-9 < live_amount_usdc:
                self._last_reason = f"INSUFFICIENT_COLLATERAL_{trigger.combo_key}"
                return
            before = self._conditional_balance(client, trigger.token_id, refresh=True)

            if not bool(getattr(self.cfg, "p25_live_armed", False)):
                self._last_reason = "DISARMED_BEFORE_RESERVE"
                return
            reserved = self.ledger.reserve(
                trigger=trigger,
                live_limit_price=float(protected_price),
                collateral_before_usdc=collateral,
                country=str(geo.get("country")) if geo.get("country") is not None else None,
                region=str(geo.get("region")) if geo.get("region") is not None else None,
            )
            if not reserved:
                self._last_reason = "CONDITION_ALREADY_ATTEMPTED_THIS_SESSION"
                return
            if not bool(getattr(self.cfg, "p25_live_armed", False)):
                self._last_reason = "DISARMED_BEFORE_SUBMIT"
                self.ledger.update(trigger.claim_key, status="DISARMED_BEFORE_SUBMIT")
                return

            raw = self._post_market_buy(
                client,
                token_id=trigger.token_id,
                amount_usdc=live_amount_usdc,
                protected_price=float(protected_price),
            )
            response_json = _sanitize_order_response(raw)
            order_id = _order_id(raw)

            # FOK should be all-or-none. Balance-delta verification still protects
            # against stale/ambiguous responses. Use a conservative 90% lower bound
            # on the fresh-book expected shares so fee/rounding cannot create a false halt.
            min_verified_shares = max(1e-6, expected_shares * _FILL_VERIFY_RATIO)
            delta = self._wait_for_fill_delta(
                client,
                token_id=trigger.token_id,
                before=before,
                requested_shares=min_verified_shares,
            )
            epsilon = max(1e-6, min_verified_shares * 1e-6)
            if delta + epsilon >= min_verified_shares:
                status = "FILLED_VERIFIED"
            elif delta <= epsilon:
                status = "NO_FILL_VERIFIED"
            else:
                status = "EXPOSURE_UNCERTAIN_HALT"

            self._last_reason = f"{status}_{trigger.combo_key}"
            self.ledger.update(
                trigger.claim_key,
                status=status,
                order_id=order_id,
                filled_shares=delta,
                response_json=response_json,
            )
            if status == "EXPOSURE_UNCERTAIN_HALT":
                self._halt(status)
        except Exception as exc:  # noqa: BLE001
            self._last_reason = f"LIVE_ERROR_{type(exc).__name__}_{trigger.combo_key}"
            log.exception("all5m market BUY LIVE error combo=%s", trigger.combo_key)
            if reserved:
                self.ledger.update(
                    trigger.claim_key,
                    status="ERROR_AFTER_RESERVE_HALT",
                    error=f"{type(exc).__name__}: {str(exc)[:240]}",
                )
                self._halt("ERROR_AFTER_RESERVE_HALT")
