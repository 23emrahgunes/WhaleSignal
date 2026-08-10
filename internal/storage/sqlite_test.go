package storage

import (
	"os"
	"testing"
	"time"

	"pm-edge/internal/engine"
)

func verifiedResult() *engine.EvaluationResult {
	now := time.Now().UTC()
	return &engine.EvaluationResult{
		Timestamp:            now.Format(time.RFC3339),
		Question:             "BTC Up or Down - 5 Minutes",
		Slug:                 "btc-updown-5m-1800000000",
		MarketEndTime:        now.Add(150 * time.Second).Format(time.RFC3339),
		PriceToBeat:          100000,
		CurrentPrice:         100050,
		SpotMinusPriceToBeat: 50,
		SecondsRemaining:     120,
		PUp:                  0.55,
		PDown:                0.45,
		BidVol:               10.5,
		AskVol:               8.2,
		SpoofFilteredBidVol:  10.5,
		SpoofFilteredAskVol:  8.2,
		Imbalance:            0.12,
		WeightedImbalance:    0.08,
		ProbabilityScore:     0.10,
		OrderFlowScore:       0.08,
		TechnicalScore:       0.5,
		Volatility:           0.15,
		Drift:                0.05,
		CompositeScore:       0.15,
		FinalScore:           0.15,
		Decision:             "NEUTRAL",
		Confidence:           15,
		MarketStale:          false,
		DataSource:           "CHAINLINK_RTDS+BINANCE_WS",
	}
}

func TestDatabaseMigrationAndInsert(t *testing.T) {
	tempFile := "data/test_db.sqlite"
	_ = os.Remove(tempFile)
	defer os.Remove(tempFile)

	db, err := NewDatabase(tempFile)
	if err != nil {
		t.Fatalf("Failed to initialize database: %v", err)
	}
	defer db.Close()

	res := verifiedResult()
	if err := db.InsertSignal(res); err != nil {
		t.Fatalf("Failed to insert verified signal: %v", err)
	}

	history, err := db.GetHistory(10)
	if err != nil {
		t.Fatalf("Failed to retrieve history: %v", err)
	}
	if len(history) != 1 {
		t.Fatalf("Expected history length 1, got %d", len(history))
	}
	if history[0].DataSource != "CHAINLINK_RTDS+BINANCE_WS" {
		t.Fatalf("unexpected data source: %q", history[0].DataSource)
	}
}

func TestDatabaseRejectsSyntheticFallbackSignal(t *testing.T) {
	tempFile := "data/test_reject.sqlite"
	_ = os.Remove(tempFile)
	defer os.Remove(tempFile)

	db, err := NewDatabase(tempFile)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	res := verifiedResult()
	res.Slug = "btc-above-100k-1505"
	res.Question = "BTC above $100,000 at 15:05?"
	res.DataSource = "BINANCE_REST"
	if err := db.InsertSignal(res); err == nil {
		t.Fatal("synthetic fallback signal must be rejected")
	}
}
