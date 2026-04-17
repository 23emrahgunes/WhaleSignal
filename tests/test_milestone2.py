import pytest
import os
import json
from src.persistence import PersistenceManager
from src.wallet_transitions import TransitionEngine
from src.wallet_quality import WalletQualityScorer

def test_persistence_snapshot(tmp_path):
    history_dir = tmp_path / "history"
    pm = PersistenceManager(history_dir=str(history_dir))
    data = [{"address": "0x1", "final_score": 0.8}]
    pm.save_snapshot(data, "test")

    assert len(os.listdir(history_dir)) == 1
    latest = pm.get_latest_snapshot("test")
    assert latest[0]["address"] == "0x1"

def test_transition_rising():
    # Mock PM to return a specific "previous" state
    class MockPM:
        def get_latest_snapshot(self, before_date=None):
            return [{"address": "0x1", "final_score": 0.5, "tier": "B", "penalties": {"stale": 0}}]

    engine = TransitionEngine()
    engine.pm = MockPM()

    current = [{"address": "0x1", "final_score": 0.7, "tier": "A", "penalties": {"stale": 0}}]
    transitions = engine.analyze_transitions(current)

    assert len(transitions["rising_wallets"]) == 1
    assert len(transitions["upgraded_wallets"]) == 1

def test_stability_modifier():
    history = [
        {"score": 0.8, "date": "2026-04-10"},
        {"score": 0.81, "date": "2026-04-11"},
        {"score": 0.79, "date": "2026-04-12"}
    ]
    scorer = WalletQualityScorer(history=history)
    bonus = scorer._calc_stability_modifier()
    assert bonus > 0 # Should get stability bonus

    volatile_history = [
        {"score": 0.8, "date": "2026-04-10"},
        {"score": 0.2, "date": "2026-04-11"},
        {"score": 0.9, "date": "2026-04-12"}
    ]
    scorer = WalletQualityScorer(history=volatile_history)
    penalty = scorer._calc_stability_modifier()
    assert penalty < 0 # Should get volatility penalty
