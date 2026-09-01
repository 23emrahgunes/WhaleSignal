"""Immediate USDC-denominated FAK executor for guarded ALL-5m LIVE.

The paper signal decides *what* to buy. LIVE execution derives a limit ceiling from
the persisted side probability, not from the stale paper fill price. On every new
paper OPEN, the controller immediately posts a $1 BUY FAK with:

    limit = min(absolute hard cap, selected side probability - minimum live edge)

The ceiling is rounded down to a conservative cent. There is deliberately no live
order-book pre-read in the submit path: the FAK limit itself is the atomic liquidity
and price check at the CLOB matching engine. If asks exist at or below the ceiling,
whatever is immediately available may fill and the remainder is killed; if nothing
matches, the authoritative FAK no-match response is a normal zero-fill outcome.

BTC/ETH/SOL/XRP executions use independent in-flight lanes. A slow fill verification
on one asset therefore cannot serialize and delay the other assets. Each condition is
still claimed at most once per operator session. Unknown post-reserve exceptions
remain fail-closed HALT events.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from typing import Any

from p25_live_all5m import (
    All5mLiveController,
    All5mLiveTrigger,
    _order_id,
    _sanitize_order_response,
    evaluate_all5m_trigger_scope,
)

log = logging.getLogger("direction_engine.p25.live_all5m_market")

_MARKET_BUY_FLOOR_USDC = 1.00
_FULL_FILL_VERIFY_RATIO = 0.90
_FAK_NO_MATCH_TEXT = "no orders found to match with fak order"
_FAK_TERMINAL_TEXT = "fak orders are partially filled or killed if no match is found"
_ALLOWED_FAST_ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP"})


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


def _safe_probability(value: Any) -> float | None:
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or not 0.0 < p < 1.0:
        return None
    return p


def _selected_probability_from_paper(paper: dict[str, Any], side: str) -> float | None:
    """Recover the exact side probability persisted with the paper OPEN row."""
    direct = _safe_probability(paper.get("selected_probability"))
    if direct is not None:
        return direct

    p_up = _safe_probability(paper.get("forecast_p_up"))
    if p_up is not None:
        return p_up if str(side).upper() == "UP" else 1.0 - p_up

    # Compatibility with older rows: edge was persisted from the selected side.
    try:
        fill = float(paper.get("fill_price") or 0.0)
        edge = float(paper.get("forecast_edge") or 0.0)
    except (TypeError, ValueError):
        return None
    return _safe_probability(fill + edge)


def _cent_floor(value: float) -> float:
    """Conservative 1-cent price cap valid for current short crypto markets."""
    return max(0.01, math.floor((float(value) + 1e-12) * 100.0) / 100.0)


class All5mMarketBuyController(All5mLiveController):
    """Persistent ALL-5m LIVE controller using immediate signal-triggered FAKs."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002,ANN003
        super().__init__(*args, **kwargs)
        self._fast_lock = threading.RLock()
        self._fast_inflight: dict[str, str] = {}
        self._asset_clients: dict[str, Any] = {}
        self._geo_cache: dict[str, Any] | None = None
        self._geo_cache_at = 0.0

    def _live_edge_floor(self) -> float:
        return max(0.0, float(getattr(self.cfg, "paper_min_edge", 0.08) or 0.0))

    def status(self) -> dict[str, Any]:
        payload = super().status()
        with self._fast_lock:
            active = dict(self._fast_inflight)
        payload.update(
            {
                "order_mode": "SIGNAL_IMMEDIATE_FAK_LIVE_EDGE_CAP",
                "market_buy_usdc": _MARKET_BUY_FLOOR_USDC,
                "local_share_min_gate": False,
                "partial_fill_ok": True,
                "fak_no_match_is_normal": True,
                "paper_drift_enforced": False,
                "live_min_edge": self._live_edge_floor(),
                "execution_price_mode": "SIGNAL_IMMEDIATE_LIMIT_CAP",
                "pre_submit_book_check": False,
                "matching_engine_is_liquidity_gate": True,
                "parallel_execution": True,
                "max_parallel_workers": 4,
                "parallel_workers": len(active),
                "active_combos": sorted(active.values()),
                "worker_active": bool(active),
                "current_combo": ",".join(sorted(active.values())) if active else None,
                "queue_length": 0,
            }
        )
        return payload

    def dry_probe(self, refs):  # noqa: ANN001,ANN201
        result = super().dry_probe(refs)
        if result.get("ok"):
            geo = ((result.get("checks") or {}).get("geoblock") or {})
            if isinstance(geo, dict) and not geo.get("blocked"):
                self._geo_cache = dict(geo)
                self._geo_cache_at = time.monotonic()
        return result

    def arm(self) -> dict[str, Any]:
        with self._fast_lock:
            if self._fast_inflight:
                return {"ok": False, "reason": "LIVE_FAST_WORKERS_BUSY", "status": self.status()}
        result = super().arm()
        if not result.get("ok") or not bool(getattr(self.cfg, "p25_live_armed", False)):
            return result

        # Warm one authenticated SDK client per asset before any signal arrives.
        try:
            clients = {
                asset: self._client_factory(
                    host=str(self.cfg.p25_live_clob_host),
                    chain_id=int(self.cfg.p25_live_chain_id),
                )
                for asset in sorted(_ALLOWED_FAST_ASSETS)
            }
        except Exception as exc:  # noqa: BLE001
            self.cfg.p25_live_armed = False
            self._last_reason = f"LIVE_CLIENT_WARMUP_FAILED_{type(exc).__name__}"
            return {"ok": False, "reason": self._last_reason, "status": self.status()}
        with self._fast_lock:
            self._asset_clients = clients
        self._last_reason = "ARMED_ALL5M_IMMEDIATE_FAST_LANES_WAITING_FOR_NEW_PAPER_OPEN"
        return {"ok": True, "reason": self._last_reason, "status": self.status()}

    def disarm(self) -> dict[str, Any]:
        self.cfg.p25_live_armed = False
        with self._fast_lock:
            active = bool(self._fast_inflight)
        self._last_reason = (
            "DISARM_REQUESTED_FAST_WORKERS_ACTIVE" if active else "DISARMED_BY_OPERATOR"
        )
        return {"ok": True, "reason": self._last_reason, "status": self.status()}

    def submit_async(self, ref, paper: dict[str, Any] | None) -> bool:  # noqa: ANN001
        trigger, reason = evaluate_all5m_trigger_scope(self.cfg, ref, paper)
        self._last_reason = reason
        if trigger is None or self._halted:
            return False

        selected_probability = _selected_probability_from_paper(paper or {}, trigger.side)
        if selected_probability is None:
            self._last_reason = f"LIVE_SELECTED_PROBABILITY_MISSING_{trigger.combo_key}"
            return False
        if self.ledger.claim_exists(trigger.claim_key):
            self._last_reason = "CONDITION_ALREADY_ATTEMPTED_THIS_SESSION"
            return False

        with self._fast_lock:
            if trigger.claim_key in self._fast_inflight:
                self._last_reason = "CONDITION_ALREADY_INFLIGHT"
                return False
            self._fast_inflight[trigger.claim_key] = trigger.combo_key

        threading.Thread(
            target=self._fast_worker,
            args=(trigger, selected_probability),
            name=f"p25-live-{trigger.combo_key.replace(':', '-').lower()}",
            daemon=True,
        ).start()
        self._last_reason = f"SUBMITTING_NOW_{trigger.combo_key}"
        return True

    def _fast_worker(self, trigger: All5mLiveTrigger, selected_probability: float) -> None:
        try:
            self._submit_one(trigger, selected_probability=selected_probability)
        finally:
            with self._fast_lock:
                self._fast_inflight.pop(trigger.claim_key, None)

    def _client_for_combo(self, combo_key: str):  # noqa: ANN201
        asset = str(combo_key).split(":", 1)[0].upper()
        with self._fast_lock:
            client = self._asset_clients.get(asset)
        if client is not None:
            return client
        client = self._client_factory(
            host=str(self.cfg.p25_live_clob_host),
            chain_id=int(self.cfg.p25_live_chain_id),
        )
        with self._fast_lock:
            self._asset_clients[asset] = client
        return client

    def _live_geoblock(self) -> dict[str, Any]:
        # DRY made the real jurisdiction call. Reuse it briefly so the hot submit
        # path is not delayed by another unrelated HTTP round trip. Once stale,
        # refresh normally; there is no geoblock bypass.
        age = time.monotonic() - self._geo_cache_at
        if self._geo_cache is not None and age <= 300.0:
            return dict(self._geo_cache)
        geo = self._geoblock()
        if not geo.get("blocked"):
            self._geo_cache = dict(geo)
            self._geo_cache_at = time.monotonic()
        return geo

    @staticmethod
    def _post_market_buy(
        client,
        *,
        token_id: str,
        amount_usdc: float,
        protected_price: float,
    ):  # noqa: ANN001,ANN201
        """Immediately post a $USDC BUY FAK with a value-derived limit ceiling."""
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
        order_id = _exception_order_id(exc)
        response_json = _fak_terminal_response(exc)
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

    def _submit_one(
        self,
        trigger: All5mLiveTrigger,
        *,
        selected_probability: float | None = None,
    ) -> None:
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
                geo = self._live_geoblock()
            except Exception as exc:  # noqa: BLE001
                if bool(getattr(self.cfg, "p25_live_require_geoblock_clear", True)):
                    self._last_reason = f"GEOBLOCK_CHECK_FAILED_{type(exc).__name__}"
                    return
                geo = {"blocked": False, "country": None, "region": None}
            if geo.get("blocked"):
                self._last_reason = "JURISDICTION_BLOCKED"
                self._halt("JURISDICTION_BLOCKED")
                return

            probability = _safe_probability(selected_probability)
            if probability is None:
                # Compatibility only for direct deterministic unit calls. Production
                # submit_async always supplies the persisted selected probability.
                probability = min(
                    0.99,
                    float(trigger.paper_fill_cap) + self._live_edge_floor() + 0.10,
                )

            min_edge = self._live_edge_floor()
            raw_edge_cap = probability - min_edge
            hard_cap = float(self.cfg.p25_live_max_limit_price)
            max_live_limit = min(hard_cap, raw_edge_cap)
            if max_live_limit <= 0.01:
                self._last_reason = f"LIVE_EDGE_CAP_INVALID_{trigger.combo_key}"
                return
            order_limit_price = _cent_floor(max_live_limit)

            live_amount_usdc = max(_MARKET_BUY_FLOOR_USDC, float(trigger.paper_stake_usdc))
            max_stake = float(self.cfg.p25_live_max_stake_usdc)
            if live_amount_usdc > max_stake + 1e-9:
                self._last_reason = f"LIVE_NOTIONAL_CAP_{trigger.combo_key}"
                return

            client = self._client_for_combo(trigger.combo_key)

            # Keep only balance/exposure safety reads before submit. There is no
            # order-book pre-read: CLOB atomically applies the FAK price ceiling.
            collateral = self._collateral_balance(client)
            if collateral + 1e-9 < live_amount_usdc:
                self._last_reason = f"INSUFFICIENT_COLLATERAL_{trigger.combo_key}"
                return
            before = self._conditional_balance(client, trigger.token_id, refresh=False)

            if not bool(getattr(self.cfg, "p25_live_armed", False)):
                self._last_reason = "DISARMED_BEFORE_RESERVE"
                return
            reserved = self.ledger.reserve(
                trigger=trigger,
                live_limit_price=float(order_limit_price),
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

            log.info(
                "all5m SIGNAL-IMMEDIATE FAK submit combo=%s side=%s p=%.4f limit=%.4f edge_floor=%.4f usdc=%.2f",
                trigger.combo_key,
                trigger.side,
                probability,
                order_limit_price,
                min_edge,
                live_amount_usdc,
            )
            try:
                raw = self._post_market_buy(
                    client,
                    token_id=trigger.token_id,
                    amount_usdc=live_amount_usdc,
                    protected_price=float(order_limit_price),
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
                (live_amount_usdc / float(order_limit_price)) * _FULL_FILL_VERIFY_RATIO,
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
