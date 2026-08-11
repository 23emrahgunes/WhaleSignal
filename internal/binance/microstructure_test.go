package binance

import (
	"math"
	"testing"
	"time"
)

func TestBandNotionalUsesPriceDistanceNotLevelCount(t *testing.T) {
	bids := map[float64]float64{100: 1, 99: 2, 90: 100}
	asks := map[float64]float64{101: 1, 102: 2, 110: 100}
	bid, ask := bandNotional(bids, asks, 100.5, 2)
	if math.Abs(bid-(100+198)) > 1e-9 {
		t.Fatalf("unexpected bid notional %.2f", bid)
	}
	if math.Abs(ask-(101+204)) > 1e-9 {
		t.Fatalf("unexpected ask notional %.2f", ask)
	}
}

func TestAggressiveTradeClassificationAndWindows(t *testing.T) {
	c := NewMicrostructureClient()
	now := time.Now().UTC()
	c.recordTrade(100, 10, false, now.Add(-2*time.Second)) // aggressive buy
	c.recordTrade(100, 5, true, now.Add(-2*time.Second))   // aggressive sell
	c.recordTrade(100, 20, true, now.Add(-20*time.Second))

	c.mu.RLock()
	buy5, sell5 := tradeWindow(c.trades, now.Add(-5*time.Second))
	buy30, sell30 := tradeWindow(c.trades, now.Add(-30*time.Second))
	c.mu.RUnlock()
	if buy5 != 1000 || sell5 != 500 {
		t.Fatalf("5s flow got buy %.2f sell %.2f", buy5, sell5)
	}
	if buy30 != 1000 || sell30 != 2500 {
		t.Fatalf("30s flow got buy %.2f sell %.2f", buy30, sell30)
	}
}

func TestDeepDiffRejectsSequenceGap(t *testing.T) {
	c := NewMicrostructureClient()
	now := time.Now().UTC()
	c.mu.Lock()
	c.bids = map[float64]float64{100: 1}
	c.asks = map[float64]float64{101: 1}
	c.bidLife = map[float64]levelLife{100: {FirstSeen: now, InitialSize: 1, Size: 1}}
	c.askLife = map[float64]levelLife{101: {FirstSeen: now, InitialSize: 1, Size: 1}}
	c.lastUpdateID = 100
	c.mu.Unlock()

	if c.applyDiff(deepDiffEvent{FirstID: 102, FinalID: 102, Bids: [][]string{{"100", "2"}}}) {
		t.Fatal("expected sequence gap rejection")
	}
	if !c.applyDiff(deepDiffEvent{FirstID: 101, FinalID: 101, Bids: [][]string{{"100", "2"}}}) {
		t.Fatal("expected contiguous diff acceptance")
	}
}

func TestPTBBarrierPenalizesHeavyAskPath(t *testing.T) {
	bids := map[float64]float64{100: 1, 99: 1, 98: 1}
	asks := map[float64]float64{101: 10, 102: 10, 103: 10, 104: 10, 105: 10}
	_, _, _, score := ptbBarrier(bids, asks, 100, 105)
	if score >= 0 {
		t.Fatalf("expected negative UP barrier score, got %.4f", score)
	}
}
