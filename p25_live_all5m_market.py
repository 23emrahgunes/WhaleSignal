"""USDC-denominated market BUY executor for guarded ALL-5m LIVE.

Polymarket's official py-clob-client-v2 market BUY API accepts ``amount`` in USDC.
Directional Edge V2 spends a fixed $1.00 per LIVE order. The controller pre-checks
fresh depth and enforces the existing paper-drift + hard price ceiling before posting
a FAK market BUY. FAK may fill any available portion immediately and cancels the
remainder; verified partial fills are normal and do not halt the continuous session.

No local share ``min_order_size`` gate or arbitrary dollar-depth floor is applied to
this market-order path. If any positive protected ask liquidity exists, the FAK is
submitted and the authenticated CLOB response plus post-order conditional-token
balance are authoritative. An explicit CLOB 400 saying no orders matched a FAK is a
normal terminal no-fill/partial-fill outcome and does not halt LIVE. Unknown
post-reserve exceptions still halt fail-closed.
"""
from __future__ import annotations

import json
import logging
import re
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
_POSITIVE_DEPTH_EPS_USDC = 1e-9
_FULL_FILL_VERIFY_RATIO = 0.90
_FAK_NO_MATCH_TEXT = "no orders found to match with fak order"
_FAK_TERMINAL_TEXT = "fak orders are partially filled or killed if no match is found"


def _is_authoritative_fak_terminal(exc: Exception) -> bool:
    """Recognize the CLOB's explicit FAK immediate terminal response."""
    text = str(exc).lower()
    return (
        _FAK_NO_MATCH_TEXT in text
        and _FAK_TERMINAL_TEXT in text
        and ("status_code=400" in text or "status=400" in text or "400 bad request" in text)
    )


def _exception_order_id(exc: Exception) -> str | None:
    text = str(exc)
    for key in ("orderID", "orderId"):
        match = re.search(rf"['\"]{key}['\"]\s*:\s*['\"]([^'\"]+)['\"]", text)
        if match:
            return match.group(1)
    return None


def _fak_terminal_response(exc: Exception) -> str:
    return json.dumps(
        {
            "classification": "AUTHORITATIVE_FAK_TERMINAL",
            "exception": type(exc).__name__,
            "orderID": _exception_order_id(exc),
            "message": str(exc)[:700],
        },
        ensure_ascii=True,
        sort_keys=True,
    )[:1000]


class All5mMarketBuyController(All5mLiveController):
    """Persistent ALL-5m LIVE controller using USDC market BUY FAK orders."""

    def status(self) -> dict[str, Any]:
        payload = super().status()
        payload.update(
            {
                "order_mode": "MARKET_BUY_FAK_USDC",
                "market_buy_usdc": _MARKET_BUY_FLOOR_USDC,
                "min_fak_depth_usdc": _POSITIVE_DEPTH_EPS_USDC,
                "positive_depth_only": True,
                "local_share_min_gate": False,
                "partial_fill_ok": True,
                "fak_no_match_is_normal": True,
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
        """Return (worst_price, expected_available_shares, capacity_usdc) under cap."""
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
            if level_capacity <= 0:
                continue
            take_usdc = min(remaining, level_capacity) if remaining > 1e-9 else 0.0
            if take_usdc > 0:
                expected_shares += take_usdc / price
                remaining -= take_usdc
                worst_price = price
            capacity_usdc += min(level_capacity, max(0.0, float(amount_usdc) - capacity_usdc))
            if capacity_usdc + 1e-9 >= float(amount_usdc):
                break

        return worst_price, expected_shares, min(capacity_usdc, float(amount_usdc))

    @staticmethod
    def _post_market_buy(
        client,
        *,
        token_id: str,
        amount_usdc: float,
        protected_price: float,
    ):  # noqa: ANN001,ANN201
        """Create + post an official SDK MARKET BUY FAK with explicit price protection."""
        from py_clob_client_v2 import MarketOrderArgs, OrderType, Side

        return client.create_and_post_market_order(
            order_args=MarketOrderArgs(
                token_id=str(token_id),
                amount=float(amount_usdc),
                side=Side.BUY,
                order_type=OrderType.FAK,
                price=float(protected_price),
            ),
            order_type=OrderType.FAK,
        )

    def _record_authoritative_fak_terminal(
        self,
        *,
        trigger: All5mLiveTrigger,
        client,
        before: float,
        exc: Exception,
    ) -> None:  # noqa: ANN001
        """Reconcile an explicit FAK terminal response without halting normal no-fills."""
        order_id = _exception_order_id(exc)
        response_json = _fak_terminal_response(exc)
        # FAK may legitimately partial-fill before the remainder is killed. Wait for
        # any positive token delta; zero after the normal verification window is no-fill.
        delta = self._wait_for_fill_delta(
            client,
            token_id=trigger.token_id,
            before=before,
            requested_shares=1e-6,
        )
        if delta <= 1e-6:
            status = "NO_FILL_FAK_KILLED"
            filled = 0.0
        else:
            status = "PARTIAL_FILL_VERIFIED"
            filled = float(delta)

        self._last_reason = f"{status}_{trigger.combo_key}"
        self.ledger.update(
            trigger.claim_key,
            status=status,
            order_id=order_id,
            filled_shares=filled,
            response_json=response_json,
        )
        log.info(
            "all5m authoritative FAK terminal combo=%s status=%s shares=%.8f order_id=%s; LIVE continues",
            trigger.combo_key,
            status,
            filled,
            order_id,
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

            live_amount_usdc = max(_MARKET_BUY_FLOOR_USDC, float(trigger.paper_stake_usdc))
            max_stake = float(self.cfg.p25_live_max_stake_usdc)
            if live_amount_usdc > max_stake + 1e-9:
                self._last_reason = f"LIVE_NOTIONAL_CAP_{trigger.combo_key}"
                return

            protected_price, _available_shares, capacity_usdc = self._fresh_market_quote_for_usdc(
                client,
                token_id=trigger.token_id,
                amount_usdc=live_amount_usdc,
                max_live_limit_price=max_live_limit,
            )
            if protected_price is None or capacity_usdc <= _POSITIVE_DEPTH_EPS_USDC:
                self._last_reason = f"FRESH_DEPTH_ZERO_OR_MOVED_{trigger.combo_key}"
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

            try:
                raw = self._post_market_buy(
                    client,
                    token_id=trigger.token_id,
                    amount_usdc=live_amount_usdc,
                    protected_price=float(protected_price),
                )
            except Exception as exc:  # noqa: BLE001
                if _is_authoritative_fak_terminal(exc):
                    self._record_authoritative_fak_terminal(
                        trigger=trigger,
                        client=client,
                        before=before,
                        exc=exc,
                    )
                    return
                raise

            response_json = _sanitize_order_response(raw)
            order_id = _order_id(raw)

            full_fill_min_shares = max(
                1e-6,
                (live_amount_usdc / float(protected_price)) * _FULL_FILL_VERIFY_RATIO,
            )
            delta = self._wait_for_fill_delta(
                client,
                token_id=trigger.token_id,
                before=before,
                requested_shares=full_fill_min_shares,
            )
            epsilon = max(1e-6, full_fill_min_shares * 1e-6)
            if delta + epsilon >= full_fill_min_shares:
                status = "FILLED_VERIFIED"
            elif delta <= epsilon:
                status = "NO_FILL_VERIFIED"
            else:
                status = "PARTIAL_FILL_VERIFIED"

            self._last_reason = f"{status}_{trigger.combo_key}"
            self.ledger.update(
                trigger.claim_key,
                status=status,
                order_id=order_id,
                filled_shares=delta,
                response_json=response_json,
            )
            if status == "PARTIAL_FILL_VERIFIED":
                log.info(
                    "all5m FAK partial fill verified combo=%s shares=%.8f; LIVE continues",
                    trigger.combo_key,
                    delta,
                )
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
