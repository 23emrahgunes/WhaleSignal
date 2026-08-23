"""Fail-closed preflight for P3 LIVE arming.

The connectivity probe never posts an order. Full arming checks jurisdiction,
credentials, authenticated CLOB access, collateral/allowance, STRICT readiness and
the configured equal-share/single-leg risk envelope.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable

from p3_config import P3Settings
from p3_dry_run import build_dry_summary
from p3_live_clients import (
    parse_clob_balance_usdc,
    probe_clob_account,
    read_live_secrets,
)
from p3_schema import connect_p3, ensure_p3_schema


def _geoblock(settings: P3Settings, *, opener: Callable | None = None) -> dict[str, Any]:
    open_fn = opener or urllib.request.urlopen
    req = urllib.request.Request(
        settings.live_geoblock_url,
        headers={"User-Agent": "WhaleSignal-P3-LivePreflight/2.0"},
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

    secrets = secret_reader()
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
    credential_gate_clear = (
        secrets.has_private_key
        and "JURISDICTION_BLOCKED" not in reasons
        and "FUNDER_REQUIRED_FOR_SIGNATURE_TYPE" not in reasons
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
    try:
        conn = connect_p3(settings.p3_db_path)
        ensure_p3_schema(conn)
        try:
            dry = dry_summary_builder(conn, settings)
        finally:
            conn.close()
        dry_status = str((dry.get("readiness") or {}).get("status") or "UNKNOWN")
        checks["strict_dry"] = {
            "status": dry_status,
            "attempts": int(dry.get("attempts_executed") or 0),
            "pnl_usdc": float(dry.get("cumulative_pnl_usdc") or 0.0),
            "pair_completion_rate": float(dry.get("pair_completion_rate") or 0.0),
            "one_leg_rate": float(dry.get("one_leg_rate") or 0.0),
        }
    except Exception as exc:  # noqa: BLE001
        checks["strict_dry"] = {"status": "ERROR", "error": type(exc).__name__}
        if for_arming:
            reasons.append("STRICT_DRY_CHECK_FAILED")

    checks["risk_config"] = {
        "sizing_mode": "EQUAL_SHARES_FRESH_DEPTH",
        "target_quantity_shares": float(settings.live_target_quantity_shares),
        "max_quantity_shares": float(settings.live_max_quantity_shares),
        "legacy_capital_scaling_enabled": False,
        "max_single_leg_notional_usdc": float(settings.live_max_single_leg_notional_usdc),
        "max_projected_unwind_loss_usdc": float(settings.live_max_projected_unwind_loss_usdc),
        "emergency_unwind_loss_usdc": float(settings.live_emergency_unwind_loss_usdc),
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
        if settings.live_require_dry_validated and dry_status != "DRY_VALIDATED":
            reasons.append("STRICT_DRY_NOT_VALIDATED")
        if collateral_usdc is None:
            reasons.append("COLLATERAL_BALANCE_UNKNOWN")
        elif collateral_usdc + 1e-9 < float(settings.live_min_collateral_to_arm_usdc):
            reasons.append("INSUFFICIENT_COLLATERAL")
        if allowance_ready is False:
            reasons.append("TRADING_ALLOWANCE_NOT_READY")

    # Connectivity-only may pass with zero collateral. It proves geo/auth only and
    # intentionally never submits an order.
    hard_probe_reasons = {
        "JURISDICTION_BLOCKED",
        "GEOBLOCK_CHECK_FAILED",
        "PRIVATE_KEY_MISSING",
        "FUNDER_REQUIRED_FOR_SIGNATURE_TYPE",
        "CLOB_AUTH_OR_BALANCE_CHECK_FAILED",
    }
    ok = not reasons if for_arming else not any(
        reason in hard_probe_reasons for reason in reasons
    )
    return {
        "ok": bool(ok),
        "purpose": "ARM_LIVE" if for_arming else "CONNECTIVITY_ONLY_NO_ORDER",
        "checked_at_ms": checked_at,
        "reasons": reasons,
        "warnings": warnings,
        "checks": checks,
        "risk": {
            "buy_merge_only": bool(settings.live_buy_merge_only),
            "sizing_mode": "EQUAL_SHARES_FRESH_DEPTH",
            "target_quantity_shares": float(settings.live_target_quantity_shares),
            "max_quantity_shares": float(settings.live_max_quantity_shares),
            "min_net_profit_usdc": float(settings.live_min_net_profit_usdc),
            "min_net_roi": float(settings.live_min_net_roi),
            "max_single_leg_notional_usdc": float(settings.live_max_single_leg_notional_usdc),
            "max_projected_unwind_loss_usdc": float(
                settings.live_max_projected_unwind_loss_usdc
            ),
            "min_edge_to_unwind_loss_ratio": float(
                settings.live_min_edge_to_unwind_loss_ratio
            ),
            "rolling_24h_gross_loss_limit_usdc": float(
                settings.live_rolling_24h_gross_loss_limit_usdc
            ),
            "require_dry_validated": bool(settings.live_require_dry_validated),
            "auto_execute_enabled": bool(settings.live_auto_execute_enabled),
        },
    }
