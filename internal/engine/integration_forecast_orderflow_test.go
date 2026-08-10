package engine

import (
	"math"
	"testing"
)

func TestEvaluatorCombinesForecastAndDepthMetrics(t *testing.T) {
	bc, market, now := readyInputs()
	res := NewEvaluator().Evaluate(bc, market, 100123.45, true, now.Format("2006-01-02T15:04:05.999999999Z07:00"))
	if res == nil {
		t.Fatal("expected integrated evaluation result")
	}
	if !res.ForecastReady || res.ForecastSamples < 10 {
		t.Fatalf("terminal forecast must be ready: %+v", res)
	}
	if res.ForecastPrice <= 0 || res.ForecastHigh95 <= res.ForecastLow95 {
		t.Fatalf("invalid terminal forecast diagnostics: %+v", res)
	}
	if res.BidNotionalUSD <= 0 || res.AskNotionalUSD <= 0 || res.TotalDepthNotionalUSD <= 0 {
		t.Fatalf("Depth20 notionals must be populated: %+v", res)
	}
	if res.BestBid <= 0 || res.BestAsk <= res.BestBid {
		t.Fatalf("invalid top of book: bid=%f ask=%f", res.BestBid, res.BestAsk)
	}
	if math.Abs(res.OrderFlowScore) > 0.8000000001 {
		t.Fatalf("order-flow score cap violated: %f", res.OrderFlowScore)
	}
	if res.PUp < 0 || res.PUp > 1 || res.PDown < 0 || res.PDown > 1 || math.Abs(res.PUp+res.PDown-1) > 1e-9 {
		t.Fatalf("invalid terminal probabilities: up=%f down=%f", res.PUp, res.PDown)
	}
}
