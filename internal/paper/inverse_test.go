package paper

import (
	"math"
	"path/filepath"
	"testing"
	"time"

	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
)

func TestProductionPaperEntryCreatesAndSettlesInverseAB(t *testing.T) {
	db, err := storage.NewDatabase(filepath.Join(t.TempDir(), "paper-inverse.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.EnsurePaperHedgeSchema(); err != nil {
		t.Fatal(err)
	}

	now := time.Unix(1786569000, 0).UTC()
	end := now.Add(90 * time.Second)
	market := &polymarket.Market{
		Question:  "Bitcoin Up or Down - 5 Minutes",
		EventSlug: "btc-updown-5m-1786569000",
		Active:    true,
		StartTime: now.Add(-210 * time.Second),
		EndTime:   end,
		Outcomes:  []string{"Up", "Down"},
		Tokens: []polymarket.Token{
			{Outcome: "Up", Price: .70, TokenID: "up-token"},
			{Outcome: "Down", Price: .30, TokenID: "down-token"},
		},
	}
	res := &engine.EvaluationResult{
		Slug:             market.EventSlug,
		Question:         market.Question,
		MarketEndTime:    end.Format(time.RFC3339),
		PriceToBeat:      64000,
		CurrentPrice:     64020,
		SecondsRemaining: 90,
		PUp:              .80,
		PDown:            .20,
		FinalScore:       .62,
		Decision:         "UP",
		Confidence:       66,
		DataSource:       "CHAINLINK_RTDS+BINANCE_WS+BINANCE_DEEP_DIFF",
		PTBTerminal: engine.PTBTerminalEstimate{
			Ready: true, Decision: "UP", PAbove: .90, PBelow: .10,
		},
	}

	pe := NewEngine(db, Config{
		Timeframe: "5m", Enabled: true, InitialBalance: 1000, Stake: 2.5,
		MinConfidence: 55, MinSecondsToEnd: 30, MaxSecondsToEnd: 240,
		MaxEffectiveEntry: .85, MinEconomicEdge: .05,
	})
	quote := func(tokenID string, budget float64) (polymarket.BuyQuote, error) {
		price := .70
		if tokenID == "down-token" {
			price = .35
		}
		shares := budget / price
		return polymarket.BuyQuote{
			BestAsk: price, AveragePrice: price, Shares: shares,
			Notional: budget, TotalCost: budget,
		}, nil
	}

	trade, opened, err := pe.MaybeOpenWithQuote(res, market, now, quote)
	if err != nil || !opened {
		t.Fatalf("open=%v trade=%+v err=%v", opened, trade, err)
	}
	if trade.Side != "UP" || math.Abs(trade.EntryPrice-.70) > 1e-9 {
		t.Fatalf("unexpected original %+v", trade)
	}
	inv, err := db.GetPaperInverseByTradeID(trade.ID)
	if err != nil || inv == nil {
		t.Fatalf("inverse=%+v err=%v", inv, err)
	}
	if inv.Side != "DOWN" || inv.OriginalSide != "UP" || math.Abs(inv.EntryPrice-.35) > 1e-9 || math.Abs(inv.TotalCost-2.5) > 1e-9 {
		t.Fatalf("unexpected inverse %+v", inv)
	}

	settled, err := pe.SettleReady(end.Add(time.Second), func(boundary time.Time) (float64, bool) {
		if !boundary.Equal(end) {
			t.Fatalf("unexpected boundary %s", boundary)
		}
		return 63990, true
	})
	if err != nil || settled != 1 {
		t.Fatalf("settled=%d err=%v", settled, err)
	}

	originalRows, err := db.GetPaperTradesByTimeframe(10, "5m")
	if err != nil || len(originalRows) != 1 {
		t.Fatalf("original rows=%d err=%v", len(originalRows), err)
	}
	if originalRows[0].Won || math.Abs(originalRows[0].PnL+2.5) > 1e-9 {
		t.Fatalf("original settlement %+v", originalRows[0])
	}
	inverseRows, err := db.GetPaperInverseTradesByTimeframe(10, "5m")
	if err != nil || len(inverseRows) != 1 {
		t.Fatalf("inverse rows=%d err=%v", len(inverseRows), err)
	}
	wantInversePnL := 2.5/.35 - 2.5
	if !inverseRows[0].Won || math.Abs(inverseRows[0].PnL-wantInversePnL) > 1e-9 {
		t.Fatalf("inverse settlement %+v wantPnL=%.8f", inverseRows[0], wantInversePnL)
	}
}
