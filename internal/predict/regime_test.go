package predict

import "testing"

func baseThresholds() Thresholds {
	return Thresholds{
		PredictabilityMin: 0.55, CoherenceMin: 0.40, MaxFlipRate: 0.50,
		HighVolBps: 8.0, TrendZMin: 1.5, MaxDataAgeMs: 3000, MinHistory: 8,
	}
}

func TestClassifyCleanChop(t *testing.T) {
	f := Features{
		BandCoherence: 0.85, OBIFlipRate: 0.10, OBISignPersistence: 0.8,
		FlowPersistence: 0.75, DirectionConsistency: 0.2, FlowAcceleration: 0.1,
		RealizedVolBps: 2.0, TrendZ: 0.3, DataAgeMs: 200, HistorySamples: 20,
	}
	r := Classify(f, baseThresholds())
	if r.Regime != RegimeChop || !r.Predictable {
		t.Fatalf("temiz market CHOP+predictable olmali: %+v", r)
	}
	if len(r.Reasons) != 0 {
		t.Fatalf("temiz markette red nedeni olmamali: %v", r.Reasons)
	}
}

func TestClassifyChaoticLowCoherence(t *testing.T) {
	f := Features{
		BandCoherence: 0.15, OBIFlipRate: 0.7, FlowPersistence: 0.3,
		RealizedVolBps: 3.0, DataAgeMs: 200, HistorySamples: 20,
	}
	r := Classify(f, baseThresholds())
	if r.Regime != RegimeChaotic || r.Predictable {
		t.Fatalf("kaotik market CHAOTIC + predictable=false olmali: %+v", r)
	}
	if !hasReason(r.Reasons, "LOW_BAND_COHERENCE") || !hasReason(r.Reasons, "HIGH_OBI_FLIP_RATE") {
		t.Fatalf("kaotik nedenler eksik: %v", r.Reasons)
	}
}

func TestClassifyUnsafeStaleAndHistory(t *testing.T) {
	f := Features{BandCoherence: 0.9, DataAgeMs: 9000, HistorySamples: 2}
	r := Classify(f, baseThresholds())
	if r.Regime != RegimeUnsafe || r.Predictable {
		t.Fatalf("stale/az-veri UNSAFE olmali: %+v", r)
	}
	if !hasReason(r.Reasons, "STALE_DATA") || !hasReason(r.Reasons, "INSUFFICIENT_HISTORY") {
		t.Fatalf("unsafe nedenler eksik: %v", r.Reasons)
	}
}

func TestClassifyHighVol(t *testing.T) {
	f := Features{BandCoherence: 0.8, OBIFlipRate: 0.1, FlowPersistence: 0.8,
		RealizedVolBps: 15.0, DataAgeMs: 100, HistorySamples: 20}
	r := Classify(f, baseThresholds())
	if r.Regime != RegimeHighVol || r.Predictable {
		t.Fatalf("yuksek vol HIGH_VOL + predictable=false olmali: %+v", r)
	}
}

func TestClassifyTrend(t *testing.T) {
	f := Features{BandCoherence: 0.8, OBIFlipRate: 0.1, FlowPersistence: 0.8,
		DirectionConsistency: 0.6, TrendZ: 2.2, RealizedVolBps: 3.0,
		DataAgeMs: 100, HistorySamples: 20}
	r := Classify(f, baseThresholds())
	if r.Regime != RegimeTrendUp {
		t.Fatalf("guclu yukari trend TREND_UP olmali: %+v", r)
	}
}

func hasReason(rs []string, want string) bool {
	for _, r := range rs {
		if r == want {
			return true
		}
	}
	return false
}
