package storage

import (
	"math"
	"path/filepath"
	"testing"
)

func TestPaperInverseStorageLifecycle(t *testing.T) {
	db, err := NewDatabase(filepath.Join(t.TempDir(), "inverse.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.EnsurePaperInverseSchema(); err != nil {
		t.Fatal(err)
	}

	original := &PaperTrade{
		MarketSlug:          "btc-updown-5m-1786569000",
		Question:            "BTC Up or Down",
		Side:                "UP",
		EntryTime:           "2026-08-13T00:30:00Z",
		MarketEndTime:       "2026-08-13T00:35:00Z",
		EntryConfidence:     65,
		EntryFinalScore:     .6,
		EntryProbability:    .75,
		EntryPrice:          .70,
		Stake:               2.5,
		Shares:              2.5 / .70,
		PriceToBeat:         64000,
		EntryReferencePrice: 64010,
		Source:              "CHAINLINK_RTDS+BINANCE_WS",
	}
	created, err := db.CreatePaperTrade(original)
	if err != nil || !created {
		t.Fatalf("create original created=%v err=%v", created, err)
	}
	stored, err := db.GetOpenPaperTradeByMarket(original.MarketSlug)
	if err != nil || stored == nil {
		t.Fatalf("get original %+v err=%v", stored, err)
	}

	inv := &PaperInverseTrade{
		PaperTradeID: stored.ID,
		MarketSlug:   stored.MarketSlug,
		OriginalSide: "UP",
		Side:         "DOWN",
		EntryTime:    stored.EntryTime,
		EntryPrice:   .35,
		Shares:       2.5 / .35,
		Notional:     2.5,
		TotalCost:    2.5,
	}
	created, err = db.CreatePaperInverseTrade(inv)
	if err != nil || !created {
		t.Fatalf("create inverse created=%v err=%v", created, err)
	}

	rows, err := db.GetPaperInverseTradesByTimeframe(10, "5m")
	if err != nil || len(rows) != 1 {
		t.Fatalf("rows=%d err=%v", len(rows), err)
	}
	if rows[0].Side != "DOWN" || rows[0].OriginalSide != "UP" || rows[0].Status != "OPEN" {
		t.Fatalf("unexpected inverse %+v", rows[0])
	}

	payout := rows[0].Shares
	wantPnL := payout - rows[0].TotalCost
	if err := db.SettlePaperInverseTrade(stored.ID, "2026-08-13T00:35:01Z", "DOWN", payout, wantPnL); err != nil {
		t.Fatal(err)
	}
	stats, err := db.GetPaperInverseStatsByTimeframe("5m")
	if err != nil {
		t.Fatal(err)
	}
	if stats.TotalTrades != 1 || stats.SettledTrades != 1 || stats.Wins != 1 || stats.Losses != 0 || math.Abs(stats.RealizedPnL-wantPnL) > 1e-9 {
		t.Fatalf("unexpected stats %+v wantPnL=%.8f", stats, wantPnL)
	}
}
