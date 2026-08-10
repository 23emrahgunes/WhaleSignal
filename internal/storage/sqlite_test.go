package storage

import (
	"os"
	"testing"
	"time"

	"pm-edge/internal/engine"
)

func TestDatabaseMigrationAndInsert(t *testing.T) {
	tempFile := "data/test_db.sqlite"
	defer os.Remove(tempFile)

	db, err := NewDatabase(tempFile)
	if err != nil {
		t.Fatalf("Failed to initialize database: %v", err)
	}
	defer db.Close()

	res := &engine.EvaluationResult{
		Timestamp:            time.Now().UTC().Format(time.RFC3339),
		Question:             "BTC above $100k?",
		Slug:                 "btc-above-100k",
		MarketEndTime:        time.Now().UTC().Add(150 * time.Second).Format(time.RFC3339),
		PriceToBeat:          100000.0,
		CurrentPrice:         99500.0,
		SpotMinusPriceToBeat: -500.0,
		SecondsRemaining:     120.0,
		PUp:                  0.35,
		PDown:                0.65,
		BidVol:               10.5,
		AskVol:               8.2,
		SpoofFilteredBidVol:  10.5,
		SpoofFilteredAskVol:  8.2,
		Imbalance:            0.12,
		WeightedImbalance:    0.08,
		ProbabilityScore:     -0.3,
		OrderFlowScore:       0.08,
		TechnicalScore:       0.5,
		Volatility:           0.15,
		Drift:                0.05,
		CompositeScore:       0.15,
		FinalScore:           0.15,
		Decision:             "NEUTRAL",
		Confidence:           15.0,
		MarketStale:          false,
		DataSource:           "BINANCE_WS",
	}

	err = db.InsertSignal(res)
	if err != nil {
		t.Fatalf("Failed to insert signal: %v", err)
	}

	history, err := db.GetHistory(10)
	if err != nil {
		t.Fatalf("Failed to retrieve history: %v", err)
	}

	if len(history) != 1 {
		t.Errorf("Expected history length 1, got %d", len(history))
	}

	if history[0].CurrentPrice != 99500.0 {
		t.Errorf("Expected current price 99500.0, got %f", history[0].CurrentPrice)
	}

	if history[0].DataSource != "BINANCE_WS" {
		t.Errorf("Expected data_source BINANCE_WS, got %q", history[0].DataSource)
	}
}
