package storage

import (
	"path/filepath"
	"testing"
	"time"

	"pm-edge/internal/engine"
)

func validSignal() *engine.EvaluationResult {
	now := time.Now().UTC()
	return &engine.EvaluationResult{
		Timestamp: now.Format(time.RFC3339Nano), Question: "Bitcoin Up or Down - 5 Minutes",
		Slug: "btc-updown-5m-1770000000", MarketEndTime: now.Add(2 * time.Minute).Format(time.RFC3339),
		PriceToBeat: 64000, CurrentPrice: 64010, SpotMinusPriceToBeat: 10, SecondsRemaining: 120,
		PUp: 0.55, PDown: 0.45, BidVol: 10.5, AskVol: 8.2, SpoofFilteredBidVol: 10.5,
		SpoofFilteredAskVol: 8.2, Imbalance: 0.12, WeightedImbalance: 0.08,
		ProbabilityScore: 0.1, OrderFlowScore: 0.08, TechnicalScore: 0.2, Volatility: 0.5,
		Drift: 0.01, CompositeScore: 0.12, FinalScore: 0.12, Decision: "NEUTRAL", Confidence: 12,
		MarketStale: false, DataSource: "CHAINLINK_RTDS+BINANCE_WS",
	}
}

func TestDatabaseMigrationInsertAndRead(t *testing.T) {
	path := filepath.Join(t.TempDir(), "signals.sqlite")
	db, err := NewDatabase(path)
	if err != nil {
		t.Fatalf("NewDatabase: %v", err)
	}
	defer db.Close()
	if err := db.InsertSignal(validSignal()); err != nil {
		t.Fatalf("InsertSignal: %v", err)
	}
	history, err := db.GetHistory(10)
	if err != nil {
		t.Fatalf("GetHistory: %v", err)
	}
	if len(history) != 1 {
		t.Fatalf("history length got %d want 1", len(history))
	}
	if history[0].Slug != "btc-updown-5m-1770000000" || history[0].DataSource != "CHAINLINK_RTDS+BINANCE_WS" {
		t.Fatalf("unexpected history row: %#v", history[0])
	}
}

func TestInsertSignalRejectsInvalidRows(t *testing.T) {
	path := filepath.Join(t.TempDir(), "signals.sqlite")
	db, err := NewDatabase(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	cases := []struct {
		name   string
		mutate func(*engine.EvaluationResult)
	}{
		{"nil-ptb", func(r *engine.EvaluationResult) { r.PriceToBeat = 0 }},
		{"legacy-slug", func(r *engine.EvaluationResult) { r.Slug = "btc-above-100k-1505" }},
		{"stale", func(r *engine.EvaluationResult) { r.MarketStale = true }},
		{"mock", func(r *engine.EvaluationResult) { r.DataSource = "CHAINLINK_RTDS+MOCK" }},
		{"expired", func(r *engine.EvaluationResult) { r.SecondsRemaining = 0 }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := validSignal()
			tc.mutate(r)
			if err := db.InsertSignal(r); err == nil {
				t.Fatalf("expected rejection")
			}
		})
	}
}

func TestMigrationPurgesExactLegacySyntheticFallback(t *testing.T) {
	path := filepath.Join(t.TempDir(), "signals.sqlite")
	db, err := NewDatabase(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := db.InsertSignal(validSignal()); err != nil {
		t.Fatal(err)
	}
	if _, err := db.db.Exec(`UPDATE signals SET slug='btc-above-100k-1505', price_to_beat=100000`); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	db2, err := NewDatabase(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db2.Close()
	history, err := db2.GetHistory(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(history) != 0 {
		t.Fatalf("legacy synthetic row survived migration: %#v", history)
	}
}

func TestDatabaseSerializesSQLiteConnectionsAndSetsBusyTimeout(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sqlite-locking.sqlite")
	db, err := NewDatabase(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if got := db.db.Stats().MaxOpenConnections; got != 1 {
		t.Fatalf("MaxOpenConnections=%d want 1", got)
	}
	var busy int
	if err := db.db.QueryRow("PRAGMA busy_timeout").Scan(&busy); err != nil {
		t.Fatal(err)
	}
	if busy < 5000 {
		t.Fatalf("busy_timeout=%d want >=5000", busy)
	}
}
