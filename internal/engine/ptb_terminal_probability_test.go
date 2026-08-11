package engine

import (
	"testing"

	"pm-edge/internal/binance"
)

func terminalSnapshot() binance.DeepMicroSnapshot {
	return binance.DeepMicroSnapshot{
		Ready: true, Synchronized: true, TradeFlowAvailable: true,
		PTBCorridorCovered: true, PTBPathBidUSD: 4_000_000, PTBPathAskUSD: 2_000_000, PTBBeyondUSD: 1_000_000,
		Trades: []binance.TradeWindow{
			{Seconds: 15, BuyUSD: 3_000_000, SellUSD: 1_000_000, Imbalance: 0.5},
			{Seconds: 30, BuyUSD: 5_000_000, SellUSD: 2_000_000, Imbalance: 0.4286},
			{Seconds: 60, BuyUSD: 8_000_000, SellUSD: 4_000_000, Imbalance: 0.3333},
		},
	}
}

func TestPTBTerminalBullishMicroRaisesPrior(t *testing.T) {
	s := terminalSnapshot()
	m := MicrostructureScores{Ready: true, PTBBarrierScore: 0.6, TradeFlowScore: 0.5, DeepBookScore: 0.3, WallDynamicsScore: 0.2}
	got := EstimatePTBTerminalMicroProbability(0.60, 60, 100, 105, s, m)
	if !got.Ready || got.PAbove <= 0.60 || got.FlowCapacityScore <= 0 {
		t.Fatalf("bullish microstructure did not raise prior: %+v", got)
	}
}

func TestPTBTerminalBearishMicroLowersPrior(t *testing.T) {
	s := terminalSnapshot()
	s.PTBPathBidUSD, s.PTBPathAskUSD = 1_000_000, 7_000_000
	s.Trades = []binance.TradeWindow{
		{Seconds: 15, BuyUSD: 300_000, SellUSD: 2_000_000, Imbalance: -0.739},
		{Seconds: 30, BuyUSD: 700_000, SellUSD: 4_000_000, Imbalance: -0.702},
		{Seconds: 60, BuyUSD: 1_200_000, SellUSD: 7_000_000, Imbalance: -0.707},
	}
	m := MicrostructureScores{Ready: true, PTBBarrierScore: -0.7, TradeFlowScore: -0.7, DeepBookScore: -0.4, WallDynamicsScore: -0.2}
	got := EstimatePTBTerminalMicroProbability(0.60, 60, 100, 105, s, m)
	if !got.Ready || got.PAbove >= 0.60 || got.FlowCapacityScore >= 0 {
		t.Fatalf("bearish microstructure did not lower prior: %+v", got)
	}
}

func TestPTBTerminalRequiresFullCorridorCoverage(t *testing.T) {
	s := terminalSnapshot()
	s.PTBCorridorCovered = false
	got := EstimatePTBTerminalMicroProbability(0.70, 60, 100, 180, s, MicrostructureScores{Ready: true})
	if got.Ready || got.PAbove != got.PriorPAbove {
		t.Fatalf("uncovered corridor should stay at prior and not be ready: %+v", got)
	}
}

func TestPTBTerminalNearExpiryAmplifiesSameEvidence(t *testing.T) {
	s := terminalSnapshot()
	m := MicrostructureScores{Ready: true, PTBBarrierScore: 0.4, TradeFlowScore: 0.4, DeepBookScore: 0.2, WallDynamicsScore: 0.1}
	near := EstimatePTBTerminalMicroProbability(0.50, 30, 100, 105, s, m)
	far := EstimatePTBTerminalMicroProbability(0.50, 300, 100, 105, s, m)
	if !near.Ready || !far.Ready || near.PAbove <= far.PAbove {
		t.Fatalf("near-expiry evidence should have larger effect: near=%+v far=%+v", near, far)
	}
}
