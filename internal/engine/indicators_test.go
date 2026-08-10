package engine

import (
	"testing"
	"time"

	"pm-edge/internal/binance"
)

func TestIndicatorsBasic(t *testing.T) {
	// Generate dummy candles
	candles := make([]binance.Candle, 60)
	price := 100.0
	for i := 0; i < 60; i++ {
		price += 1.0 // upward trend
		candles[i] = binance.Candle{
			StartTime: time.Now().Add(time.Duration(-60+i) * time.Minute),
			Open:      price - 0.5,
			High:      price + 1.0,
			Low:       price - 1.0,
			Close:     price,
			Volume:    10.0,
		}
	}

	scores := GetIndicatorScores(candles, candles)
	if len(scores) == 0 {
		t.Errorf("Expected indicator scores, got empty map")
	}

	// Because of upward trend, SMA, MACD, etc. should ideally be positive (+1)
	if scores["SMA"] != 1 {
		t.Errorf("Expected SMA to be bullish (1), got %d", scores["SMA"])
	}
}
