"""Guarded XRP 5m directional LIVE pilot triggered by the proven paper cohort."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from p3_live_clients import make_clob_client, parse_clob_balance_usdc, parse_conditional_balance_shares

log = logging.getLogger("direction_engine.p25.live_xrp5m")


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _order_id(payload: Any) -> str | None:
    item = payload[0] if isinstance(payload, list) and payload else payload
    if isinstance(item, dict):
        for key in ("orderID", "orderId", "id"):
            value = item.get(key)
            if value:
                return str(value)
    return None


def _sanitize_order_response(payload: Any) -> str:
    item = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(item, dict):
        return str(item)[:500]
    allowed = {"success", "errorMsg", "error", "orderID", "orderId", "id", "status", "takingAmount", "makingAmount", "matched", "message"}
    safe = {key: item.get(key) for key in allowed if key in item}
    return json.dumps(safe, ensure_ascii=True, sort_keys=True)[:1000]


@dataclass(frozen=True)
class LiveTrigger:
    condition_id: str
    market_id: str
    combo_key: str
    strategy_version: str
    side: str
    token_id: str
    requested_shares: float
    paper_fill_cap: float
    paper_stake_usdc: float


def evaluate_live_trigger_scope(cfg, ref, paper: dict[str, Any] | None) -> tuple[LiveTrigger | None, str]:
    if not bool(getattr(cfg, "p25_live_feature_enabled", False)):
        return None, "LIVE_FEATURE_DISABLED"
    if not bool(getattr(cfg, "p25_live_armed", False)):
        return None, "LIVE_NOT_ARMED"
    arm_nonce = str(getattr(cfg, "p25_live_arm_nonce", "") or "").strip()
    if len(arm_nonce) < 8:
        return None, "LIVE_ARM_NONCE_MISSING"
    if paper is None or str(paper.get("status") or "").upper() != "OPEN":
        return None, "PAPER_OPEN_REQUIRED"
    asset = str(ref.combo.asset.value).upper()
    horizon = str(ref.combo.horizon.value).lower()
    if asset != str(getattr(cfg, "p25_live_asset", "XRP")).upper() or horizon != str(getattr(cfg, "p25_live_horizon", "5m")).lower():
        return None, "OUTSIDE_LIVE_SCOPE"
    strategy = str(paper.get("strategy_version") or "")
    if strategy != str(getattr(cfg, "p25_live_strategy_version", "") or ""):
        return None, "LIVE_STRATEGY_MISMATCH"
    side = str(paper.get("side") or "").upper()
    if side not in {"UP", "DOWN"}:
        return None, "INVALID_PAPER_SIDE"
    try:
        shares = float(paper.get("shares") or 0.0)
        fill_cap = float(paper.get("fill_price") or 0.0)
        stake = float(paper.get("stake_usdc") or 0.0)
    except (TypeError, ValueError):
        return None, "INVALID_PAPER_AMOUNTS"
    if shares <= 0 or not 0 < fill_cap < 1 or stake <= 0:
        return None, "INVALID_PAPER_AMOUNTS"
    if stake > float(getattr(cfg, "p25_live_max_stake_usdc", 1.0)) + 1e-12:
        return None, "LIVE_STAKE_CAP"
    if fill_cap > float(getattr(cfg, "p25_live_max_limit_price", 0.255)) + 1e-12:
        return None, "LIVE_PRICE_CAP"
    token_id = str(ref.up_token_id if side == "UP" else ref.down_token_id)
    if not token_id:
        return None, "TOKEN_ID_MISSING"
    return LiveTrigger(str(ref.condition_id), str(ref.market_id), str(ref.combo.key), strategy, side, token_id, shares, fill_cap, stake), "OK"


class LivePilotLedger:
    def __init__(self, path: str) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS p25_live_direction_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    arm_nonce TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    condition_id TEXT NOT NULL,
                    market_id TEXT,
                    combo_key TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    side TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    requested_shares REAL NOT NULL,
                    paper_fill_cap REAL NOT NULL,
                    live_limit_price REAL,
                    paper_stake_usdc REAL NOT NULL,
                    status TEXT NOT NULL,
                    order_id TEXT,
                    filled_shares REAL,
                    collateral_before_usdc REAL,
                    country TEXT,
                    region TEXT,
                    response_json TEXT,
                    error TEXT
                )
            """)
            conn.commit()

    def consumed(self, arm_nonce: str) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM p25_live_direction_events WHERE arm_nonce=? LIMIT 1", (str(arm_nonce),)).fetchone() is not None

    def reserve(self, *, arm_nonce: str, trigger: LiveTrigger, live_limit_price: float, collateral_before_usdc: float, country: str | None, region: str | None) -> bool:
        now = time.time()
        try:
            with self._connect() as conn:
                conn.execute("""
                    INSERT INTO p25_live_direction_events (
                        arm_nonce,created_at,updated_at,condition_id,market_id,combo_key,
                        strategy_version,side,token_id,requested_shares,paper_fill_cap,
                        live_limit_price,paper_stake_usdc,status,collateral_before_usdc,
                        country,region
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (str(arm_nonce), now, now, trigger.condition_id, trigger.market_id, trigger.combo_key, trigger.strategy_version, trigger.side, trigger.token_id, trigger.requested_shares, trigger.paper_fill_cap, float(live_limit_price), trigger.paper_stake_usdc, "RESERVED", float(collateral_before_usdc), country, region))
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update(self, arm_nonce: str, *, status: str, order_id: str | None = None, filled_shares: float | None = None, response_json: str | None = None, error: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute("""
                UPDATE p25_live_direction_events
                SET updated_at=?, status=?, order_id=COALESCE(?,order_id),
                    filled_shares=COALESCE(?,filled_shares), response_json=COALESCE(?,response_json),
                    error=COALESCE(?,error)
                WHERE arm_nonce=?
            """, (time.time(), str(status), order_id, filled_shares, response_json, error, str(arm_nonce)))
            conn.commit()

    def latest(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM p25_live_direction_events ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row is not None else None


class XRP5mLivePilot:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.ledger = LivePilotLedger(str(cfg.p25_live_ledger_path))
        self._worker_lock = threading.Lock()
        self._worker_active = False
        self._last_reason = "IDLE"

    def status(self) -> dict[str, Any]:
        nonce = str(getattr(self.cfg, "p25_live_arm_nonce", "") or "").strip()
        return {"feature_enabled": bool(self.cfg.p25_live_feature_enabled), "armed": bool(self.cfg.p25_live_armed), "scope": f"{self.cfg.p25_live_asset}:{self.cfg.p25_live_horizon}", "strategy_version": str(self.cfg.p25_live_strategy_version), "max_stake_usdc": float(self.cfg.p25_live_max_stake_usdc), "one_cycle_per_arm": True, "arm_nonce_configured": bool(nonce), "arm_consumed": self.ledger.consumed(nonce) if nonce else False, "worker_active": bool(self._worker_active), "last_reason": self._last_reason, "latest_event": self.ledger.latest()}

    def submit_async(self, ref, paper: dict[str, Any] | None) -> bool:
        trigger, reason = evaluate_live_trigger_scope(self.cfg, ref, paper)
        self._last_reason = reason
        if trigger is None:
            return False
        nonce = str(self.cfg.p25_live_arm_nonce).strip()
        if self.ledger.consumed(nonce):
            self._last_reason = "ARM_ALREADY_CONSUMED"
            return False
        with self._worker_lock:
            if self._worker_active:
                self._last_reason = "LIVE_WORKER_BUSY"
                return False
            self._worker_active = True
        threading.Thread(target=self._submit_worker, args=(trigger,), name="p25-xrp5m-live-pilot", daemon=True).start()
        return True

    def _geoblock(self) -> dict[str, Any]:
        req = urllib.request.Request(str(self.cfg.p25_live_geoblock_url), headers={"User-Agent": "WhaleSignal-P25-XRP5m-LivePilot/1.0"})
        with urllib.request.urlopen(req, timeout=5.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("geoblock response is not an object")
        return {"blocked": bool(payload.get("blocked")), "country": payload.get("country"), "region": payload.get("region")}

    def _conditional_balance(self, client, token_id: str, *, refresh: bool) -> float:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams
        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=str(token_id))
        if refresh:
            try:
                client.update_balance_allowance(params)
            except Exception:
                pass
        return parse_conditional_balance_shares(client.get_balance_allowance(params))

    def _collateral_balance(self, client) -> float:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams
        return parse_clob_balance_usdc(client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)))

    @staticmethod
    def _fresh_limit_for_quantity(client, *, token_id: str, requested_shares: float, paper_fill_cap: float) -> tuple[float | None, float, float]:
        book = client.get_order_book(str(token_id))
        levels = []
        for level in (_field(book, "asks", []) or []):
            try:
                price = float(_field(level, "price")); size = max(0.0, float(_field(level, "size")))
            except (TypeError, ValueError):
                continue
            if 0 < price <= float(paper_fill_cap) + 1e-12 and size > 0:
                levels.append((price, size))
        levels.sort(key=lambda item: item[0])
        cumulative = 0.0; limit = None
        for price, size in levels:
            cumulative += size
            if cumulative + 1e-9 >= float(requested_shares):
                limit = price; break
        try:
            min_size = float(_field(book, "min_order_size", 0.0) or 0.0)
        except (TypeError, ValueError):
            min_size = 0.0
        return limit, cumulative, min_size

    def _wait_for_fill_delta(self, client, *, token_id: str, before: float, requested_shares: float) -> float:
        deadline = time.monotonic() + float(self.cfg.p25_live_settlement_wait_sec)
        last = float(before)
        while time.monotonic() <= deadline:
            last = self._conditional_balance(client, token_id, refresh=True)
            delta = max(0.0, last - float(before))
            if delta + max(1e-6, requested_shares * 1e-6) >= requested_shares:
                return delta
            time.sleep(float(self.cfg.p25_live_settlement_poll_sec))
        return max(0.0, last - float(before))

    def _submit_worker(self, trigger: LiveTrigger) -> None:
        nonce = str(self.cfg.p25_live_arm_nonce).strip(); reserved = False
        try:
            try:
                geo = self._geoblock()
            except Exception as exc:
                if bool(self.cfg.p25_live_require_geoblock_clear):
                    self._last_reason = f"GEOBLOCK_CHECK_FAILED_{type(exc).__name__}"; return
                geo = {"blocked": False, "country": None, "region": None}
            if geo.get("blocked"):
                self._last_reason = "JURISDICTION_BLOCKED"; return
            client = make_clob_client(host=str(self.cfg.p25_live_clob_host), chain_id=int(self.cfg.p25_live_chain_id))
            collateral = self._collateral_balance(client)
            if collateral + 1e-9 < trigger.paper_stake_usdc:
                self._last_reason = "INSUFFICIENT_COLLATERAL"; return
            limit, _capacity, min_size = self._fresh_limit_for_quantity(client, token_id=trigger.token_id, requested_shares=trigger.requested_shares, paper_fill_cap=trigger.paper_fill_cap)
            if limit is None:
                self._last_reason = "FRESH_DEPTH_OR_PRICE_MOVED"; return
            if trigger.requested_shares + 1e-9 < min_size:
                self._last_reason = "BELOW_MIN_ORDER_SIZE"; return
            if trigger.requested_shares * float(limit) > float(self.cfg.p25_live_max_stake_usdc) + 1e-9:
                self._last_reason = "LIVE_NOTIONAL_CAP"; return
            before = self._conditional_balance(client, trigger.token_id, refresh=True)
            reserved = self.ledger.reserve(arm_nonce=nonce, trigger=trigger, live_limit_price=float(limit), collateral_before_usdc=collateral, country=str(geo.get("country")) if geo.get("country") is not None else None, region=str(geo.get("region")) if geo.get("region") is not None else None)
            if not reserved:
                self._last_reason = "ARM_ALREADY_CONSUMED"; return
            from py_clob_client_v2 import OrderArgs, OrderType, PostOrdersV2Args, Side
            signed = client.create_order(OrderArgs(token_id=str(trigger.token_id), price=float(limit), side=Side.BUY, size=float(trigger.requested_shares)))
            raw = client.post_orders([PostOrdersV2Args(order=signed, orderType=OrderType.FOK)])
            response_json = _sanitize_order_response(raw); order_id = _order_id(raw)
            delta = self._wait_for_fill_delta(client, token_id=trigger.token_id, before=before, requested_shares=trigger.requested_shares)
            epsilon = max(1e-6, trigger.requested_shares * 1e-6)
            if delta + epsilon >= trigger.requested_shares:
                status = "FILLED_VERIFIED"
            elif delta <= epsilon:
                status = "NO_FILL_VERIFIED"
            else:
                status = "EXPOSURE_UNCERTAIN_HALT"
            self._last_reason = status
            self.ledger.update(nonce, status=status, order_id=order_id, filled_shares=delta, response_json=response_json)
        except Exception as exc:
            self._last_reason = f"LIVE_ERROR_{type(exc).__name__}"
            log.exception("XRP5m LIVE pilot error")
            if reserved:
                self.ledger.update(nonce, status="ERROR_AFTER_RESERVE_HALT", error=f"{type(exc).__name__}: {str(exc)[:240]}")
        finally:
            with self._worker_lock:
                self._worker_active = False
