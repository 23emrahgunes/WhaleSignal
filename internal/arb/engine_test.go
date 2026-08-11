package arb

import (
	"math"
	"testing"

	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
)

func book(token string, bid, ask float64) polymarket.BookSnapshot {
	return polymarket.BookSnapshot{TokenID: token, BestBid: bid, BestAsk: ask, TickSize: 0.01, MinOrderSize: 5}
}

func baseResult() *engine.EvaluationResult {
	return &engine.EvaluationResult{Timestamp: "2026-08-12T00:00:00Z", PUp: .7, PDown: .3, PTBTerminal: engine.PTBTerminalEstimate{Ready: true, Decision: "UP", PAbove: .80, PBelow: .20, Confidence: 60}}
}

func TestMakerBuyPriceNeverCrossesAsk(t *testing.T) {
	p, ok := MakerBuyPrice(book("u", .42, .44), true)
	if !ok || math.Abs(p-.43) > 1e-9 {
		t.Fatalf("got %.4f ok=%v", p, ok)
	}
	p, ok = MakerBuyPrice(book("u", .42, .43), true)
	if !ok || math.Abs(p-.42) > 1e-9 {
		t.Fatalf("one-tick spread must join bid: %.4f", p)
	}
}

func TestDynamicMinOrderSizeAndSafeLegSkew(t *testing.T) {
	e := NewEngine(Config{Enabled: true, Timeframe: "5m", TargetEdge: .02, OperationalBuffer: .002, UncertaintyPenalty: .02, MaxStrandedUnits: 1})
	up := book("up", .40, .44)
	down := book("down", .54, .58)
	down.MinOrderSize = 7
	s := e.Evaluate(baseResult(), &polymarket.Market{Slug: "btc-updown-5m-1"}, up, down)
	if s == nil {
		t.Fatal("nil snapshot")
	}
	if s.OrderSize != 7 || s.MaxStrandedShares != 7 {
		t.Fatalf("dynamic min size not used: %+v", s)
	}
	if s.FirstLeg != "UP" {
		t.Fatalf("expected safe UP leg, got %s", s.FirstLeg)
	}
	if s.UpMakerPrice != .41 || s.DownMakerPrice != .54 {
		t.Fatalf("expected safe-leg queue jump, got %.2f/%.2f", s.UpMakerPrice, s.DownMakerPrice)
	}
	if !s.PairEdgePass || s.Status != StatusCandidate {
		t.Fatalf("expected candidate: %+v", s)
	}
}

func TestPairEdgeBelowTargetBlocked(t *testing.T) {
	e := NewEngine(Config{Enabled: true, TargetEdge: .03, OperationalBuffer: .002})
	s := e.Evaluate(baseResult(), &polymarket.Market{Slug: "btc-updown-5m-1"}, book("up", .49, .51), book("down", .49, .51))
	if s.PairEdgePass || s.Status != StatusBlocked || s.Reason != "PAIR_EDGE_BELOW_TARGET" {
		t.Fatalf("unexpected %+v", s)
	}
}

func TestPTBNotReadyFailsClosed(t *testing.T) {
	r := baseResult()
	r.PTBTerminal.Ready = false
	e := NewEngine(Config{Enabled: true, TargetEdge: .02, OperationalBuffer: .002})
	s := e.Evaluate(r, &polymarket.Market{Slug: "btc-updown-5m-1"}, book("up", .40, .44), book("down", .54, .58))
	if s.Status != StatusBlocked || s.Reason != "PTB_TERMINAL_NOT_READY" {
		t.Fatalf("unexpected %+v", s)
	}
}

func TestCompletionMaxPreservesTargetAndPostOnly(t *testing.T) {
	opposite := book("down", .54, .58)
	got := completionMax(.41, .02, .002, opposite)
	if got != .56 {
		t.Fatalf("got %.4f want .56 (arb ceiling)", got)
	}
	if .41+got+.02+.002 > 1+1e-9 {
		t.Fatal("completion price breaks target edge")
	}
}
