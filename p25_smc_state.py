"""Zero-blocking operational state for the SMC Selective V3 service.

The generic P2.5 snapshot path performs historical SQLite analytics.  That is useful
for offline research, but it must not share a SQLite connection with the 500ms trading
loop: a background state refresh can otherwise serialize recorder reads/writes and
starve the aiohttp control plane for many seconds.

This module builds the live dashboard/control payload exclusively from in-memory
runtime state.  Historical forecast analytics remain available from the database and
CSV/offline tools, but they are deliberately not computed by ``/api/state``.
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Any

import p25_engine as core_engine
from main import build_combos
from models import AbstainReason, Decision

_INSTALLED = False
_ORIGINAL_CORE_SNAPSHOT = None


def _copy_mapping(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    # ``dict.copy`` executes while holding the GIL and is safe for our small runtime
    # maps.  A defensive retry keeps the state endpoint fail-visible under unusual
    # third-party mapping implementations.
    for _ in range(2):
        try:
            return dict(value)
        except RuntimeError:
            time.sleep(0)
    return {}


def _copy_events(value: object) -> list:
    for _ in range(2):
        try:
            return list(value or [])
        except RuntimeError:
            time.sleep(0)
    return []


def _safe_call(obj: object, method: str, default: object) -> object:
    fn = getattr(obj, method, None)
    if not callable(fn):
        return default
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _runtime_recorder_view(engine: object) -> dict[str, Any]:
    """Return recorder counters without issuing a single SQL statement."""
    fired = getattr(engine, "_fired", {}) or {}
    try:
        snapshot_events = sum(len(items) for items in fired.values())
    except Exception:  # noqa: BLE001
        snapshot_events = 0
    return {
        "operational_fast_path": True,
        "database_scanned": False,
        "markets_runtime": len(getattr(engine, "_recorded_markets", set()) or set()),
        "snapshots_runtime": int(snapshot_events),
        "resolved_runtime": int(getattr(engine, "_resolve_count", 0) or 0),
        "forecasts_runtime": int(getattr(engine, "_forecast_writes", 0) or 0),
    }


def build_operational_state(engine: object) -> dict[str, Any]:
    """Build the SMC V3 state payload from memory only.

    Invariant: this function never calls ``engine.recorder.stats()``,
    ``forecast_analytics()`` or any other SQLite query.
    """
    started = time.perf_counter()
    cfg = engine.cfg
    hub = engine.hub
    latest = _copy_mapping(getattr(engine, "latest", {}))

    discovery = getattr(hub, "discovery", None)
    status = _safe_call(discovery, "snapshot_status", {})
    status = status if isinstance(status, dict) else {}

    cards: list[dict[str, Any]] = []
    up_mids: list[float] = []
    clob_quote_healthy = 0
    ptb_healthy = 0

    for combo in build_combos(cfg):
        raw = latest.get(combo.key)
        card = dict(raw) if isinstance(raw, dict) else None
        if card is None or not card.get("active"):
            card = {
                "combo": combo.key,
                "active": False,
                "discovery_status": status.get(combo.key, "NOT_FOUND"),
                "decision": Decision.ABSTAIN.value,
                "abstain_reason": AbstainReason.NO_MARKET.value,
                "why": [f"discovery={status.get(combo.key, 'NOT_FOUND')}"],
            }
        else:
            card["discovery_status"] = status.get(combo.key, "FOUND")
            up_mid = card.get("up_mid")
            down_mid = card.get("down_mid")
            if up_mid is not None and down_mid is not None:
                try:
                    up_mids.append(float(up_mid))
                    clob_quote_healthy += 1
                except (TypeError, ValueError):
                    pass
            if card.get("official_reference_open") is not None:
                ptb_healthy += 1
        cards.append(card)

    active_count = sum(1 for card in cards if card.get("active"))
    clob = getattr(engine, "_clob", None)
    clob_transport = bool(clob and getattr(clob, "transport_healthy", False))
    clob_transport_healthy = active_count if clob_transport else 0

    suspicious = False
    if len(up_mids) >= 3:
        most_common = Counter(round(value, 3) for value in up_mids).most_common(1)
        suspicious = bool(most_common and most_common[0][1] >= 3)

    binance = getattr(hub, "binance", None)
    chainlink = getattr(getattr(hub, "reference", None), "chainlink", None)
    clob_store = getattr(hub, "clob_store", None)
    clob_counters = _copy_mapping(getattr(clob_store, "counters", {}))
    recorder_view = _runtime_recorder_view(engine)

    model_payload = _safe_call(getattr(engine, "model", None), "stats", {})
    calibration_payload = _safe_call(getattr(engine, "calib", None), "summary", {})
    chainlink_payload = _safe_call(chainlink, "status", {}) if chainlink is not None else {}

    footer = {
        "markets_active": active_count,
        "markets_discovered_total": recorder_view["markets_runtime"],
        "snapshots_total": recorder_view["snapshots_runtime"],
        "snapshots_labeled": 0,
        "resolved_total": recorder_view["resolved_runtime"],
        "label_mismatch": 0,
        "clob_transport_healthy": clob_transport_healthy,
        "clob_quote_healthy": clob_quote_healthy,
        "ptb_states_healthy": ptb_healthy,
        "discovery_errors": int(getattr(discovery, "discovery_errors", 0) or 0),
        "data_quality_errors": int(getattr(engine, "_data_quality_errors", 0) or 0),
        "suspicious_identical_quotes": suspicious,
        "features_ready": sum(
            1
            for card in cards
            if card.get("active") and (card.get("feature") or {}).get("ready")
        ),
        "model_ready_cards": sum(
            1
            for card in cards
            if card.get("active") and card.get("p_up_raw") is not None
        ),
        "forecast_writes_runtime": recorder_view["forecasts_runtime"],
        # Historical totals are intentionally unavailable in the operational route.
        "forecasts": recorder_view["forecasts_runtime"],
        "labeled_forecasts": 0,
        **clob_counters,
    }

    payload = {
        "now": time.time(),
        "uptime_sec": round(
            max(0.0, time.time() - float(getattr(engine, "started_at", time.time()))),
            1,
        ),
        "mode": "SHADOW",
        "phase": str(getattr(cfg, "phase", "P2.5")),
        "cards": cards,
        "recorder": recorder_view,
        "model": model_payload if isinstance(model_payload, dict) else {},
        "calibration": (
            calibration_payload if isinstance(calibration_payload, dict) else {}
        ),
        "min_markets_for_stats": int(getattr(cfg, "min_markets_for_stats", 30) or 30),
        "events": _copy_events(getattr(engine, "events", [])),
        "discovery_status": status,
        "discovery_last_ts": float(getattr(discovery, "last_discovery_ts", 0.0) or 0.0),
        "binance_connected": bool(getattr(binance, "connected", False)),
        "clock_synced": bool(getattr(binance, "clock_synced", False)),
        "clock_offset_ms": getattr(binance, "clock_offset_ms", None),
        "chainlink": chainlink_payload if isinstance(chainlink_payload, dict) else {},
        "forecast_analytics": {
            "status": "DEFERRED",
            "reason": "SMC_V3_ZERO_BLOCKING_OPERATIONAL_STATE",
        },
        "safety": {
            "phase": str(getattr(cfg, "phase", "P2.5")),
            "model_training_enabled": bool(getattr(cfg, "training_active", False)),
            "calibration_enabled": bool(getattr(cfg, "calibration_active", False)),
            "model_learn_calls": int(getattr(engine, "_model_learn_calls", 0) or 0),
            "model_save_calls": int(getattr(engine, "_model_save_calls", 0) or 0),
            "calibration_writes": int(getattr(engine, "_calibration_writes", 0) or 0),
            "live_orders": 0,
            "model_inference_enabled": bool(
                getattr(cfg, "model_inference_active", False)
            ),
            "forecast_recording_enabled": bool(
                getattr(cfg, "forecast_recording_active", False)
            ),
            "execution_enabled": False,
            "private_key_loaded": False,
        },
        "footer": footer,
    }
    payload["operational_state_build_ms"] = round(
        (time.perf_counter() - started) * 1000.0,
        3,
    )
    return payload


def install_zero_blocking_operational_state() -> None:
    """Replace the core P2.5 snapshot after all other V3 patches are installed."""
    global _INSTALLED, _ORIGINAL_CORE_SNAPSHOT
    if _INSTALLED:
        return
    _ORIGINAL_CORE_SNAPSHOT = core_engine.P25Engine.snapshot
    core_engine.P25Engine.snapshot = build_operational_state
    _INSTALLED = True
