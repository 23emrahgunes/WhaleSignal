package storage

import (
	"path/filepath"
	"testing"
)

func TestTimeframeScopedPaperStats(t *testing.T) {
	db, err := NewDatabase(filepath.Join(t.TempDir(), "tf.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	mk := func(slug string, p float64) *PaperTrade {
		return &PaperTrade{MarketSlug: slug, Question: "BTC Up or Down", Side: "UP", EntryTime: "2026-08-11T00:00:00Z", MarketEndTime: "2026-08-11T00:05:00Z", EntryConfidence: 60, EntryFinalScore: .6, EntryProbability: .65, EntryPrice: p, Stake: 2.5, Shares: 2.5 / p, PriceToBeat: 64000, EntryReferencePrice: 64001, Status: "OPEN", Source: "CHAINLINK_RTDS+BINANCE_REST_DEPTH20"}
	}

	five := mk("btc-updown-5m-1786406400", .5)
	created, err := db.CreatePaperTrade(five)
	if err != nil || !created {
		t.Fatalf("create 5m: created=%v err=%v", created, err)
	}
	fifteen := mk("btc-updown-15m-1786406400", .5)
	created, err = db.CreatePaperTrade(fifteen)
	if err != nil || !created {
		t.Fatalf("create 15m: created=%v err=%v", created, err)
	}

	open5, err := db.GetOpenPaperTradesByTimeframe("5m")
	if err != nil || len(open5) != 1 {
		t.Fatalf("5m open=%d err=%v", len(open5), err)
	}
	open15, err := db.GetOpenPaperTradesByTimeframe("15m")
	if err != nil || len(open15) != 1 {
		t.Fatalf("15m open=%d err=%v", len(open15), err)
	}
	if err := db.SettlePaperTrade(open5[0].ID, "2026-08-11T00:05:01Z", 64010, "UP", true, open5[0].Shares, open5[0].Shares-open5[0].Stake); err != nil {
		t.Fatal(err)
	}
	if err := db.SettlePaperTrade(open15[0].ID, "2026-08-11T00:15:01Z", 63990, "DOWN", false, 0, -open15[0].Stake); err != nil {
		t.Fatal(err)
	}

	s5, err := db.GetTimeframeStats(1000, "5m")
	if err != nil {
		t.Fatal(err)
	}
	s15, err := db.GetTimeframeStats(1000, "15m")
	if err != nil {
		t.Fatal(err)
	}
	if s5.SettledTrades != 1 || s5.Wins != 1 || s5.RealizedPnL <= 0 {
		t.Fatalf("unexpected 5m stats: %+v", s5)
	}
	if s15.SettledTrades != 1 || s15.Wins != 0 || s15.RealizedPnL != -2.5 {
		t.Fatalf("unexpected 15m stats: %+v", s15)
	}
	if s5.CashBalance == s15.CashBalance {
		t.Fatalf("timeframe balances are not independent: 5m=%f 15m=%f", s5.CashBalance, s15.CashBalance)
	}
}
