package engine

import (
	"math"
	"testing"
)

func TestDepthMetricsNotionalAndRanges(t *testing.T) {
	bids := map[float64]float64{100.0: 2.0, 99.0: 3.0, 98.0: 1.0}
	asks := map[float64]float64{101.0: 1.5, 102.0: 2.0, 103.0: 1.0}
	m := CalculateDepthMetrics(bids, asks, nil)
	if math.Abs(m.BidVol-6.0) > 1e-12 || math.Abs(m.AskVol-4.5) > 1e-12 {
		t.Fatalf("unexpected volumes: %+v", m)
	}
	if math.Abs(m.BidNotionalUSD-595.0) > 1e-12 {
		t.Fatalf("bid notional=%f want 595", m.BidNotionalUSD)
	}
	if math.Abs(m.AskNotionalUSD-458.5) > 1e-12 {
		t.Fatalf("ask notional=%f want 458.5", m.AskNotionalUSD)
	}
	if m.BestBid != 100 || m.WorstBid != 98 || m.BestAsk != 101 || m.WorstAsk != 103 {
		t.Fatalf("unexpected book bounds: %+v", m)
	}
	if m.SpreadUSD != 1 || m.BidRangeUSD != 2 || m.AskRangeUSD != 2 {
		t.Fatalf("unexpected dollar ranges: %+v", m)
	}
}

func TestDepthWeightedImbalanceSymmetricBook(t *testing.T) {
	bids := map[float64]float64{}
	asks := map[float64]float64{}
	for i := 0; i < 20; i++ {
		bids[100.0-float64(i)*0.1] = 1.0
		asks[100.1+float64(i)*0.1] = 1.0
	}
	m := CalculateDepthMetrics(bids, asks, nil)
	if math.Abs(m.WeightedImbalance) > 1e-12 {
		t.Fatalf("symmetric book should have zero weighted imbalance, got %f", m.WeightedImbalance)
	}
	if math.Abs(m.Imbalance) > 1e-12 {
		t.Fatalf("symmetric book should have zero raw imbalance, got %f", m.Imbalance)
	}
}

func TestDepthWeightedImbalanceNoDistanceSingularity(t *testing.T) {
	bids := map[float64]float64{99.99: 1.0, 99.98: 1.0, 99.97: 1.0}
	asks := map[float64]float64{100.001: 1.0, 100.02: 1.0, 100.03: 1.0}
	m := CalculateDepthMetrics(bids, asks, nil)
	if math.Abs(m.WeightedImbalance) > 1e-12 {
		t.Fatalf("equal rank sizes should remain balanced regardless of tiny distance asymmetry: %f", m.WeightedImbalance)
	}
}

func TestDepthMetricsSpoofFiltering(t *testing.T) {
	bids := map[float64]float64{100: 100, 99: 1}
	asks := map[float64]float64{101: 1, 102: 1}
	spoof := func(price, size float64, isBid bool) bool {
		return isBid && price == 100 && size == 100
	}
	m := CalculateDepthMetrics(bids, asks, spoof)
	if m.BidVol != 101 {
		t.Fatalf("raw bid volume must retain displayed depth, got %f", m.BidVol)
	}
	if m.FilteredBidVol != 1 {
		t.Fatalf("spoof-filtered bid volume got %f want 1", m.FilteredBidVol)
	}
	if math.Abs(m.WeightedImbalance) > 0.5 {
		t.Fatalf("spoof level must not dominate weighted score: %f", m.WeightedImbalance)
	}
}

func TestRankWeightHalfLife(t *testing.T) {
	if math.Abs(rankWeight(0)-1) > 1e-12 {
		t.Fatal("rank zero must have unit weight")
	}
	if math.Abs(rankWeight(5)-0.5) > 1e-12 {
		t.Fatalf("rank five should have half weight, got %f", rankWeight(5))
	}
}
