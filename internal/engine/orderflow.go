package engine

import (
	"math"
	"sort"
)

const depthRankHalfLife = 5.0

type DepthMetrics struct {
	BidVol            float64
	AskVol            float64
	FilteredBidVol    float64
	FilteredAskVol    float64
	BidNotionalUSD    float64
	AskNotionalUSD    float64
	TotalNotionalUSD  float64
	BestBid           float64
	BestAsk           float64
	WorstBid          float64
	WorstAsk          float64
	MidPrice          float64
	SpreadUSD         float64
	SpreadBps         float64
	BidRangeUSD       float64
	AskRangeUSD       float64
	BidRangeBps       float64
	AskRangeBps       float64
	Imbalance         float64
	WeightedImbalance float64
}

type depthLevel struct {
	price float64
	size  float64
}

// CalculateDepthMetrics summarizes Depth20 without the 1/distance^2 singularity.
// Rank weights have a five-level half-life, so the top of book matters more while
// a one-cent distance difference cannot dominate the complete order book.
func CalculateDepthMetrics(bids, asks map[float64]float64, isSpoofing func(price, size float64, isBid bool) bool) DepthMetrics {
	m := DepthMetrics{}
	bidLevels := sortedDepthLevels(bids, true)
	askLevels := sortedDepthLevels(asks, false)
	if len(bidLevels) == 0 || len(askLevels) == 0 {
		return m
	}

	m.BestBid = bidLevels[0].price
	m.WorstBid = bidLevels[len(bidLevels)-1].price
	m.BestAsk = askLevels[0].price
	m.WorstAsk = askLevels[len(askLevels)-1].price
	m.MidPrice = 0.5 * (m.BestBid + m.BestAsk)
	m.SpreadUSD = math.Max(0, m.BestAsk-m.BestBid)
	m.BidRangeUSD = math.Max(0, m.BestBid-m.WorstBid)
	m.AskRangeUSD = math.Max(0, m.WorstAsk-m.BestAsk)
	if m.MidPrice > 0 {
		m.SpreadBps = m.SpreadUSD / m.MidPrice * 10000.0
		m.BidRangeBps = m.BidRangeUSD / m.MidPrice * 10000.0
		m.AskRangeBps = m.AskRangeUSD / m.MidPrice * 10000.0
	}

	weightedBid := 0.0
	weightedAsk := 0.0
	for rank, level := range bidLevels {
		m.BidVol += level.size
		m.BidNotionalUSD += level.price * level.size
		if isSpoofing != nil && isSpoofing(level.price, level.size, true) {
			continue
		}
		m.FilteredBidVol += level.size
		weightedBid += level.size * rankWeight(rank)
	}
	for rank, level := range askLevels {
		m.AskVol += level.size
		m.AskNotionalUSD += level.price * level.size
		if isSpoofing != nil && isSpoofing(level.price, level.size, false) {
			continue
		}
		m.FilteredAskVol += level.size
		weightedAsk += level.size * rankWeight(rank)
	}
	m.TotalNotionalUSD = m.BidNotionalUSD + m.AskNotionalUSD

	if total := m.FilteredBidVol + m.FilteredAskVol; total > 0 {
		m.Imbalance = (m.FilteredBidVol - m.FilteredAskVol) / total
	}
	if total := weightedBid + weightedAsk; total > 0 {
		m.WeightedImbalance = (weightedBid - weightedAsk) / total
	}
	return m
}

func rankWeight(rank int) float64 {
	if rank <= 0 {
		return 1.0
	}
	return math.Exp(-math.Ln2 * float64(rank) / depthRankHalfLife)
}

func sortedDepthLevels(levels map[float64]float64, descending bool) []depthLevel {
	result := make([]depthLevel, 0, len(levels))
	for price, size := range levels {
		if price > 0 && size > 0 {
			result = append(result, depthLevel{price: price, size: size})
		}
	}
	sort.Slice(result, func(i, j int) bool {
		if descending {
			return result[i].price > result[j].price
		}
		return result[i].price < result[j].price
	})
	return result
}
