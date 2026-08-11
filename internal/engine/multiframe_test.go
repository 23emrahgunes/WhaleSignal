package engine

import (
	"testing"
	"time"

	"pm-edge/internal/polymarket"
)

func TestEvaluatorSupportsFullBTC15mHorizon(t *testing.T) {
	bc, _, now := readyInputs()
	m := &polymarket.Market{
		Question:    "Bitcoin Up or Down - 15 Minutes",
		EventSlug:   "btc-updown-15m-test",
		Active:      true,
		StartTime:   now.Add(-2 * time.Minute),
		EndTime:     now.Add(13 * time.Minute),
		PriceToBeat: 99950,
	}
	res := NewEvaluator().Evaluate(bc, m, 100123.45, true, now.Format(time.RFC3339Nano))
	if res == nil {
		t.Fatal("15m market with 13 minutes remaining must evaluate; horizon must not be hard-capped at 305 seconds")
	}
	if res.SecondsRemaining < 12*60 || res.SecondsRemaining > 14*60 {
		t.Fatalf("unexpected remaining seconds %.2f", res.SecondsRemaining)
	}
	if !res.ForecastReady || res.ForecastSigmaExpiryBps <= 0 {
		t.Fatalf("15m forecast not ready: %+v", res)
	}
}
