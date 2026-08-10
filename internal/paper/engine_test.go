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

func TestPaperEntryDedupeAndWinningSettlement(t *testing.T) {
	db, err := storage.NewDatabase(filepath.Join(t.TempDir(), "paper.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	now := time.Unix(1786390000, 0).UTC()
	end := now.Add(90 * time.Second)
	market := &polymarket.Market{
		Question:  "Bitcoin Up or Down - 5 Minutes",
		EventSlug: "btc-updown-5m-1786389900",
		Active:    true,
		StartTime: end.Add(-5 * time.Minute),
		EndTime:   end,
		Outcomes:  []string{"Up", "Down"},
		Tokens: []polymarket.Token{
			{Outcome: "Up", Price: 0.60},
			{Outcome: "Down", Price: 0.40},
		},
	}
	res := &engine.EvaluationResult{
		Slug:             market.EventSlug,
		Question:         market.Question,
		MarketEndTime:    end.Format(time.RFC3339),
		PriceToBeat:      64000,
		CurrentPrice:     64020,
		SecondsRemaining: 90,
		PUp:              0.72,
		PDown:            0.28,
		FinalScore:       0.61,
		Decision:         "UP",
		Confidence:       61,
		DataSource:       "CHAINLINK_RTDS+BINANCE_REST+BINANCE_REST_DEPTH20",
	}
	pe := NewEngine(db, Config{Enabled: true, InitialBalance: 1000, Stake: 2.5, MinConfidence: 55, MinSecondsToEnd: 30, MaxSecondsToEnd: 240})
	trade, opened, err := pe.MaybeOpen(res, market, now)
	if err != nil || !opened {
		t.Fatalf("MaybeOpen opened=%v err=%v", opened, err)
	}
	if math.Abs(trade.Shares-(2.5/0.60)) > 1e-9 {
		t.Fatalf("shares %.8f", trade.Shares)
	}
	if _, openedAgain, err := pe.MaybeOpen(res, market, now.Add(time.Second)); err != nil || openedAgain {
		t.Fatalf("duplicate market should not open twice, opened=%v err=%v", openedAgain, err)
	}
	stats, err := db.GetPaperStats(1000)
	if err != nil {
		t.Fatal(err)
	}
	if stats.OpenTrades != 1 || math.Abs(stats.CashBalance-997.5) > 1e-9 {
		t.Fatalf("unexpected open stats %#v", stats)
	}

	settled, err := pe.SettleReady(end.Add(time.Second), func(boundary time.Time) (float64, bool) {
		if !boundary.Equal(end) {
			t.Fatalf("unexpected boundary %s", boundary)
		}
		return 64010, true
	})
	if err != nil || settled != 1 {
		t.Fatalf("settled=%d err=%v", settled, err)
	}
	trades, err := db.GetPaperTrades(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(trades) != 1 || trades[0].Status != "SETTLED" || !trades[0].Won || trades[0].Outcome != "UP" {
		t.Fatalf("unexpected settled trade %#v", trades)
	}
	wantPnL := 2.5/0.60 - 2.5
	if math.Abs(trades[0].PnL-wantPnL) > 1e-9 {
		t.Fatalf("pnl %.8f want %.8f", trades[0].PnL, wantPnL)
	}
	stats, err = db.GetPaperStats(1000)
	if err != nil {
		t.Fatal(err)
	}
	if stats.Wins != 1 || stats.Losses != 0 || stats.OpenTrades != 0 || math.Abs(stats.RealizedPnL-wantPnL) > 1e-9 {
		t.Fatalf("unexpected settled stats %#v", stats)
	}
}

func TestPaperEntryThresholds(t *testing.T) {
	db, err := storage.NewDatabase(filepath.Join(t.TempDir(), "paper.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	now := time.Now().UTC()
	market := &polymarket.Market{Question: "BTC", EventSlug: "btc-updown-5m-1786389900", Active: true, EndTime: now.Add(90 * time.Second), Tokens: []polymarket.Token{{Outcome: "Up", Price: .5}}}
	res := &engine.EvaluationResult{PriceToBeat: 64000, CurrentPrice: 64001, SecondsRemaining: 90, PUp: .6, Decision: "UP", Confidence: 54.9, DataSource: "CHAINLINK_RTDS+BINANCE_WS+BINANCE_WS_DEPTH20"}
	pe := NewEngine(db, Config{Enabled: true, InitialBalance: 1000, Stake: 2.5, MinConfidence: 55, MinSecondsToEnd: 30, MaxSecondsToEnd: 240})
	if _, opened, err := pe.MaybeOpen(res, market, now); err != nil || opened {
		t.Fatalf("below threshold opened=%v err=%v", opened, err)
	}
}
