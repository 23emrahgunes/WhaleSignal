package storage

import (
	"path/filepath"
	"testing"

	"pm-edge/internal/binance"
	"pm-edge/internal/engine"
)

func TestMicrostructureSnapshotRoundTrip(t *testing.T) {
	db, err := NewDatabase(filepath.Join(t.TempDir(), "micro.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.EnsureMicrostructureSchema(); err != nil {
		t.Fatal(err)
	}
	r := &engine.EvaluationResult{
		Timestamp: "2026-08-11T15:00:00Z",
		Slug:      "btc-updown-5m-1786450800",
		DeepMicrostructure: binance.DeepMicroSnapshot{
			Ready: true, Synchronized: true, Source: "BINANCE_DEEP_DIFF", AgeMs: 25, BidLevels: 4900, AskLevels: 4800,
			Bands: []binance.DeepBand{{DistanceUSD: 10, BidUSD: 1000, AskUSD: 800, Imbalance: 0.111}, {DistanceUSD: 50, BidUSD: 5000, AskUSD: 7000, Imbalance: -0.167}},
			Trades: []binance.TradeWindow{{Seconds: 5, BuyUSD: 2000, SellUSD: 1000, Imbalance: 0.333}, {Seconds: 60, BuyUSD: 9000, SellUSD: 11000, Imbalance: -0.1}},
			TradeAcceleration: 0.2, BidWallScore: 0.3, AskWallScore: 0.4, BidDepletionScore: 0.1, AskDepletionScore: 0.5,
			PTBPathBidUSD: 3000, PTBPathAskUSD: 6000, PTBBeyondUSD: 2000,
		},
		DeepBookScore: 0.12, TradeFlowScore: 0.22, WallDynamicsScore: 0.32, PTBBarrierScore: -0.42,
		MicrostructureScore: 0.08, ShadowModelBScore: 0.24, ShadowDecision: "UP", ShadowConfidence: 24,
	}
	if err := db.InsertMicrostructureSnapshot(r); err != nil {
		t.Fatal(err)
	}
	rows, err := db.GetMicrostructureSnapshots(10, "5m")
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("expected one row, got %d", len(rows))
	}
	got := rows[0]
	if got.Band10BidUSD != 1000 || got.Trade5BuyUSD != 2000 || got.ShadowDecision != "UP" || got.PTBBarrierScore != -0.42 {
		t.Fatalf("unexpected round trip %+v", got)
	}
}
