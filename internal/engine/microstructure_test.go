package engine

import (
	"testing"

	"pm-edge/internal/binance"
)

func TestMicrostructureScoreUsesDeepAndExecutedFlow(t *testing.T) {
	s := binance.DeepMicroSnapshot{
		Ready:              true,
		TradeFlowAvailable: true,
		Bands: []binance.DeepBand{
			{DistanceUSD: 10, Imbalance: 0.2},
			{DistanceUSD: 25, Imbalance: 0.3},
			{DistanceUSD: 50, Imbalance: 0.5},
			{DistanceUSD: 75, Imbalance: 0.6},
		},
		Trades: []binance.TradeWindow{
			{Seconds: 5, Imbalance: 0.8},
			{Seconds: 15, Imbalance: 0.6},
			{Seconds: 30, Imbalance: 0.4},
			{Seconds: 60, Imbalance: 0.2},
		},
		TradeAcceleration: 0.4,
		BidWallScore:      0.5,
		AskWallScore:      0.1,
		AskDepletionScore: 0.4,
		BidDepletionScore: 0.1,
		PTBBarrierScore:   0.3,
	}
	m := ScoreMicrostructure(s)
	if !m.Ready || m.DeepBookScore <= 0 || m.TradeFlowScore <= 0 || m.MicrostructureScore <= 0 {
		t.Fatalf("unexpected scores %+v", m)
	}
	score := ShadowModelB(0.4, 0.1, m)
	decision, confidence := ShadowDecision(score)
	if decision != "UP" || confidence <= 0 {
		t.Fatalf("expected UP shadow score, got %s %.4f", decision, confidence)
	}
}

func TestMicrostructureNotReadyWithoutTradeFlow(t *testing.T) {
	m := ScoreMicrostructure(binance.DeepMicroSnapshot{Ready: true, TradeFlowAvailable: false})
	if m.Ready {
		t.Fatal("expected shadow microstructure to wait for executed trade flow")
	}
}

func TestBinanceEquivalentPTBRemovesChainlinkBasis(t *testing.T) {
	got := binanceEquivalentPTB(63475, 63480, 63534)
	want := 63529.0
	if got != want {
		t.Fatalf("got %.2f want %.2f", got, want)
	}
}
