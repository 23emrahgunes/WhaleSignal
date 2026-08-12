package dual40

import (
	"math"
	"testing"
)

func TestAnalyzeTrials(t *testing.T) {
	trials := []Trial{
		// completed (first fill CHOP), +1.0
		{State: StateCompleted, PaperPnL: 1.0, FirstFillAt: "x", FirstFillRegime: "CHOP", FirstFillDriftBps: 1.0},
		{State: StateCompleted, PaperPnL: 1.0, FirstFillAt: "x", FirstFillRegime: "CHOP", FirstFillDriftBps: 0.5},
		// hedged (first fill TREND), -0.25
		{State: StateHedged, PaperPnL: -0.25, FirstFillAt: "x", FirstFillRegime: "TREND_UP", FirstFillDriftBps: 6.0},
		{State: StateHedged, PaperPnL: -0.25, FirstFillAt: "x", FirstFillRegime: "TREND_UP", FirstFillDriftBps: 5.0},
		// expired no fill (no first fill), 0
		{State: StateExpiredNoFill, PaperPnL: 0},
		// data gap invalid -> HARIC
		{State: StateDataGapInvalid, PaperPnL: -99},
		// skipped -> HARIC
		{State: StateSkipped, PaperPnL: 0},
	}
	a := AnalyzeTrials(trials)

	if a.ResolvedN != 5 { // 2 completed + 2 hedged + 1 expired (datagap & skipped haric)
		t.Fatalf("ResolvedN=%d, beklenen 5", a.ResolvedN)
	}
	if math.Abs(a.NetPnL-1.5) > 1e-9 { // 1+1-0.25-0.25+0
		t.Fatalf("NetPnL=%.4f, beklenen 1.5", a.NetPnL)
	}
	if a.Completed != 2 || a.Hedged != 2 {
		t.Fatalf("Completed=%d Hedged=%d", a.Completed, a.Hedged)
	}
	if math.Abs(a.DualFillRate-0.5) > 1e-9 { // 2/(2+2)
		t.Fatalf("DualFillRate=%.4f, beklenen 0.5", a.DualFillRate)
	}
	if a.FirstFillN != 4 {
		t.Fatalf("FirstFillN=%d, beklenen 4", a.FirstFillN)
	}
	// CHOP bucket: 2 first fill, ikisi de completed -> P=1.0
	var chop *BucketStat
	for i := range a.ByRegime {
		if a.ByRegime[i].Key == "CHOP" {
			chop = &a.ByRegime[i]
		}
	}
	if chop == nil || math.Abs(chop.PSecondGivenFirst-1.0) > 1e-9 {
		t.Fatalf("CHOP bucket P(second|first) beklenen 1.0, got %+v", chop)
	}
	// TREND bucket: 2 first fill, ikisi de hedged -> P=0
	var trend *BucketStat
	for i := range a.ByRegime {
		if a.ByRegime[i].Key == "TREND_UP" {
			trend = &a.ByRegime[i]
		}
	}
	if trend == nil || trend.PSecondGivenFirst != 0 {
		t.Fatalf("TREND bucket P(second|first) beklenen 0, got %+v", trend)
	}
}
