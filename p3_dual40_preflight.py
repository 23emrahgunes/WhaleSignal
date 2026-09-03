"""DUAL40-specific preflight wrapper using the real P2.6 health contract."""
from __future__ import annotations

import json
import time
from typing import Any

from p3_config import P3Settings
from p3_dual40_store import active_cycle, connect_dual40, ladder_state
from p3_live_preflight import run_live_preflight as _base_preflight
from p3_schema import open_p26_read_only


_DUAL40_REASON_PREFIXES = (
    "DUAL40_",
)


def _runtime_check(settings: P3Settings) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    conn = connect_dual40(settings.p3_db_path)
    p26 = open_p26_read_only(settings.p26_db_path)
    try:
        state = ladder_state(conn, "LIVE")
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
            and heartbeat_age <= max(5000, int(settings.dual40_book_fresh_ms) * 3)
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
            1 for row in markets if int(row.get("maker_fee_sides") or 0) == 2
        )
        ok = bool(
            not state["hard_stopped"]
            and current is None
            and transport_ok
            and maker_ready >= 1
        )
        return {
            "ok": ok,
            "ladder_state": state,
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


def run_dual40_preflight(
    settings: P3Settings,
    *,
    for_arming: bool,
    **kwargs: Any,
) -> dict[str, Any]:
    """Reuse geo/auth/account checks, then replace the legacy DUAL40 runtime check."""
    result = _base_preflight(settings, for_arming=for_arming, **kwargs)
    if not settings.dual40_active:
        return result

    check = _runtime_check(settings)
    result.setdefault("checks", {})["dual40"] = check

    reasons = [
        str(reason)
        for reason in (result.get("reasons") or [])
        if not any(str(reason).startswith(prefix) for prefix in _DUAL40_REASON_PREFIXES)
    ]
    if for_arming:
        ladder = check.get("ladder_state") or {}
        if bool(ladder.get("hard_stopped")):
            reasons.append("DUAL40_HARD_STOP_ACTIVE")
        if check.get("active_cycle") is not None:
            reasons.append("DUAL40_ACTIVE_CYCLE_EXISTS")
        if not bool((check.get("transport") or {}).get("ok")):
            reasons.append("DUAL40_BOOK_TRANSPORT_NOT_READY")
        if int(check.get("maker_zero_fee_markets") or 0) < 1:
            reasons.append("DUAL40_NO_MAKER_READY_5M_MARKET")

    # Deduplicate while preserving diagnostic order.
    result["reasons"] = list(dict.fromkeys(reasons))
    hard_probe_reasons = {
        "JURISDICTION_BLOCKED",
        "GEOBLOCK_CHECK_FAILED",
        "PRIVATE_KEY_MISSING",
        "FUNDER_REQUIRED_FOR_SIGNATURE_TYPE",
        "CLOB_AUTH_OR_BALANCE_CHECK_FAILED",
    }
    result["ok"] = (
        not result["reasons"]
        if for_arming
        else not any(reason in hard_probe_reasons for reason in result["reasons"])
    )
    result["dual40_runtime_contract"] = "P26_BOOK_COLLECTOR_HEALTH_V1"
    return result
