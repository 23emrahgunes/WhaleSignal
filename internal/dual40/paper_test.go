package dual40

import (
	"math"
	"testing"
	"time"

	"pm-edge/internal/polymarket"
)

func dualBook(token string, queue, bestAsk float64) polymarket.BookSnapshot {
	return polymarket.BookSnapshot{
		TokenID: token, BestBid: 0.40, BestAsk: bestAsk, MinOrderSize: 5, TickSize: 0.01,
		Bids: []polymarket.CLOBLevel{{Price: 0.40, Size: queue}, {Price: 0.39, Size: 100}},
		Asks: []polymarket.CLOBLevel{{Price: bestAsk, Size: 100}},
	}
}

func TestDual40QueueAwareBothFillLocksOneDollar(t *testing.T) {
	cfg := DefaultConfig()
	m := Metrics{Eligible: true, Regime: "CHOP", Reason: "ELIGIBLE_CHOP", ChopScore: 88}
	now := time.Date(2026, 8, 12, 20, 0, 0, 0, time.UTC)
	upBook := dualBook("UP", 5, 0.50)
	downBook := dualBook("DOWN", 0, 0.50)
	trial, err := NewRestingTrial("5m", "m1", 10, m, "UP", "DOWN", upBook, downBook, cfg, now, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	trades := []polymarket.MarketTrade{
		{Seq: 1, TokenID: "UP", Price: 0.40, Size: 5, Side: "SELL", Timestamp: now.Add(time.Second)},
		{Seq: 2, TokenID: "UP", Price: 0.40, Size: 5, Side: "SELL", Timestamp: now.Add(2 * time.Second)},
		{Seq: 3, TokenID: "DOWN", Price: 0.39, Size: 1, Side: "SELL", Timestamp: now.Add(3 * time.Second)},
	}
	if !Advance(trial, upBook, downBook, trades, 3, now.Add(4*time.Second), now.Add(5*time.Minute), cfg) {
		t.Fatal("expected trial change")
	}
	if trial.State != StateCompleted {
		t.Fatalf("expected completed, got %s up=%.2f down=%.2f", trial.State, trial.UpMakerFilled, trial.DownMakerFilled)
	}
	if math.Abs(trial.PaperPnL-1.0) > 1e-9 {
		t.Fatalf("expected $1 locked pnl, got %.6f", trial.PaperPnL)
	}
}

func TestAdaptiveHedgeUsesOppositeSideAndQuoteCost(t *testing.T) {
	cfg := DefaultConfig()
	m := Metrics{Eligible: true, Regime: "CHOP", ChopScore: 80}
	now := time.Date(2026, 8, 12, 20, 0, 0, 0, time.UTC)
	upBook := dualBook("UP", 0, 0.50)
	downBook := dualBook("DOWN", 0, 0.71)
	trial, err := NewRestingTrial("5m", "m2", 10, m, "UP", "DOWN", upBook, dualBook("DOWN", 0, 0.50), cfg, now, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	trades := []polymarket.MarketTrade{{Seq: 1, TokenID: "UP", Price: 0.39, Size: 1, Side: "SELL", Timestamp: now.Add(time.Second)}}
	Advance(trial, upBook, downBook, trades, 1, now.Add(2*time.Second), now.Add(5*time.Minute), cfg)
	req := HedgeNeeded(trial, m, upBook, downBook, now.Add(2*time.Second), now.Add(5*time.Minute), cfg)
	if !req.Needed || req.Side != "DOWN" || math.Abs(req.Shares-5) > 1e-9 {
		t.Fatalf("unexpected hedge request: %+v", req)
	}
	q := polymarket.BuyQuote{TokenID: "DOWN", AveragePrice: 0.71, Shares: 5, Notional: 3.55, Fee: 0.05, TotalCost: 3.60}
	if err := ApplyHedge(trial, req.Side, q, req.TriggerPrice, req.Reason, now.Add(3*time.Second)); err != nil {
		t.Fatal(err)
	}
	if trial.State != StateHedged {
		t.Fatalf("expected hedged, got %s", trial.State)
	}
	if math.Abs(trial.PaperPnL-(-0.60)) > 1e-9 {
		t.Fatalf("expected -0.60 pnl including hedge fee, got %.6f", trial.PaperPnL)
	}
}
