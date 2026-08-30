from p25_signal_stability import SignalStabilityGate


def test_same_side_must_persist_for_required_seconds():
    gate = SignalStabilityGate(required_sec=3.0, max_gap_sec=1.5)
    assert gate.observe("c1", "UP", 100.0) == (False, 0.0)
    ok, elapsed = gate.observe("c1", "UP", 101.0)
    assert not ok and elapsed == 1.0
    ok, elapsed = gate.observe("c1", "UP", 102.0)
    assert not ok and elapsed == 2.0
    ok, elapsed = gate.observe("c1", "UP", 103.0)
    assert ok and elapsed == 3.0


def test_direction_flip_or_observation_gap_resets_clock():
    gate = SignalStabilityGate(required_sec=3.0, max_gap_sec=1.5)
    gate.observe("c1", "UP", 100.0)
    gate.observe("c1", "UP", 101.0)
    ok, elapsed = gate.observe("c1", "DOWN", 102.0)
    assert not ok and elapsed == 0.0
    ok, elapsed = gate.observe("c1", "DOWN", 104.0)
    assert not ok and elapsed == 0.0
