"""Guarded BTC/ETH/SOL/XRP 5m LIVE controller for Directional Edge V2.

The controller is downstream-only: it never invents a signal and can only consume a
new paper OPEN row from the configured strategy. A no-order DRY probe must pass before
arming. DRY makes real geoblock/auth/account/book requests but never calls post_orders.

A LIVE session stays armed until the operator disarms it. Each market condition can
be claimed at most once per session. Concurrent paper triggers are serialized through
one worker queue so simultaneous 5m signals are not silently dropped. Any ambiguous
post-submit exposure halts the whole session fail-closed.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from p3_live_clients import (
    make_clob_client,
    parse_clob_balance_usdc,
    parse_conditional_balance_shares,
    read_live_secrets,
)

log = logging.getLogger("direction_engine.p25.live_all5m")

_ALLOWED_ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP"})


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
    allowed = {
        "success", "errorMsg", "error", "orderID", "orderId", "id", "status",
        "takingAmount", "makingAmount", "matched", "message",
    }
    safe = {key: item.get(key) for key in allowed if key in item}
    return json.dumps(safe, ensure_ascii=True, sort_keys=True)[:1000]


def _configured_assets(cfg) -> tuple[str, ...]:  # noqa: ANN001
    raw = str(getattr(cfg, "p25_live_assets_csv", "BTC,ETH,SOL,XRP") or "")
    values = []
    for item in raw.split(","):
        asset = item.strip().upper()
        if asset and asset not in values:
            values.append(asset)
    return tuple(values)


@dataclass(frozen=True)
class All5mLiveTrigger:
    session_nonce: str
    condition_id: str
    market_id: str
    combo_key: str
    strategy_version: str
    side: str
    token_id: str
    requested_shares: float
    paper_fill_cap: float
    paper_stake_usdc: float

    @property
    def claim_key(self) -> str:
        return f"{self.session_nonce}:{self.condition_id}"


def evaluate_all5m_trigger_scope(
    cfg,
    ref,
    paper: dict[str, Any] | None,
) -> tuple[All5mLiveTrigger | None, str]:  # noqa: ANN001
    if not bool(getattr(cfg, "p25_live_feature_enabled", False)):
        return None, "LIVE_FEATURE_DISABLED"
    if not bool(getattr(cfg, "p25_live_armed", False)):
        return None, "LIVE_NOT_ARMED"
    nonce = str(getattr(cfg, "p25_live_arm_nonce", "") or "").strip()
    if len(nonce) < 8:
        return None, "LIVE_SESSION_NONCE_MISSING"
    if paper is None or str(paper.get("status") or "").upper() != "OPEN":
        return None, "PAPER_OPEN_REQUIRED"

    asset = str(ref.combo.asset.value).upper()
    horizon = str(ref.combo.horizon.value).lower()
    assets = set(_configured_assets(cfg))
    if asset not in assets or asset not in _ALLOWED_ASSETS or horizon != "5m":
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
    if stake > float(getattr(cfg, "p25_live_max_stake_usdc", 1.10)) + 1e-12:
        return None, "LIVE_STAKE_CAP"
    if fill_cap > float(getattr(cfg, "p25_live_max_limit_price", 0.83)) + 1e-12:
        return None, "LIVE_PRICE_CAP"

    token_id = str(ref.up_token_id if side == "UP" else ref.down_token_id)
    if not token_id:
        return None, "TOKEN_ID_MISSING"
    return (
        All5mLiveTrigger(
            session_nonce=nonce,
            condition_id=str(ref.condition_id),
            market_id=str(ref.market_id),
            combo_key=str(ref.combo.key),
            strategy_version=strategy,
            side=side,
            token_id=token_id,
            requested_shares=shares,
            paper_fill_cap=fill_cap,
            paper_stake_usdc=stake,
        ),
        "OK",
    )


class All5mLiveLedger:
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS p25_live_all5m_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_nonce TEXT NOT NULL,
                    claim_key TEXT NOT NULL UNIQUE,
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
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_p25_live_all5m_session "
                "ON p25_live_all5m_events(session_nonce,id)"
            )
            conn.commit()

    def claim_exists(self, claim_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM p25_live_all5m_events WHERE claim_key=? LIMIT 1",
                (str(claim_key),),
            ).fetchone()
        return row is not None

    def reserve(
        self,
        *,
        trigger: All5mLiveTrigger,
        live_limit_price: float,
        collateral_before_usdc: float,
        country: str | None,
        region: str | None,
    ) -> bool:
        now = time.time()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO p25_live_all5m_events (
                        session_nonce,claim_key,created_at,updated_at,condition_id,market_id,
                        combo_key,strategy_version,side,token_id,requested_shares,
                        paper_fill_cap,live_limit_price,paper_stake_usdc,status,
                        collateral_before_usdc,country,region
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        trigger.session_nonce,
                        trigger.claim_key,
                        now,
                        now,
                        trigger.condition_id,
                        trigger.market_id,
                        trigger.combo_key,
                        trigger.strategy_version,
                        trigger.side,
                        trigger.token_id,
                        trigger.requested_shares,
                        trigger.paper_fill_cap,
                        float(live_limit_price),
                        trigger.paper_stake_usdc,
                        "RESERVED",
                        float(collateral_before_usdc),
                        country,
                        region,
                    ),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update(
        self,
        claim_key: str,
        *,
        status: str,
        order_id: str | None = None,
        filled_shares: float | None = None,
        response_json: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE p25_live_all5m_events
                SET updated_at=?,status=?,order_id=COALESCE(?,order_id),
                    filled_shares=COALESCE(?,filled_shares),
                    response_json=COALESCE(?,response_json),error=COALESCE(?,error)
                WHERE claim_key=?
                """,
                (
                    time.time(), str(status), order_id, filled_shares,
                    response_json, error, str(claim_key),
                ),
            )
            conn.commit()

    def latest(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM p25_live_all5m_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def network_cycles(self) -> int:
        with self._connect() as conn:
            return int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM p25_live_all5m_events
                    WHERE order_id IS NOT NULL OR response_json IS NOT NULL
                    """
                ).fetchone()[0]
            )

    def session_attempts(self, session_nonce: str) -> int:
        if not session_nonce:
            return 0
        with self._connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM p25_live_all5m_events WHERE session_nonce=?",
                    (str(session_nonce),),
                ).fetchone()[0]
            )


class All5mLiveController:
    def __init__(
        self,
        cfg,
        *,
        client_factory: Callable[..., Any] = make_clob_client,
        secret_reader: Callable[[], Any] = read_live_secrets,
    ) -> None:
        self.cfg = cfg
        self.ledger = All5mLiveLedger(str(cfg.p25_live_ledger_path))
        self._client_factory = client_factory
        self._secret_reader = secret_reader
        self._lock = threading.RLock()
        self._queue: deque[All5mLiveTrigger] = deque()
        self._queued_claims: set[str] = set()
        self._worker_active = False
        self._current_combo: str | None = None
        self._last_reason = "IDLE"
        self._dry_result: dict[str, Any] | None = None
        self._dry_pass_at: float | None = None
        self._halted = False
        self._halt_reason: str | None = None

    def _dry_ttl_sec(self) -> float:
        return max(30.0, float(getattr(self.cfg, "p25_live_dry_pass_ttl_sec", 600.0)))

    def _dry_age_sec(self) -> float | None:
        if self._dry_pass_at is None:
            return None
        return max(0.0, time.time() - self._dry_pass_at)

    def _dry_ready(self) -> bool:
        age = self._dry_age_sec()
        return bool(
            self._dry_result
            and self._dry_result.get("ok")
            and age is not None
            and age <= self._dry_ttl_sec()
        )

    def status(self) -> dict[str, Any]:
        nonce = str(getattr(self.cfg, "p25_live_arm_nonce", "") or "").strip()
        with self._lock:
            queued = len(self._queue)
            worker = self._worker_active
            current = self._current_combo
            halted = self._halted
            halt_reason = self._halt_reason
        dry_age = self._dry_age_sec()
        return {
            "feature_enabled": bool(getattr(self.cfg, "p25_live_feature_enabled", False)),
            "armed": bool(getattr(self.cfg, "p25_live_armed", False)),
            "halted": halted,
            "halt_reason": halt_reason,
            "scope": "BTC/ETH/SOL/XRP:5m",
            "assets": list(_configured_assets(self.cfg)),
            "horizon": "5m",
            "strategy_version": str(getattr(self.cfg, "p25_live_strategy_version", "")),
            "max_stake_usdc": float(getattr(self.cfg, "p25_live_max_stake_usdc", 1.10)),
            "max_price_drift_pct": float(getattr(self.cfg, "p25_live_max_price_drift_pct", 0.10)),
            "max_limit_price": float(getattr(self.cfg, "p25_live_max_limit_price", 0.83)),
            "min_arm_collateral_usdc": float(
                getattr(self.cfg, "p25_live_min_arm_collateral_usdc", 4.40)
            ),
            "session_nonce_configured": bool(nonce),
            "session_attempts": self.ledger.session_attempts(nonce),
            "continuous_session": True,
            "one_attempt_per_condition": True,
            "worker_active": worker,
            "current_combo": current,
            "queue_length": queued,
            "last_reason": self._last_reason,
            "network_cycles": self.ledger.network_cycles(),
            "latest_event": self.ledger.latest(),
            "dry_ready": self._dry_ready(),
            "dry_age_sec": round(dry_age, 1) if dry_age is not None else None,
            "dry_ttl_sec": self._dry_ttl_sec(),
            "dry_result": self._dry_result,
            "post_orders_called_by_dry": False,
            "auto_redeem": False,
        }

    def _geoblock(self) -> dict[str, Any]:
        req = urllib.request.Request(
            str(self.cfg.p25_live_geoblock_url),
            headers={"User-Agent": "WhaleSignal-P25-All5m-Live/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("geoblock response is not an object")
        return {
            "blocked": bool(payload.get("blocked")),
            "country": payload.get("country"),
            "region": payload.get("region"),
        }

    def _collateral_balance(self, client) -> float:  # noqa: ANN001
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        payload = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        return parse_clob_balance_usdc(payload)

    def _conditional_balance(self, client, token_id: str, *, refresh: bool) -> float:  # noqa: ANN001
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=str(token_id),
        )
        if refresh:
            try:
                client.update_balance_allowance(params)
            except Exception:
                pass
        return parse_conditional_balance_shares(client.get_balance_allowance(params))

    @staticmethod
    def _book_probe(client, token_id: str) -> dict[str, Any]:  # noqa: ANN001
        book = client.get_order_book(str(token_id))
        asks: list[float] = []
        for level in (_field(book, "asks", []) or []):
            try:
                price = float(_field(level, "price"))
            except (TypeError, ValueError):
                continue
            if 0 < price < 1:
                asks.append(price)
        try:
            min_size = float(_field(book, "min_order_size", 0.0) or 0.0)
        except (TypeError, ValueError):
            min_size = 0.0
        return {
            "request_ok": True,
            "has_ask": bool(asks),
            "best_ask": round(min(asks), 6) if asks else None,
            "min_order_size": min_size,
        }

    def dry_probe(self, refs: Iterable[Any]) -> dict[str, Any]:
        """Make real geo/auth/account/book requests; never create/post an order."""
        if bool(getattr(self.cfg, "p25_live_armed", False)):
            return {"ok": False, "reason": "DISARM_BEFORE_DRY", "status": self.status()}
        if self._halted:
            return {"ok": False, "reason": "LIVE_HALTED_RESTART_REQUIRED", "status": self.status()}

        reasons: list[str] = []
        checks: dict[str, Any] = {}
        checked_at = time.time()
        expected_assets = tuple(_configured_assets(self.cfg))
        if set(expected_assets) != set(_ALLOWED_ASSETS):
            reasons.append("ALL5M_ASSET_SCOPE_MUST_BE_BTC_ETH_SOL_XRP")
        if str(getattr(self.cfg, "p25_live_horizon", "5m")).lower() != "5m":
            reasons.append("LIVE_HORIZON_NOT_5M")
        if str(getattr(self.cfg, "p25_live_strategy_version", "")) != str(
            getattr(self.cfg, "paper_strategy_version", "")
        ):
            reasons.append("LIVE_STRATEGY_MISMATCH")

        try:
            geo = self._geoblock()
            checks["geoblock"] = geo
            if geo.get("blocked"):
                reasons.append("JURISDICTION_BLOCKED")
        except Exception as exc:  # noqa: BLE001
            checks["geoblock"] = {"ok": False, "error": type(exc).__name__}
            if bool(getattr(self.cfg, "p25_live_require_geoblock_clear", True)):
                reasons.append("GEOBLOCK_CHECK_FAILED")

        try:
            live_secrets = self._secret_reader()
            checks["credentials"] = {
                "private_key_present": bool(live_secrets.has_private_key),
                "funder_configured": bool(live_secrets.funder),
                "signature_type": int(live_secrets.signature_type),
                "clob_api_creds_present": bool(live_secrets.has_full_clob_creds),
            }
            if not live_secrets.has_private_key:
                reasons.append("PRIVATE_KEY_MISSING")
            if int(live_secrets.signature_type) != 0 and not live_secrets.funder:
                reasons.append("FUNDER_REQUIRED_FOR_SIGNATURE_TYPE")
        except Exception as exc:  # noqa: BLE001
            checks["credentials"] = {"ok": False, "error": type(exc).__name__}
            reasons.append("CREDENTIAL_CONFIG_INVALID")
            live_secrets = None

        client = None
        collateral: float | None = None
        if not any(
            reason in reasons
            for reason in (
                "JURISDICTION_BLOCKED", "GEOBLOCK_CHECK_FAILED", "PRIVATE_KEY_MISSING",
                "FUNDER_REQUIRED_FOR_SIGNATURE_TYPE", "CREDENTIAL_CONFIG_INVALID",
            )
        ):
            try:
                client = self._client_factory(
                    host=str(self.cfg.p25_live_clob_host),
                    chain_id=int(self.cfg.p25_live_chain_id),
                )
                server_ok = client.get_ok() if hasattr(client, "get_ok") else True
                collateral = self._collateral_balance(client)
                checks["account"] = {
                    "authenticated_request_ok": True,
                    "server_ok": server_ok,
                    "collateral_usdc": round(float(collateral), 6),
                }
                required = float(getattr(self.cfg, "p25_live_min_arm_collateral_usdc", 4.40))
                if collateral + 1e-9 < required:
                    reasons.append("INSUFFICIENT_COLLATERAL_FOR_ALL5M")
            except Exception as exc:  # noqa: BLE001
                checks["account"] = {
                    "authenticated_request_ok": False,
                    "error": type(exc).__name__,
                    "message": str(exc)[:180],
                }
                reasons.append("CLOB_AUTH_OR_BALANCE_CHECK_FAILED")

        active: dict[str, Any] = {}
        for ref in refs:
            try:
                asset = str(ref.combo.asset.value).upper()
                horizon = str(ref.combo.horizon.value).lower()
            except Exception:
                continue
            if asset in _ALLOWED_ASSETS and horizon == "5m" and asset not in active:
                active[asset] = ref

        market_checks: dict[str, Any] = {}
        book_requests = 0
        for asset in expected_assets:
            ref = active.get(asset)
            if ref is None:
                market_checks[asset] = {"ok": False, "reason": "ACTIVE_5M_MARKET_MISSING"}
                reasons.append(f"ACTIVE_{asset}_5M_MARKET_MISSING")
                continue
            item: dict[str, Any] = {"ok": True, "combo": f"{asset}:5m", "books": {}}
            if client is None:
                item["ok"] = False
                item["reason"] = "ACCOUNT_PROBE_NOT_READY"
                market_checks[asset] = item
                continue
            for side, token in (("UP", ref.up_token_id), ("DOWN", ref.down_token_id)):
                if not token:
                    item["ok"] = False
                    item["books"][side] = {"request_ok": False, "reason": "TOKEN_MISSING"}
                    reasons.append(f"{asset}_{side}_TOKEN_MISSING")
                    continue
                try:
                    item["books"][side] = self._book_probe(client, str(token))
                    book_requests += 1
                except Exception as exc:  # noqa: BLE001
                    item["ok"] = False
                    item["books"][side] = {
                        "request_ok": False,
                        "error": type(exc).__name__,
                    }
                    reasons.append(f"{asset}_{side}_BOOK_REQUEST_FAILED")
            market_checks[asset] = item
        checks["markets"] = market_checks
        checks["network"] = {
            "authenticated_account_request": bool(
                (checks.get("account") or {}).get("authenticated_request_ok")
            ),
            "book_requests_ok": int(book_requests),
            "book_requests_expected": 8,
            "post_orders_called": False,
        }
        if book_requests != 8:
            reasons.append("ALL8_BOOK_REQUESTS_NOT_CONFIRMED")

        ok = not reasons
        result = {
            "ok": ok,
            "purpose": "ALL5M_DRY_REAL_REQUESTS_NO_ORDER",
            "checked_at": checked_at,
            "reason": "DRY_PASS_NO_ORDER" if ok else (reasons[0] if reasons else "DRY_FAILED"),
            "reasons": reasons,
            "checks": checks,
            "limits": {
                "assets": list(expected_assets),
                "horizon": "5m",
                "max_order_notional_usdc": float(
                    getattr(self.cfg, "p25_live_max_stake_usdc", 1.10)
                ),
                "min_arm_collateral_usdc": float(
                    getattr(self.cfg, "p25_live_min_arm_collateral_usdc", 4.40)
                ),
                "max_limit_price": float(getattr(self.cfg, "p25_live_max_limit_price", 0.83)),
                "max_price_drift_pct": float(
                    getattr(self.cfg, "p25_live_max_price_drift_pct", 0.10)
                ),
            },
        }
        self._dry_result = result
        self._dry_pass_at = checked_at if ok else None
        self._last_reason = str(result["reason"])
        return {**result, "status": self.status()}

    def arm(self) -> dict[str, Any]:
        """Arm a persistent all-5m session only after a recent successful DRY probe."""
        with self._lock:
            if self._worker_active or self._queue:
                return {"ok": False, "reason": "LIVE_WORKER_OR_QUEUE_BUSY", "status": self.status()}
        if self._halted:
            return {"ok": False, "reason": "LIVE_HALTED_RESTART_REQUIRED", "status": self.status()}
        if not self._dry_ready():
            self._last_reason = "RECENT_DRY_PASS_REQUIRED"
            return {"ok": False, "reason": self._last_reason, "status": self.status()}
        if str(getattr(self.cfg, "p25_live_strategy_version", "")) != str(
            getattr(self.cfg, "paper_strategy_version", "")
        ):
            self._last_reason = "LIVE_STRATEGY_MISMATCH"
            return {"ok": False, "reason": self._last_reason, "status": self.status()}
        if set(_configured_assets(self.cfg)) != set(_ALLOWED_ASSETS):
            self._last_reason = "INVALID_ALL5M_SCOPE"
            return {"ok": False, "reason": self._last_reason, "status": self.status()}

        if bool(getattr(self.cfg, "p25_live_armed", False)):
            self._last_reason = "ALREADY_ARMED_ALL5M"
            return {"ok": True, "reason": self._last_reason, "status": self.status()}

        nonce = f"all5m-{int(time.time())}-{secrets.token_hex(5)}"
        self.cfg.p25_live_feature_enabled = True
        self.cfg.p25_live_armed = True
        self.cfg.p25_live_arm_nonce = nonce
        self._last_reason = "ARMED_ALL5M_WAITING_FOR_NEW_PAPER_OPEN"
        return {"ok": True, "reason": self._last_reason, "status": self.status()}

    def disarm(self) -> dict[str, Any]:
        self.cfg.p25_live_armed = False
        with self._lock:
            self._queue.clear()
            self._queued_claims.clear()
            active = self._worker_active
        self._last_reason = (
            "DISARM_REQUESTED_WORKER_ACTIVE" if active else "DISARMED_BY_OPERATOR"
        )
        return {"ok": True, "reason": self._last_reason, "status": self.status()}

    def _halt(self, reason: str) -> None:
        with self._lock:
            self._halted = True
            self._halt_reason = str(reason)
            self._queue.clear()
            self._queued_claims.clear()
        self.cfg.p25_live_armed = False
        self._last_reason = str(reason)

    def submit_async(self, ref, paper: dict[str, Any] | None) -> bool:  # noqa: ANN001
        trigger, reason = evaluate_all5m_trigger_scope(self.cfg, ref, paper)
        self._last_reason = reason
        if trigger is None or self._halted:
            return False
        if self.ledger.claim_exists(trigger.claim_key):
            self._last_reason = "CONDITION_ALREADY_ATTEMPTED_THIS_SESSION"
            return False
        with self._lock:
            if trigger.claim_key in self._queued_claims:
                self._last_reason = "CONDITION_ALREADY_QUEUED"
                return False
            self._queue.append(trigger)
            self._queued_claims.add(trigger.claim_key)
            should_start = not self._worker_active
            if should_start:
                self._worker_active = True
        if should_start:
            threading.Thread(
                target=self._drain_queue,
                name="p25-all5m-live-worker",
                daemon=True,
            ).start()
        self._last_reason = f"QUEUED_{trigger.combo_key}"
        return True

    @staticmethod
    def _fresh_limit_for_quantity(
        client,
        *,
        token_id: str,
        requested_shares: float,
        max_live_limit_price: float,
    ) -> tuple[float | None, float, float]:  # noqa: ANN001
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
        cumulative = 0.0
        limit = None
        for price, size in levels:
            cumulative += size
            if cumulative + 1e-9 >= float(requested_shares):
                limit = price
                break
        try:
            min_size = float(_field(book, "min_order_size", 0.0) or 0.0)
        except (TypeError, ValueError):
            min_size = 0.0
        return limit, cumulative, min_size

    def _wait_for_fill_delta(
        self,
        client,
        *,
        token_id: str,
        before: float,
        requested_shares: float,
    ) -> float:  # noqa: ANN001
        deadline = time.monotonic() + float(self.cfg.p25_live_settlement_wait_sec)
        last = float(before)
        while time.monotonic() <= deadline:
            last = self._conditional_balance(client, token_id, refresh=True)
            delta = max(0.0, last - float(before))
            if delta + max(1e-6, requested_shares * 1e-6) >= requested_shares:
                return delta
            time.sleep(float(self.cfg.p25_live_settlement_poll_sec))
        return max(0.0, last - float(before))

    def _drain_queue(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._halted or not self._queue:
                        self._current_combo = None
                        return
                    trigger = self._queue.popleft()
                    self._queued_claims.discard(trigger.claim_key)
                    self._current_combo = trigger.combo_key
                self._submit_one(trigger)
        finally:
            with self._lock:
                self._current_combo = None
                self._worker_active = False

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
            limit, _capacity, min_size = self._fresh_limit_for_quantity(
                client,
                token_id=trigger.token_id,
                requested_shares=trigger.requested_shares,
                max_live_limit_price=max_live_limit,
            )
            if limit is None:
                self._last_reason = f"FRESH_DEPTH_OR_PRICE_MOVED_{trigger.combo_key}"
                return
            if trigger.requested_shares + 1e-9 < min_size:
                self._last_reason = f"BELOW_MIN_ORDER_SIZE_{trigger.combo_key}"
                return

            live_notional = trigger.requested_shares * float(limit)
            if live_notional > float(self.cfg.p25_live_max_stake_usdc) + 1e-9:
                self._last_reason = f"LIVE_NOTIONAL_CAP_{trigger.combo_key}"
                return
            collateral = self._collateral_balance(client)
            if collateral + 1e-9 < live_notional:
                self._last_reason = f"INSUFFICIENT_COLLATERAL_{trigger.combo_key}"
                return
            before = self._conditional_balance(client, trigger.token_id, refresh=True)

            if not bool(getattr(self.cfg, "p25_live_armed", False)):
                self._last_reason = "DISARMED_BEFORE_RESERVE"
                return
            reserved = self.ledger.reserve(
                trigger=trigger,
                live_limit_price=float(limit),
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

            from py_clob_client_v2 import OrderArgs, OrderType, PostOrdersV2Args, Side

            signed = client.create_order(
                OrderArgs(
                    token_id=str(trigger.token_id),
                    price=float(limit),
                    side=Side.BUY,
                    size=float(trigger.requested_shares),
                )
            )
            raw = client.post_orders(
                [PostOrdersV2Args(order=signed, orderType=OrderType.FOK)]
            )
            response_json = _sanitize_order_response(raw)
            order_id = _order_id(raw)
            delta = self._wait_for_fill_delta(
                client,
                token_id=trigger.token_id,
                before=before,
                requested_shares=trigger.requested_shares,
            )
            epsilon = max(1e-6, trigger.requested_shares * 1e-6)
            if delta + epsilon >= trigger.requested_shares:
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
            log.exception("all5m LIVE error combo=%s", trigger.combo_key)
            if reserved:
                self.ledger.update(
                    trigger.claim_key,
                    status="ERROR_AFTER_RESERVE_HALT",
                    error=f"{type(exc).__name__}: {str(exc)[:240]}",
                )
                self._halt("ERROR_AFTER_RESERVE_HALT")
