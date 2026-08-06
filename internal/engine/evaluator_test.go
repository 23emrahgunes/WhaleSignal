package engine

import (
	"testing"
	"time"

	"pm-edge/internal/binance"
	"pm-edge/internal/polymarket"
)

func TestEvaluator(t *testing.T) {
	ev := NewEvaluator()
	bc := binance.NewClient()

	bids := [][]string{
		{"99000", "2.0"},
	}
	asks := [][]string{
		{"101000", "4.0"},
	}
	bc.UpdateDepth(bids, asks, time.Now().UTC())

	bc.UpdateFromTrade(100000.0, 1.0, time.Now().UTC(), true)
	bc.UpdateFromTrade(100100.0, 1.5, time.Now().UTC(), true)

	market := &polymarket.Market{
		PriceToBeat: 100200.0,
		EndTime:     time.Now().UTC().Add(5 * time.Minute),
	}

	res := ev.Evaluate(bc, market, time.Now().UTC().Format(time.RFC3339), 1.0)
	if res == nil {
		t.Fatal("Expected evaluation result, got nil")
	}

	if res.CurrentPrice != 100100.0 {
		t.Errorf("Expected current price 100100.0, got %f", res.CurrentPrice)
	}

	if res.PriceToBeat != 100200.0 {
		t.Errorf("Expected price to beat 100200.0, got %f", res.PriceToBeat)
	}
}
