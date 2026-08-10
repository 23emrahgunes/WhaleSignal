package engine

import (
	"testing"
	"time"

	"pm-edge/internal/binance"
	"pm-edge/internal/polymarket"
)

func readyInputs() (*binance.Client, *polymarket.Market, time.Time) {
	now := time.Now().UTC()
	bc := binance.NewClient()
	bc.UpdateDepth([][]string{{"99990","2.0"}}, [][]string{{"100010","4.0"}}, now)
	bc.UpdateFromTrade(100000, 1, now, true)
	m := &polymarket.Market{Question:"Bitcoin Up or Down - 5 Minutes", EventSlug:"btc-updown-5m-test", Active:true, StartTime:now.Add(-time.Minute), EndTime:now.Add(4*time.Minute), PriceToBeat:99950}
	return bc, m, now
}

func TestEvaluatorUsesCanonicalReferencePrice(t *testing.T) {
	ev := NewEvaluator()
	bc, market, now := readyInputs()
	res := ev.Evaluate(bc, market, 100123.45, true, now.Format(time.RFC3339Nano))
	if res == nil { t.Fatal("expected evaluation result") }
	if res.CurrentPrice != 100123.45 { t.Fatalf("current price must be canonical reference: got %.2f", res.CurrentPrice) }
	if res.PriceToBeat != market.PriceToBeat { t.Fatalf("PTB got %.2f want %.2f", res.PriceToBeat, market.PriceToBeat) }
	if res.Slug != market.EventSlug { t.Fatalf("slug got %q want %q", res.Slug, market.EventSlug) }
}

func TestEvaluatorRejectsMissingMarket(t *testing.T) {
	ev := NewEvaluator(); bc, _, now := readyInputs()
	if got := ev.Evaluate(bc, nil, 100000, true, now.Format(time.RFC3339Nano)); got != nil { t.Fatal("nil market must produce NO_SIGNAL") }
}

func TestEvaluatorRejectsMissingPriceToBeat(t *testing.T) {
	ev := NewEvaluator(); bc, m, now := readyInputs(); m.PriceToBeat = 0
	if got := ev.Evaluate(bc, m, 100000, true, now.Format(time.RFC3339Nano)); got != nil { t.Fatal("missing PTB must produce NO_SIGNAL") }
}

func TestEvaluatorRejectsStaleReference(t *testing.T) {
	ev := NewEvaluator(); bc, m, now := readyInputs()
	if got := ev.Evaluate(bc, m, 100000, false, now.Format(time.RFC3339Nano)); got != nil { t.Fatal("stale Chainlink reference must produce NO_SIGNAL") }
}

func TestEvaluatorRejectsExpiredMarket(t *testing.T) {
	ev := NewEvaluator(); bc, m, now := readyInputs(); m.EndTime = now.Add(-time.Second)
	if got := ev.Evaluate(bc, m, 100000, true, now.Format(time.RFC3339Nano)); got != nil { t.Fatal("expired market must produce NO_SIGNAL") }
}
