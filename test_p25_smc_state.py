from pathlib import Path
from types import SimpleNamespace

from p25_smc_state import build_operational_state


class _Cfg:
    phase = "P2.5"
    min_markets_for_stats = 30
    training_active = True
    calibration_active = True
    model_inference_active = True
    forecast_recording_active = True

    @staticmethod
    def assets():
        return ["BTC", "ETH", "SOL", "XRP"]

    @staticmethod
    def horizons():
        return ["5m"]


class _ExplodingRecorder:
    def stats(self):
        raise AssertionError("operational state must never query recorder.stats")

    def forecast_analytics(self, *_args, **_kwargs):
        raise AssertionError("operational state must never query forecast analytics")


class _Discovery:
    discovery_errors = 0
    last_discovery_ts = 123.0

    @staticmethod
    def snapshot_status():
        return {
            "BTC:5m": "FOUND",
            "ETH:5m": "FOUND",
            "SOL:5m": "NOT_FOUND",
            "XRP:5m": "FOUND",
        }


class _Model:
    @staticmethod
    def stats():
        return {"ready": True}


class _Calibration:
    @staticmethod
    def summary():
        return {"samples": 7}


def _engine():
    discovery = _Discovery()
    binance = SimpleNamespace(
        connected=True,
        clock_synced=True,
        clock_offset_ms=1.2,
    )
    chainlink = SimpleNamespace(status=lambda: {"connection": "connected"})
    hub = SimpleNamespace(
        discovery=discovery,
        binance=binance,
        reference=SimpleNamespace(chainlink=chainlink),
        clob_store=SimpleNamespace(counters={"book_events": 10}),
    )
    return SimpleNamespace(
        cfg=_Cfg(),
        hub=hub,
        recorder=_ExplodingRecorder(),
        model=_Model(),
        calib=_Calibration(),
        latest={
            "BTC:5m": {
                "combo": "BTC:5m",
                "active": True,
                "up_mid": 0.55,
                "down_mid": 0.45,
                "official_reference_open": 100.0,
                "feature": {"ready": True},
                "p_up_raw": 0.70,
            },
            "ETH:5m": {
                "combo": "ETH:5m",
                "active": True,
                "up_mid": 0.50,
                "down_mid": 0.50,
                "official_reference_open": 200.0,
                "feature": {"ready": True},
                "p_up_raw": 0.45,
            },
            "XRP:5m": {
                "combo": "XRP:5m",
                "active": True,
                "up_mid": 0.40,
                "down_mid": 0.60,
                "official_reference_open": 3.0,
                "feature": {"ready": False},
                "p_up_raw": None,
            },
        },
        events=[],
        started_at=0.0,
        _clob=SimpleNamespace(transport_healthy=True),
        _recorded_markets={"a", "b", "c"},
        _fired={"a": {60, 30}, "b": {60}},
        _resolve_count=2,
        _forecast_writes=5,
        _data_quality_errors=0,
        _model_learn_calls=0,
        _model_save_calls=0,
        _calibration_writes=0,
    )


def test_operational_state_never_scans_sqlite_and_keeps_four_5m_cards():
    state = build_operational_state(_engine())

    assert state["forecast_analytics"] == {
        "status": "DEFERRED",
        "reason": "SMC_V3_ZERO_BLOCKING_OPERATIONAL_STATE",
    }
    assert state["recorder"]["database_scanned"] is False
    assert state["recorder"]["markets_runtime"] == 3
    assert state["footer"]["markets_active"] == 3
    assert state["footer"]["clob_quote_healthy"] == 3
    assert state["footer"]["ptb_states_healthy"] == 3
    assert state["footer"]["features_ready"] == 2
    assert [card["combo"] for card in state["cards"]] == [
        "BTC:5m",
        "ETH:5m",
        "SOL:5m",
        "XRP:5m",
    ]
    sol = next(card for card in state["cards"] if card["combo"] == "SOL:5m")
    assert sol["active"] is False
    assert sol["discovery_status"] == "NOT_FOUND"
    assert state["operational_state_build_ms"] >= 0.0


def test_smc_entrypoint_installs_zero_blocking_state_after_smc_patch():
    text = Path("p25_main_smc.py").read_text(encoding="utf-8")
    runtime = text.index("install_smc_v3_runtime_hardening()")
    structural = text.index("enable_smc_v3()")
    zero_blocking = text.index("install_zero_blocking_operational_state()")
    assert runtime < structural < zero_blocking

    state_text = Path("p25_smc_state.py").read_text(encoding="utf-8")
    assert "engine.recorder.stats" in state_text  # documented forbidden invariant
    assert "recorder.stats()" not in state_text
    assert "forecast_analytics()" not in state_text
