package dual40

import "testing"

func TestClassifyChoppyOpeningIsEligible(t *testing.T) {
	cfg := DefaultConfig()
	samples := []Sample{
		{ElapsedSec: 0, Price: 100000, FlowImbalance: 0.04, UpMid: 0.50, DownMid: 0.50},
		{ElapsedSec: 2, Price: 100020, FlowImbalance: -0.06, UpMid: 0.51, DownMid: 0.49},
		{ElapsedSec: 4, Price: 99990, FlowImbalance: 0.05, UpMid: 0.49, DownMid: 0.51},
		{ElapsedSec: 6, Price: 100018, FlowImbalance: -0.04, UpMid: 0.51, DownMid: 0.49},
		{ElapsedSec: 8, Price: 99992, FlowImbalance: 0.03, UpMid: 0.49, DownMid: 0.51},
		{ElapsedSec: 10, Price: 100015, FlowImbalance: -0.02, UpMid: 0.51, DownMid: 0.49},
	}
	m := Classify(samples, cfg)
	if !m.Eligible {
		t.Fatalf("expected CHOP eligible, got regime=%s score=%.2f reason=%s drift=%.2f range=%.2f", m.Regime, m.ChopScore, m.Reason, m.DriftBps, m.RangeBps)
	}
	if m.Regime != "CHOP" {
		t.Fatalf("expected CHOP, got %s", m.Regime)
	}
	if m.ReversalRate < 0.80 {
		t.Fatalf("expected high reversal rate, got %.3f", m.ReversalRate)
	}
}

func TestClassifyTrendOpeningIsRejected(t *testing.T) {
	cfg := DefaultConfig()
	samples := []Sample{
		{ElapsedSec: 0, Price: 100000, FlowImbalance: 0.55, UpMid: 0.52, DownMid: 0.48},
		{ElapsedSec: 2, Price: 100020, FlowImbalance: 0.60, UpMid: 0.54, DownMid: 0.46},
		{ElapsedSec: 4, Price: 100040, FlowImbalance: 0.62, UpMid: 0.57, DownMid: 0.43},
		{ElapsedSec: 6, Price: 100060, FlowImbalance: 0.65, UpMid: 0.60, DownMid: 0.40},
		{ElapsedSec: 8, Price: 100080, FlowImbalance: 0.68, UpMid: 0.64, DownMid: 0.36},
	}
	m := Classify(samples, cfg)
	if m.Eligible {
		t.Fatalf("trend should be rejected: %+v", m)
	}
	if m.Regime != "TREND_UP" && m.Regime != "FLOW_UP" {
		t.Fatalf("expected upward trend/flow regime, got %s", m.Regime)
	}
}

func TestOpeningWindowCoverageRequiresStartObservation(t *testing.T) {
	samples := []Sample{{ElapsedSec: 8, Price: 100}, {ElapsedSec: 9, Price: 101}, {ElapsedSec: 10, Price: 100}, {ElapsedSec: 11, Price: 101}}
	if OpeningWindowCovered(samples, 10) {
		t.Fatal("mid-window startup must not pretend it observed the opening")
	}
}
