"""Regression tests for the P2.5 model decision safety gate."""

from p25_safety_engine import evaluate_decision_gate


def test_current_absurd_down_case_is_blocked_as_unvalidated():
    """Reproduces BTC5m screenshot: model DOWN, PTB/market/consensus UP."""
    result = evaluate_decision_gate(
        p_up=0.009,
        threshold_source="DEFAULT_INSUFFICIENT",
        calibration_source="OVERALL_RELIABILITY",
        calibration_n=14,
        market_up=0.995,
        ptb_model_up=1.0,
        directional_vote=1.0,
        directional_consensus=1.0,
        calibration_enabled=True,
        require_learned_threshold=True,
    )
    assert not result.allowed
    assert result.reason == "MODEL_UNVALIDATED"
    assert result.candidate_decision == "DOWN"


def test_learned_threshold_still_blocks_extreme_market_contradiction():
    result = evaluate_decision_gate(
        p_up=0.02,
        threshold_source="OVERALL_LEARNED",
        calibration_source="OVERALL_RELIABILITY",
        calibration_n=40,
        market_up=0.995,
        ptb_model_up=0.99,
        directional_vote=1.0,
        directional_consensus=0.95,
        calibration_enabled=True,
        require_learned_threshold=True,
    )
    assert not result.allowed
    assert result.reason == "MODEL_MARKET_CONFLICT"
    assert result.opposing_votes >= 2


def test_learned_threshold_blocks_two_independent_opposing_votes():
    result = evaluate_decision_gate(
        p_up=0.80,
        threshold_source="OVERALL_LEARNED",
        calibration_source="OVERALL_RELIABILITY",
        calibration_n=50,
        market_up=0.25,
        ptb_model_up=0.20,
        directional_vote=0.1,
        directional_consensus=0.2,
        calibration_enabled=True,
        require_learned_threshold=True,
    )
    assert not result.allowed
    assert result.reason == "MODEL_BASELINE_CONFLICT"
    assert result.opposing_votes == 2


def test_per_combo_validated_model_can_pass_with_three_supporting_votes():
    result = evaluate_decision_gate(
        p_up=0.78,
        threshold_source="PER_COMBO_LEARNED",
        calibration_source="PER_COMBO_RELIABILITY",
        calibration_n=80,
        market_up=0.75,
        ptb_model_up=0.72,
        directional_vote=0.8,
        directional_consensus=0.9,
        calibration_enabled=True,
        require_learned_threshold=True,
    )
    assert result.allowed
    assert result.reason == "PASS"
    assert result.supporting_votes == 3


def test_uncalibrated_bin_is_blocked_even_after_threshold_learning():
    result = evaluate_decision_gate(
        p_up=0.70,
        threshold_source="OVERALL_LEARNED",
        calibration_source="RAW_INSUFFICIENT_BIN",
        calibration_n=3,
        market_up=0.65,
        ptb_model_up=0.68,
        directional_vote=0.7,
        directional_consensus=0.9,
        calibration_enabled=True,
        require_learned_threshold=True,
    )
    assert not result.allowed
    assert result.reason == "MODEL_UNCALIBRATED_BIN"
