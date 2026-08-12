package arb

import (
	"math"
	"testing"

	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
)

func book(token string, bid, ask float64) polymarket.BookSnapshot {
	return polymarket.BookSnapshot{TokenID: token, BestBid: bid, BestAsk: ask, TickSize: .01, MinOrderSize: 5,
		Bids: []polymarket.CLOBLevel{{Price: bid, Size: 10}}, Asks: []polymarket.CLOBLevel{{Price: ask, Size: 10}}}
}
func baseResult() *engine.EvaluationResult {
	return &engine.EvaluationResult{Timestamp: "2026-08-12T00:00:00Z", PUp: .7, PDown: .3, PTBTerminal: engine.PTBTerminalEstimate{Ready: true, Decision: "UP", PAbove: .80, PBelow: .20, Confidence: 60}}
}

func TestMakerBuyPriceNeverCrossesAsk(t *testing.T) {
	p, ok := MakerBuyPrice(book("u", .42, .44), true)
	if !ok || math.Abs(p-.43) > 1e-9 {
		t.Fatalf("%.4f %v", p, ok)
	}
	p, ok = MakerBuyPrice(book("u", .42, .43), true)
	if !ok || math.Abs(p-.42) > 1e-9 {
		t.Fatalf("one tick %.4f", p)
	}
}

func TestSafeFirstSequentialAndDynamicMinSize(t *testing.T) {
	e := NewEngine(Config{Enabled: true, Timeframe: "5m", TargetEdge: .02, PaperMinEdge: .002, OperationalBuffer: .002, UncertaintyPenalty: .02, MaxStrandedUnits: 1})
	up := book("up", .40, .44)
	down := book("down", .53, .58)
	down.MinOrderSize = 7
	s := e.Evaluate(baseResult(), &polymarket.Market{Slug: "m"}, up, down)
	if s.OrderSize != 7 || s.FirstLeg != "UP" {
		t.Fatalf("%+v", s)
	}
	if s.StrategyMode != "SAFE_FIRST_SEQUENTIAL_MAKER" || s.UpMakerPrice != .41 || s.DownMakerPrice != .54 {
		t.Fatalf("sequential %+v", s)
	}
	if !s.PaperEdgePass || !s.LiveEdgePass || s.Status != StatusCandidate {
		t.Fatalf("candidate %+v", s)
	}
}

func TestPaperCandidateBelowLiveTargetStillCollects(t *testing.T) {
	e := NewEngine(Config{Enabled: true, TargetEdge: .02, PaperMinEdge: .002, OperationalBuffer: .002})
	// 0.99 planned pair -> 0.8% net, below 2% live but above 0.2% paper.
	s := e.Evaluate(baseResult(), &polymarket.Market{Slug: "m"}, book("up", .12, .13), book("down", .87, .88))
	if !s.PaperEdgePass || s.LiveEdgePass || s.Status != StatusPaperCandidate || s.Reason != "PAPER_READY_LIVE_EDGE_BELOW_TARGET" {
		t.Fatalf("%+v", s)
	}
}

func TestBelowPaperMinBlocked(t *testing.T) {
	e := NewEngine(Config{Enabled: true, TargetEdge: .02, PaperMinEdge: .019, OperationalBuffer: .002})
	s := e.Evaluate(baseResult(), &polymarket.Market{Slug: "m"}, book("up", .49, .50), book("down", .50, .51))
	if s.PaperEdgePass || s.Status != StatusBlocked || s.Reason != "NO_COMPETITIVE_COMPLETION_WITHIN_EDGE" {
		t.Fatalf("%+v", s)
	}
}

func TestPTBNotReadyFailsClosed(t *testing.T) {
	r := baseResult()
	r.PTBTerminal.Ready = false
	e := NewEngine(Config{Enabled: true, TargetEdge: .02, PaperMinEdge: .002, OperationalBuffer: .002})
	s := e.Evaluate(r, &polymarket.Market{Slug: "m"}, book("up", .40, .44), book("down", .53, .58))
	if s.Status != StatusBlocked || s.Reason != "PTB_TERMINAL_NOT_READY" {
		t.Fatalf("%+v", s)
	}
}

func TestQueueAheadCountsOnlySamePriceFIFO(t *testing.T) {
	b := book("u", .40, .44)
	b.Bids = []polymarket.CLOBLevel{{Price: .41, Size: 3}, {Price: .40, Size: 7}}
	if q := buyQueueAhead(b, .40); q != 7 {
		t.Fatalf("same-price q %.2f", q)
	}
	if q := buyQueueAhead(b, .42); q != 0 {
		t.Fatalf("improved q %.2f", q)
	}
}
