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

func TestRecordTradeWithIDDeduplicatesRESTAndWS(t *testing.T) {
	c := NewMicrostructureClient()
	now := time.Now().UTC()
	c.recordTradeWithID(100, 1, false, now, 10)
	c.recordTradeWithID(100, 1, false, now, 10)
	c.mu.RLock()
	defer c.mu.RUnlock()
	if len(c.trades) != 1 {
		t.Fatalf("duplicate aggregate trade stored: %d", len(c.trades))
	}
}

func TestRecordTradeWithIDPreservesOutOfOrderRESTBackfill(t *testing.T) {
	c := NewMicrostructureClient()
	now := time.Now().UTC()
	c.recordTradeWithID(100, 2, true, now.Add(-time.Second), 105)    // newer aggressive sell arrives first
	c.recordTradeWithID(100, 3, false, now.Add(-2*time.Second), 103) // older aggressive buy arrives later via REST
	c.recordTradeWithID(100, 3, false, now.Add(-2*time.Second), 103) // duplicate must still be ignored
	c.mu.RLock()
	buy, sell := tradeWindow(c.trades, now.Add(-5*time.Second))
	c.mu.RUnlock()
	if buy != 300 || sell != 200 {
		t.Fatalf("out-of-order backfill lost flow: buy %.2f sell %.2f", buy, sell)
	}
}

func TestReconcileLifePreservesFirstSeen(t *testing.T) {
	now := time.Now().UTC()
	first := now.Add(-5 * time.Second)
	old := map[float64]levelLife{100: {FirstSeen: first, InitialSize: 10, Size: 10}}
	got := reconcileLife(old, map[float64]float64{100: 4, 99: 2}, now)
	if !got[100].FirstSeen.Equal(first) || got[100].InitialSize != 10 || got[100].Size != 4 {
		t.Fatalf("existing level lifecycle lost: %#v", got[100])
	}
	if !got[99].FirstSeen.Equal(now) || got[99].InitialSize != 2 {
		t.Fatalf("new level lifecycle wrong: %#v", got[99])
	}
}

func TestRESTAuthoritativeReconcileCorrectsWrongWSClassification(t *testing.T) {
	c := NewMicrostructureClient()
	now := time.Now().UTC().Truncate(time.Millisecond)

	// Simulate a provisional WS record with the wrong aggressor side.
	c.recordTradeWithID(100, 2, true, now.Add(-2*time.Second), 42)
	rows := []aggTradeRESTEvent{{
		AggregateTradeID: 42,
		Price:            "100",
		Quantity:         "2",
		TradeTime:        now.Add(-2 * time.Second).UnixMilli(),
		BuyerIsMaker:     false, // authoritative REST says aggressive BUY
	}}
	if err := c.reconcileRESTAggTradeWindow(rows, now); err != nil {
		t.Fatal(err)
	}

	c.mu.RLock()
	buy, sell := tradeWindow(c.trades, now.Add(-5*time.Second))
	restFresh := !c.lastAggRESTTime.IsZero()
	c.mu.RUnlock()
	if buy != 200 || sell != 0 {
		t.Fatalf("REST did not correct WS side: buy %.2f sell %.2f", buy, sell)
	}
	if !restFresh {
		t.Fatal("expected authoritative REST reconcile timestamp")
	}
}

func TestRESTAuthoritativeReconcilePreservesNewerWSRecord(t *testing.T) {
	c := NewMicrostructureClient()
	now := time.Now().UTC().Truncate(time.Millisecond)
	c.recordTradeWithID(100, 1, true, now.Add(time.Second), 101)
	rows := []aggTradeRESTEvent{{
		AggregateTradeID: 100,
		Price:            "100",
		Quantity:         "2",
		TradeTime:        now.Add(-time.Second).UnixMilli(),
		BuyerIsMaker:     false,
	}}
	if err := c.reconcileRESTAggTradeWindow(rows, now); err != nil {
		t.Fatal(err)
	}
	c.mu.RLock()
	buy, sell := tradeWindow(c.trades, now.Add(-5*time.Second))
	c.mu.RUnlock()
	if buy != 200 || sell != 100 {
		t.Fatalf("newer provisional WS record lost: buy %.2f sell %.2f", buy, sell)
	}
}

func TestTradeFlowRequiresFreshAuthoritativeREST(t *testing.T) {
	c := NewMicrostructureClient()
	now := time.Now().UTC()
	c.mu.Lock()
	c.synchronized = true
	c.source = "BINANCE_DEEP_REST1000"
	c.lastBookTime = now
	c.bids = map[float64]float64{100: 1, 20: 1}
	c.asks = map[float64]float64{101: 1, 181: 1}
	c.lastTradeTime = now
	c.trades = []aggressiveTrade{{Time: now, BuyUSD: 100}}
	c.mu.Unlock()

	before := c.Snapshot(100.5, 110, now)
	if before.TradeFlowAvailable {
		t.Fatal("unverified WS-only trade flow must fail closed")
	}

	c.mu.Lock()
	c.lastAggRESTTime = now
	c.mu.Unlock()
	after := c.Snapshot(100.5, 110, now)
	if !after.TradeFlowAvailable {
		t.Fatalf("fresh REST-authoritative flow should be available: %#v", after)
	}
	if after.TradeFlowSource != "BINANCE_AGGTRADES_REST_AUTH" {
		t.Fatalf("unexpected trade-flow source %q", after.TradeFlowSource)
	}
}
