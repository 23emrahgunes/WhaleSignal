package storage

import (
	"math"
	"path/filepath"
	"testing"
)

func TestPaperStatsExposeProbabilityCalibration(t *testing.T) {
	db, err := NewDatabase(filepath.Join(t.TempDir(), "calibration.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	trades := []PaperTrade{
		{
			MarketSlug: "btc-updown-5m-100", Question: "BTC", Side: "UP",
			EntryTime: "2026-08-11T00:00:00Z", MarketEndTime: "2026-08-11T00:05:00Z",
			EntryConfidence: 70, EntryFinalScore: 0.7, EntryProbability: 0.8,
			EntryPrice: 0.4, Stake: 2.5, Shares: 6.25, PriceToBeat: 64000,
			EntryReferencePrice: 64010, Source: "CHAINLINK_RTDS+BINANCE_WS+BINANCE_WS_DEPTH20",
		},
		{
			MarketSlug: "btc-updown-5m-200", Question: "BTC", Side: "DOWN",
			EntryTime: "2026-08-11T00:05:00Z", MarketEndTime: "2026-08-11T00:10:00Z",
			EntryConfidence: 60, EntryFinalScore: -0.6, EntryProbability: 0.6,
			EntryPrice: 0.5, Stake: 2.5, Shares: 5, PriceToBeat: 64000,
			EntryReferencePrice: 63990, Source: "CHAINLINK_RTDS+BINANCE_WS+BINANCE_WS_DEPTH20",
		},
	}
	for i := range trades {
		created, err := db.CreatePaperTrade(&trades[i])
		if err != nil || !created {
			t.Fatalf("create trade %d created=%v err=%v", i, created, err)
		}
	}
	persisted, err := db.GetPaperTrades(10)
	if err != nil || len(persisted) != 2 {
		t.Fatalf("persisted trades=%d err=%v", len(persisted), err)
	}
	ids := map[string]int64{}
	for _, tr := range persisted {
		ids[tr.MarketSlug] = tr.ID
	}
	if err := db.SettlePaperTrade(ids["btc-updown-5m-100"], "2026-08-11T00:05:01Z", 64020, "UP", true, 6.25, 3.75); err != nil {
		t.Fatal(err)
	}
	if err := db.SettlePaperTrade(ids["btc-updown-5m-200"], "2026-08-11T00:10:01Z", 64020, "UP", false, 0, -2.5); err != nil {
		t.Fatal(err)
	}

	stats, err := db.GetPaperStats(1000)
	if err != nil {
		t.Fatal(err)
	}
	if stats.CalibrationN != 2 {
		t.Fatalf("calibrationN=%d", stats.CalibrationN)
	}
	if math.Abs(stats.AverageEntryProbability-0.7) > 1e-12 {
		t.Fatalf("avg predicted=%f", stats.AverageEntryProbability)
	}
	if math.Abs(stats.ActualWinProbability-0.5) > 1e-12 {
		t.Fatalf("actual=%f", stats.ActualWinProbability)
	}
	if math.Abs(stats.CalibrationGap-(-0.2)) > 1e-12 {
		t.Fatalf("calibration gap=%f", stats.CalibrationGap)
	}
	if math.Abs(stats.BrierScore-0.20) > 1e-12 {
		t.Fatalf("brier=%f", stats.BrierScore)
	}
	if math.Abs(stats.ExpectedWins-1.4) > 1e-12 {
		t.Fatalf("expectedWins=%f", stats.ExpectedWins)
	}
}
