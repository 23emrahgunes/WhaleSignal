package engine

import (
	"testing"
	"time"

	"pm-edge/internal/binance"
	"pm-edge/internal/polymarket"
)

func TestEvaluatorWithVerifiedInputs(t *testing.T) {
	ev := NewEvaluator()
	bc := binance.NewClient()
	now := time.Now().UTC()

	bids := [][]string{{"99990", "2.0"}}
	asks := [][]string{{"100010", "4.0"}}
	bc.UpdateDepth(bids, asks, now)
	bc.UpdateFromTrade(100000, 1, now, true)

	market := &polymarket.Market{
		Question:    "BTC Up or Down - 5 Minutes",
		EventSlug:   "btc-updown-5m-test",
		PriceToBeat: 99950,
		StartTime:   now.Add(-time.Minute),
		EndTime:     now.Add(4 * time.Minute),
		Active:      true,
		Closed:      false,
	}

	res := ev.Evaluate(bc, market, 100000, true, now.Format(time.RFC3339))
	if res == nil {
		t.Fatal("expected evaluation result, got nil")
	}
	if res.CurrentPrice != 100000 {
		t.Errorf("expected Chainlink current price 100000, got %f", res.CurrentPrice)
	}
	if res.PriceToBeat != 99950 {
		t.Errorf("expected Chainlink price-to-beat 99950, got %f", res.PriceToBeat)
	}
	if res.DataSource != "CHAINLINK_RTDS+BINANCE_WS" {
		t.Errorf("unexpected data source: %s", res.DataSource)
	}
}

func TestEvaluatorRejectsMissingOrStaleReference(t *testing.T) {
	ev := NewEvaluator()
	bc := binance.NewClient()
	now := time.Now().UTC()
	bc.UpdateFromTrade(100000, 1, now, true)
	market := &polymarket.Market{
		PriceToBeat: 99950,
		StartTime:   now.Add(-time.Minute),
		EndTime:     now.Add(4 * time.Minute),
		Active:      true,
	}

	if got := ev.Evaluate(bc, nil, 100000, true, now.Format(time.RFC3339)); got != nil {
		t.Fatal("nil market must produce NO_SIGNAL")
	}
	if got := ev.Evaluate(bc, market, 100000, false, now.Format(time.RFC3339)); got != nil {
		t.Fatal("stale Chainlink reference must produce NO_SIGNAL")
	}
	market.PriceToBeat = 0
	if got := ev.Evaluate(bc, market, 100000, true, now.Format(time.RFC3339)); got != nil {
		t.Fatal("missing price-to-beat must produce NO_SIGNAL")
	}
}
