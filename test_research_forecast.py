"""Regression tests for the separate always-on research forecast layer."""
from __future__ import annotations

import time

import pytest

from models import (
    Asset,
    AssetHorizon,
    Decision,
    FeatureSnapshot,
    Horizon,
    MarketRef,
    ResolutionType,
)
from p25_research_forecast import build_research_forecast
from p25_research_recorder import P25ResearchRecorder


COMBO = AssetHorizon(Asset.BTC, Horizon.H5M)


def test_screenshot_absurd_down_candidate_becomes_up_research_forecast():
    """Model says 0.9% UP while PTB, market and features all say UP."""
    forecast = build_research_forecast(
        model_p_up=0.009,
        external_p_up=0.13,
        ptb_model_p_up=1.0,
        ptb_heuristic_p_up=0.90,
        market_p_up=0.995,
        directional_vote=1.0,
        directional_consensus=1.0,
        predictability=0.88,
        conflict_score=0.0,
        model_markets=29,
        validated_signal=False,
    )
    assert forecast.direction == "UP"
    assert forecast.p_up > 0.70
    assert forecast.status == "PROVISIONAL"
    assert forecast.grade in {"MEDIUM", "HIGH"}
    contributions = {c.name: c.contribution for c in forecast.components}
    assert contributions["full_model"] < 0
    assert sum(v for v in contributions.values()) > 0


def test_aligned_down_sources_produce_down_forecast():
    forecast = build_research_forecast(
        model_p_up=0.03,
        external_p_up=0.08,
        ptb_model_p_up=0.10,
        ptb_heuristic_p_up=0.20,
        market_p_up=0.06,
        directional_vote=-0.85,
        directional_consensus=0.90,
        predictability=0.82,
        conflict_score=0.05,
        model_markets=45,
        validated_signal=False,
    )
    assert forecast.direction == "DOWN"
    assert forecast.p_up < 0.30
    assert forecast.confidence > 0.45


def test_early_forecast_works_without_trained_model():
    forecast = build_research_forecast(
        model_p_up=None,
        external_p_up=None,
        ptb_model_p_up=None,
        ptb_heuristic_p_up=0.72,
        market_p_up=0.68,
        directional_vote=0.65,
        directional_consensus=0.75,
        predictability=0.70,
        conflict_score=0.10,
        model_markets=0,
        validated_signal=False,
    )
    assert forecast.direction == "UP"
    assert forecast.p_up > 0.60
    assert forecast.status == "PROVISIONAL"


def test_research_probability_is_bounded_and_symmetric():
    up = build_research_forecast(
        model_p_up=1.0,
        external_p_up=1.0,
        ptb_model_p_up=1.0,
        ptb_heuristic_p_up=1.0,
        market_p_up=1.0,
        directional_vote=1.0,
        directional_consensus=1.0,
        predictability=1.0,
        conflict_score=0.0,
        model_markets=500,
        validated_signal=True,
    )
    down = build_research_forecast(
        model_p_up=0.0,
        external_p_up=0.0,
        ptb_model_p_up=0.0,
        ptb_heuristic_p_up=0.0,
        market_p_up=0.0,
        directional_vote=-1.0,
        directional_consensus=1.0,
        predictability=1.0,
        conflict_score=0.0,
        model_markets=500,
        validated_signal=True,
    )
    assert 0.5 < up.p_up <= 0.95
    assert 0.05 <= down.p_up < 0.5
    assert up.p_up == pytest.approx(1.0 - down.p_up)


def _ref() -> MarketRef:
    start = time.time() - 240.0
    return MarketRef(
        combo=COMBO,
        condition_id="0xresearch",
        slug=f"btc-updown-5m-{int(start)}",
        question="Bitcoin Up or Down",
        up_token_id="up-token",
        down_token_id="down-token",
        start_ts=start,
        end_ts=start + 300.0,
        market_start_ts=start,
        market_end_ts=start + 300.0,
        resolution_source="Chainlink BTC/USD",
        resolution_type=ResolutionType.CHAINLINK_TWAP,
        official_reference_open=100.0,
        official_reference_open_time=start,
        official_reference_source="CHAINLINK_DATA_STREAM_RTDS",
    )


def _snapshot() -> FeatureSnapshot:
    return FeatureSnapshot(
        combo=COMBO,
        ts=time.time(),
        seconds_remaining=60.0,
        tte_sec=60.0,
        up_mid=0.70,
        down_mid=0.30,
    )


def _trace() -> dict:
    return {
        "phase": "P2.5",
        "model_version": "MODEL_B2_LOGISTIC_V1",
        "model_source": "MODEL_B2_FULL:shared",
        "feature_ready": True,
        "feature_coverage": 1.0,
        "predictability": 0.80,
        "conflict_score": 0.05,
        "directional_consensus": 0.90,
        "regime": "TREND_UP",
        "p_up_raw": 0.10,
        "p_up_calibrated": 0.12,
        "p_up_ptb": 0.75,
        "p_up_ptb_heuristic": 0.70,
        "p_up_external": 0.40,
        "p_up_market": 0.70,
        "confidence": 0.0,
        "decision": "ABSTAIN",
        "abstain_reason": "MODEL_UNVALIDATED",
        "threshold": 0.62,
        "threshold_source": "DEFAULT_INSUFFICIENT",
        "calibration_source": "RAW_INSUFFICIENT_BIN",
        "forecast_direction": "UP",
        "forecast_p_up": 0.72,
        "forecast_confidence": 0.61,
        "forecast_grade": "MEDIUM",
        "forecast_status": "PROVISIONAL",
        "forecast_source": "ROBUST_ENSEMBLE_V1",
        "forecast_agreement": 0.82,
        "forecast_model_maturity": 0.25,
    }


def test_research_forecast_is_persisted_and_scored_separately(tmp_path):
    recorder = P25ResearchRecorder(str(tmp_path / "research.sqlite"))
    ref = _ref()
    snap = _snapshot()
    recorder.record_market(ref)
    assert recorder.record_forecast(ref, snap, 60, _trace())

    row = recorder.conn.execute(
        """
        SELECT decision, forecast_direction, forecast_p_up,
               forecast_status, forecast_correct
        FROM forecasts WHERE condition_id=?
        """,
        (ref.condition_id,),
    ).fetchone()
    assert row["decision"] == "ABSTAIN"
    assert row["forecast_direction"] == "UP"
    assert row["forecast_p_up"] == pytest.approx(0.72)
    assert row["forecast_status"] == "PROVISIONAL"
    assert row["forecast_correct"] is None

    ref.resolved = True
    ref.official_result = Decision.UP
    ref.resolved_outcome = Decision.UP
    ref.official_result_source = "winning_outcome"
    ref.computed_result = Decision.UP
    ref.computed_result_source = "CHAINLINK_DATA_STREAM_RTDS_CLOSE"
    recorder.settle(ref)

    row = recorder.conn.execute(
        """
        SELECT correct, forecast_correct, forecast_brier
        FROM forecasts WHERE condition_id=?
        """,
        (ref.condition_id,),
    ).fetchone()
    assert row["correct"] is None  # validated signal was ABSTAIN
    assert row["forecast_correct"] == 1
    assert row["forecast_brier"] == pytest.approx((0.72 - 1.0) ** 2)

    analytics = recorder.forecast_analytics(min_n=1)["overall"]["research_forecast"]
    assert analytics["n"] == 1
    assert analytics["accuracy"] == 1.0
    assert analytics["brier"] == pytest.approx((0.72 - 1.0) ** 2)
    recorder.close()
