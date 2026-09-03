"""Fail-closed preflight for P3 LIVE arming.

Connectivity probes never post an order. Structural BUY+MERGE keeps its historical
STRICT DRY gate. DUAL40 verifies the persistent ladder hard-stop, absence of an
active cycle, live P2.6 books, maker-zero-fee lineage and enough collateral to
complete the capped 5 -> 10 -> 30 ladder.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable

from p3_config import P3Settings
from p3_dry_run import build_dry_summary
from p3_dual40_capital import required_live_collateral
from p3_dual40_core import Dual40Policy
from p3_dual40_store import active_cycle, connect_dual40, ladder_state
from p3_live_clients import (
    parse_clob_balance_usdc,
    probe_clob_account,
    read_live_secrets,
)
from p3_schema import connect_p3, ensure_p3_schema, open_p26_read_only


def _geoblock(
    settings: P3Settings,
    *,
    opener: Callable | None = None,
) -> dict[str, Any]:
    open_fn = opener or urllib.request.urlopen
    req = urllib.request.Request(
        settings.live_geoblock_url,
        headers={"User-Agent": "WhaleSignal-P3-LivePreflight/3.0"},
    )
    with open_fn(req, timeout=5.0) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("geoblock response is not a JSON object")
    return {
        "blocked": bool(payload.get("blocked")),
        "country": payload.get("country"),
        "region": payload.get("region"),
    }


def _allowance_ready(payload: Any) -> bool | None:
    if not isinstance(payload, dict):
        return None
    allowances = payload.get("allowances")
    if not isinstance(allowances, dict) or not allowances:
        return None
    positives: list[bool] = []
    for value in allowances.values():
        try:
            positives.append(float(value) > 0)
        except (TypeError, ValueError):
            continue
    return any(positives) if positives else None


def _dual40_runtime_check(settings: P3Settings) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    conn = connect_dual40(settings.p3_db_path)
    p26 = open_p26_read_only(settings.p26_db_path)
    try:
        live_ladder = ladder_state(conn, "LIVE")
        current = active_cycle(conn)
        health_row = p26.execute(
            "SELECT value FROM p26_meta WHERE key='book_collector_health_json'"
        ).fetchone()
        try:
            health = json.loads(str(health_row[0])) if health_row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            health = {}
        heartbeat = int((health or {}).get("heartbeat_ts_ms") or 0)
        last_message = int((health or {}).get("last_message_recv_ms") or 0)
        heartbeat_age = max(0, now_ms - heartbeat) if heartbeat else None
        message_age = max(0, now_ms - last_message) if last_message else None
        transport_ok = bool(
            (health or {}).get("connected")
            and heartbeat_age is not None
            and heartbeat_age
            <= max(5000, int(settings.dual40_book_fresh_ms) * 3)
        )

        assets = settings.dual40_assets()
        placeholders = ",".join("?" for _ in assets)
        rows = p26.execute(
            f"""
            SELECT mt.condition_id,mt.combo_key,COUNT(DISTINCT mt.side) AS sides,
                   COUNT(DISTINCT CASE WHEN fs.taker_only=1 THEN mt.side END)
                     AS maker_fee_sides
            FROM p26_market_tokens mt
            LEFT JOIN p26_fee_schedules fs
              ON fs.condition_id=mt.condition_id AND fs.token_id=mt.token_id
            WHERE mt.active=1
              AND mt.market_end_ts_ms>?
              AND substr(mt.combo_key,1,instr(mt.combo_key,':')-1)
                    IN ({placeholders})
              AND mt.combo_key LIKE '%:5m'
            GROUP BY mt.condition_id,mt.combo_key
            HAVING COUNT(DISTINCT mt.side)=2
            """,
            (now_ms, *assets),
        ).fetchall()
        markets = [dict(row) for row in rows]
        maker_ready = sum(
            1
            for row in markets
            if int(row.get("maker_fee_sides") or 0) == 2
        )
        return {
            "ok": bool(
                not live_ladder["hard_stopped"]
                and current is None
                and transport_ok
                and maker_ready >= 1
            ),
            "ladder_state": live_ladder,
            "active_cycle": current,
            "transport": {
                "connected": bool((health or {}).get("connected")),
                "heartbeat_age_ms": heartbeat_age,
                "last_message_age_ms": message_age,
                "ok": transport_ok,
                "raw": health,
            },
            "active_5m_markets": len(markets),
            "maker_zero_fee_markets": maker_ready,
            "markets": markets,
        }
    finally:
        p26.close()
        conn.close()


def run_live_preflight(
    settings: P3Settings,
    *,
    for_arming: bool,
    account_probe: Callable[..., dict[str, Any]] = probe_clob_account,
    geoblock_opener: Callable | None = None,
    secret_reader: Callable[[], Any] = read_live_secrets,
    dry_summary_builder: Callable[[Any, P3Settings], dict[str, Any]] = build_dry_summary,
) -> dict[str, Any]:
    checked_at = int(time.time() * 1000)
    reasons: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    try:
        geo = _geoblock(settings, opener=geoblock_opener)
        checks["geoblock"] = geo
        if geo["blocked"]:
            reasons.append("JURISDICTION_BLOCKED")
    except Exception as exc:  # noqa: BLE001
        checks["geoblock"] = {"error": type(exc).__name__}
        if settings.live_require_geoblock_clear:
            reasons.append("GEOBLOCK_CHECK_FAILED")
        else:
            warnings.append("GEOBLOCK_CHECK_FAILED_BUT_NOT_REQUIRED")

    try:
        secrets = secret_reader()
    except Exception as exc:  # noqa: BLE001
        secrets = None
        checks["credentials"] = {
            "private_key_present": False,
            "wallet_configured": False,
            "funder_configured": False,
            "clob_api_creds_present": False,
            "signature_type": None,
            "error": type(exc).__name__,
        }
        reasons.append("CREDENTIAL_CONFIG_INVALID")

    if secrets is not None:
        signature_type = int(secrets.signature_type)
        funder_configured = bool(secrets.funder)
        checks["credentials"] = {
            "private_key_present": bool(secrets.has_private_key),
            "wallet_configured": bool(secrets.wallet or secrets.funder),
            "funder_configured": funder_configured,
            "clob_api_creds_present": bool(secrets.has_full_clob_creds),
            "signature_type": signature_type,
        }
        if not secrets.has_private_key:
            reasons.append("PRIVATE_KEY_MISSING")
        if signature_type != 0 and not funder_configured:
            reasons.append("FUNDER_REQUIRED_FOR_SIGNATURE_TYPE")

    collateral_usdc: float | None = None
    allowance_ready: bool | None = None
    credential_gate_clear = bool(
        secrets is not None
        and secrets.has_private_key
        and "JURISDICTION_BLOCKED" not in reasons
        and "FUNDER_REQUIRED_FOR_SIGNATURE_TYPE" not in reasons
        and "CREDENTIAL_CONFIG_INVALID" not in reasons
    )
    if credential_gate_clear:
        try:
            account = account_probe(
                host=settings.live_clob_host,
                chain_id=settings.live_chain_id,
            )
            balance_payload = account.get("balance_payload")
            collateral_usdc = parse_clob_balance_usdc(balance_payload)
            allowance_ready = _allowance_ready(balance_payload)
            checks["clob"] = {
                "ok": True,
                "server_ok": account.get("server_ok"),
                "signer": account.get("signer"),
                "collateral_usdc": collateral_usdc,
                "allowance_ready": allowance_ready,
            }
        except Exception as exc:  # noqa: BLE001
            checks["clob"] = {
                "ok": False,
                "error": type(exc).__name__,
                "message": str(exc)[:240],
            }
            reasons.append("CLOB_AUTH_OR_BALANCE_CHECK_FAILED")
    else:
        checks["clob"] = {"ok": False, "skipped": True}

    dry_status = "UNKNOWN"
    dual40_check: dict[str, Any] | None = None
    if settings.dual40_active:
        try:
            dual40_check = _dual40_runtime_check(settings)
            checks["dual40"] = dual40_check
            dry_status = (
                "DUAL40_RUNTIME_READY"
                if dual40_check.get("ok")
                else "DUAL40_RUNTIME_BLOCKED"
            )
        except Exception as exc:  # noqa: BLE001
            dual40_check = {
                "ok": False,
                "error": type(exc).__name__,
                "message": str(exc)[:200],
            }
            checks["dual40"] = dual40_check
            dry_status = "ERROR"
            if for_arming:
                reasons.append("DUAL40_RUNTIME_CHECK_FAILED")
    else:
        try:
            conn = connect_p3(settings.p3_db_path)
            ensure_p3_schema(conn)
            try:
                dry = dry_summary_builder(conn, settings)
            finally:
                conn.close()
            dry_status = str(
                (dry.get("readiness") or {}).get("status") or "UNKNOWN"
            )
            checks["strict_dry"] = {
                "status": dry_status,
                "attempts": int(dry.get("attempts_executed") or 0),
                "pnl_usdc": float(dry.get("cumulative_pnl_usdc") or 0.0),
                "pair_completion_rate": float(
                    dry.get("pair_completion_rate") or 0.0
                ),
                "one_leg_rate": float(dry.get("one_leg_rate") or 0.0),
            }
        except Exception as exc:  # noqa: BLE001
            checks["strict_dry"] = {
                "status": "ERROR",
                "error": type(exc).__name__,
            }
            if for_arming:
                reasons.append("STRICT_DRY_CHECK_FAILED")

    dual40_required_collateral: float | None = None
    dual40_level_index: int | None = None
    if settings.dual40_active:
        policy = Dual40Policy(
            price=settings.dual40_price,
            ladder=settings.dual40_ladder(),
        )
        try:
            ladder = (dual40_check or {}).get("ladder_state") or {}
            dual40_level_index = int(ladder.get("level_index") or 0)
            dual40_required_collateral = required_live_collateral(
                policy=policy,
                level_index=dual40_level_index,
                initial_arm_floor_usdc=(
                    settings.dual40_min_collateral_to_arm_usdc
                ),
            )
            operator_buffer = max(
                0.0,
                float(settings.dual40_min_collateral_to_arm_usdc)
                - float(policy.full_ladder_capital),
            )
            capital_error = None
        except Exception as exc:  # noqa: BLE001
            operator_buffer = None
            capital_error = {
                "type": type(exc).__name__,
                "message": str(exc)[:200],
            }
            if for_arming:
                reasons.append("DUAL40_CAPITAL_PATH_INVALID")

        checks["risk_config"] = {
            "strategy": "DUAL40_MAKER_RECOVERY_V1",
            "order": "POST_ONLY_GTC",
            "price_each_side": settings.dual40_price,
            "ladder": list(settings.dual40_ladder()),
            "current_level_index": dual40_level_index,
            "hard_stop_after_30": True,
            "one_global_market_only": True,
            "full_ladder_capital_usdc": policy.full_ladder_capital,
            "initial_arm_floor_usdc": (
                settings.dual40_min_collateral_to_arm_usdc
            ),
            "required_collateral_now_usdc": dual40_required_collateral,
            "operator_buffer_usdc": operator_buffer,
            "capital_path_error": capital_error,
            "cancel_tte_sec": settings.dual40_cancel_tte_sec,
        }
    else:
        checks["risk_config"] = {
            "sizing_mode": "EQUAL_SHARES_FRESH_DEPTH",
            "target_quantity_shares": float(
                settings.live_target_quantity_shares
            ),
            "max_quantity_shares": float(settings.live_max_quantity_shares),
            "legacy_capital_scaling_enabled": False,
            "max_single_leg_notional_usdc": float(
                settings.live_max_single_leg_notional_usdc
            ),
            "max_projected_unwind_loss_usdc": float(
                settings.live_max_projected_unwind_loss_usdc
            ),
            "emergency_unwind_loss_usdc": float(
                settings.live_emergency_unwind_loss_usdc
            ),
            "halt_after_one_leg": bool(settings.live_halt_after_one_leg),
            "rolling_24h_gross_loss_limit_usdc": float(
                settings.live_rolling_24h_gross_loss_limit_usdc
            ),
        }

    if for_arming:
        if not settings.live_feature_enabled:
            reasons.append("LIVE_FEATURE_DISABLED")
        if not settings.live_auto_execute_enabled:
            warnings.append("LIVE_ARMED_WILL_NOT_AUTO_EXECUTE")
        if settings.dual40_active:
            check = dual40_check or {}
            ladder = check.get("ladder_state") or {}
            if bool(ladder.get("hard_stopped")):
                reasons.append("DUAL40_HARD_STOP_ACTIVE")
            if check.get("active_cycle") is not None:
                reasons.append("DUAL40_ACTIVE_CYCLE_EXISTS")
            if not bool((check.get("transport") or {}).get("ok")):
                reasons.append("DUAL40_BOOK_TRANSPORT_NOT_READY")
            if int(check.get("maker_zero_fee_markets") or 0) < 1:
                reasons.append("DUAL40_NO_MAKER_READY_5M_MARKET")
        elif settings.live_require_dry_validated and dry_status != "DRY_VALIDATED":
            reasons.append("STRICT_DRY_NOT_VALIDATED")

        required_collateral = (
            dual40_required_collateral
            if settings.dual40_active
            else float(settings.live_min_collateral_to_arm_usdc)
        )
        if collateral_usdc is None:
            reasons.append("COLLATERAL_BALANCE_UNKNOWN")
        elif required_collateral is None:
            reasons.append("DUAL40_CAPITAL_PATH_INVALID")
        elif collateral_usdc + 1e-9 < required_collateral:
            reasons.append("INSUFFICIENT_COLLATERAL")
        if allowance_ready is False:
            reasons.append("TRADING_ALLOWANCE_NOT_READY")

    reasons = list(dict.fromkeys(reasons))
    warnings = list(dict.fromkeys(warnings))
    hard_probe_reasons = {
        "JURISDICTION_BLOCKED",
        "GEOBLOCK_CHECK_FAILED",
        "PRIVATE_KEY_MISSING",
        "FUNDER_REQUIRED_FOR_SIGNATURE_TYPE",
        "CREDENTIAL_CONFIG_INVALID",
        "CLOB_AUTH_OR_BALANCE_CHECK_FAILED",
    }
    ok = (
        not reasons
        if for_arming
        else not any(reason in hard_probe_reasons for reason in reasons)
    )
    return {
        "ok": bool(ok),
        "purpose": "ARM_LIVE" if for_arming else "CONNECTIVITY_ONLY_NO_ORDER",
        "strategy_mode": settings.strategy_mode,
        "checked_at_ms": checked_at,
        "reasons": reasons,
        "warnings": warnings,
        "checks": checks,
        "risk": {
            "strategy_mode": settings.strategy_mode,
            "buy_merge_only": (
                bool(settings.live_buy_merge_only)
                if not settings.dual40_active
                else False
            ),
            "post_only": bool(settings.dual40_active),
            "target_quantity_shares": (
                float(settings.dual40_ladder()[0])
                if settings.dual40_active
                else float(settings.live_target_quantity_shares)
            ),
            "max_quantity_shares": (
                float(settings.dual40_ladder()[-1])
                if settings.dual40_active
                else float(settings.live_max_quantity_shares)
            ),
            "required_collateral_now_usdc": (
                dual40_required_collateral
                if settings.dual40_active
                else float(settings.live_min_collateral_to_arm_usdc)
            ),
            "require_dry_validated": bool(
                settings.live_require_dry_validated
                and not settings.dual40_active
            ),
            "auto_execute_enabled": bool(
                settings.live_auto_execute_enabled
            ),
        },
    }
