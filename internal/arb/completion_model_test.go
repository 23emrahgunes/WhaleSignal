package arb

import (
	"fmt"
	"math"
	"testing"
)

func modelSnap() *Snapshot {
	return &Snapshot{
		Timeframe: "5m", FirstLeg: "UP", OrderSize: 5, TargetEdge: .02, PaperMinEdge: .002, PTBReady: true,
		UpBestBid: .40, DownBestBid: .53, UpMakerPrice: .41, DownMakerPrice: .56, NetEdge: .028,
		UpPathEligible: true, UpPathFirstPrice: .41, UpPathCompletionPrice: .56, UpPathNetEdge: .028, UpPathQueueAhead: 0,
		DownPathEligible: true, DownPathFirstPrice: .54, DownPathCompletionPrice: .43, DownPathNetEdge: .028, DownPathQueueAhead: 0,
		UpCompletionMax: .56, DownCompletionMax: .56,
	}
}

func trainingCycle(i int, leg, status string, completionMs int64, pnl float64, full bool) PaperCycle {
	c := PaperCycle{ID: int64(i + 1), PreferredFirstLeg: leg, ActualFirstLeg: leg, FillModel: "WS_SELL_TRADES_PRICE_TIME_QUEUE_PARTIAL", StrategyMode: "SAFE_FIRST_SEQUENTIAL_MAKER", Status: status, OrderSize: 5, FirstFilledShares: 5, EntryNetEdge: .028, PaperPnL: pnl, CompletionMs: completionMs, FirstPartialAt: "2026-08-12T00:00:00Z"}
	if full {
		c.FirstFullAt = "2026-08-12T00:00:00.100Z"
	} else {
		c.FirstFilledShares = 2
	}
	return c
}

func TestCompletionModelWarmupFailClosedButPaperCandidate(t *testing.T) {
	s := modelSnap()
	s.Status = StatusPaperCandidate
	s.PaperEdgePass = true
	s.LiveEdgePass = true
	rows := []PaperCycle{trainingCycle(1, "UP", PaperStatusCompleted, 800, .12, true)}
	p := DefaultCompletionPolicy()
	ApplyCompletionModel(s, rows, p)
	if s.Status != StatusPaperCandidate || s.Reason != "COMPLETION_MODEL_WARMUP" || s.PairEdgePass {
		t.Fatalf("warmup %+v", s)
	}
}

func TestCompletionModelPromotesOnlyWithProbabilityAndPositiveCycleEV(t *testing.T) {
	s := modelSnap()
	s.Status = StatusPaperCandidate
	s.PaperEdgePass = true
	s.LiveEdgePass = true
	rows := make([]PaperCycle, 0, 50)
	for i := 0; i < 36; i++ {
		rows = append(rows, trainingCycle(i, "UP", PaperStatusCompleted, 900, .14, true))
	}
	for i := 36; i < 40; i++ {
		rows = append(rows, trainingCycle(i, "UP", PaperStatusStrandedTimeout, 0, -.12, true))
	}
	for i := 40; i < 45; i++ {
		c := trainingCycle(i, "UP", PaperStatusExpiredNoFill, 0, 0, false)
		c.ActualFirstLeg = ""
		c.FirstPartialAt = ""
		c.FirstFilledShares = 0
		rows = append(rows, c)
	}
	// Also provide DOWN warmup data so path selection cannot accidentally use an empty alternative.
	for i := 45; i < 85; i++ {
		rows = append(rows, trainingCycle(i, "DOWN", PaperStatusStrandedTimeout, 0, -.40, true))
	}
	p := DefaultCompletionPolicy()
	p.MinSamples = 30
	p.MinStrandedSamples = 3
	p.MinPComplete5sLower95 = .70
	p.MinCycleEV = .01
	ApplyCompletionModel(s, rows, p)
	if s.Status != StatusCandidate || s.Reason != "READY_COMPLETION_EV" || !s.CompletionModelReady || !s.PairEdgePass {
		t.Fatalf("candidate %+v", s)
	}
	if s.PComplete5sLower95 < .70 || s.ConservativeCycleEV <= 0 || s.OpportunityEV <= 0 {
		t.Fatalf("model metrics %+v", s)
	}
}

func TestNegativeCycleEVBlocksLiveEvenWhenRawPairEdgeLooksGood(t *testing.T) {
	s := modelSnap()
	s.Status = StatusPaperCandidate
	s.PaperEdgePass = true
	s.LiveEdgePass = true
	rows := make([]PaperCycle, 0, 40)
	for i := 0; i < 30; i++ {
		rows = append(rows, trainingCycle(i, "UP", PaperStatusCompleted, 1000, .14, true))
	}
	for i := 30; i < 40; i++ {
		rows = append(rows, trainingCycle(i, "UP", PaperStatusStrandedTimeout, 0, -1.0, true))
	}
	p := DefaultCompletionPolicy()
	p.MinSamples = 30
	p.MinStrandedSamples = 3
	p.MinPComplete5sLower95 = .40
	p.MinCycleEV = .01
	p.MaxStrandedLossMultiple = 20
	ApplyCompletionModel(s, rows, p)
	if s.Status == StatusCandidate || s.ConservativeCycleEV >= p.MinCycleEV {
		t.Fatalf("must fail cycle EV %+v", s)
	}
}

func TestWilsonLowerIsConservative(t *testing.T) {
	got := wilsonLower95(27, 30)
	if !(got < .90 && got > .70) {
		t.Fatalf("wilson %.6f", got)
	}
}

func TestCompletionScopeFallsBackWhenNarrowBandSparse(t *testing.T) {
	s := modelSnap()
	rows := make([]PaperCycle, 0, 30)
	for i := 0; i < 30; i++ {
		c := trainingCycle(i, "UP", PaperStatusCompleted, 1000, .14, true)
		c.EntryNetEdge = .006
		rows = append(rows, c)
	}
	p := DefaultCompletionPolicy()
	p.MinSamples = 30
	p.MinStrandedSamples = 1
	e := EstimateCompletionModel(rows, s, p)
	if e.Scope != "LEG_ALL" {
		t.Fatalf("scope=%s %s", e.Scope, fmt.Sprint(e))
	}
	if math.IsNaN(e.PComplete5s) {
		t.Fatal("nan")
	}
}
