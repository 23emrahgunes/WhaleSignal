package binance

import (
	"math"
	"testing"
	"time"
)

func TestClientReturnsAreSampledPerSecond(t *testing.T) {
	c := NewClient()
	base := time.Unix(1770000000, 0).UTC()

	c.UpdateFromTrade(1000.0, 1.0, base, true)
	c.UpdateFromTrade(1010.0, 1.0, base.Add(500*time.Millisecond), true)
	if got := len(c.GetLogReturns()); got != 0 {
		t.Fatalf("same-second ticks must not create returns, got %d", got)
	}

	c.UpdateFromTrade(1020.1, 1.0, base.Add(time.Second), true)
	c.UpdateFromTrade(1030.301, 1.0, base.Add(2*time.Second), true)
	returns := c.GetLogReturns()
	if len(returns) != 2 {
		t.Fatalf("expected 2 one-second returns, got %d", len(returns))
	}
	if want := math.Log(1020.1 / 1010.0); math.Abs(returns[0]-want) > 1e-9 {
		t.Fatalf("first return got %.12f want %.12f", returns[0], want)
	}
	if want := math.Log(1030.301 / 1020.1); math.Abs(returns[1]-want) > 1e-9 {
		t.Fatalf("second return got %.12f want %.12f", returns[1], want)
	}
	if c.GetPrice() != 1030.301 {
		t.Fatalf("unexpected current price %.3f", c.GetPrice())
	}
}

func TestGapReturnIsNormalizedToOneSecondEquivalent(t *testing.T) {
	c := NewClient()
	base := time.Unix(1770000000, 0).UTC()
	c.UpdateFromTrade(100.0, 1, base, true)
	c.UpdateFromTrade(121.0, 1, base.Add(2*time.Second), true)
	returns := c.GetLogReturns()
	if len(returns) != 1 {
		t.Fatalf("expected one return, got %d", len(returns))
	}
	want := math.Log(121.0/100.0) / 2.0
	if math.Abs(returns[0]-want) > 1e-12 {
		t.Fatalf("normalized return got %.12f want %.12f", returns[0], want)
	}
}

func TestFreshLargeWallIsFilteredUntilPersistent(t *testing.T) {
	c := NewClient()
	base := time.Unix(1770000000, 0).UTC()
	bids := [][]string{{"99000", "1.0"}, {"98900", "2.5"}, {"98800", "3.0"}, {"98700", "0.5"}}
	asks := [][]string{{"99100", "1.5"}, {"99200", "4.0"}}
	c.UpdateDepth(bids, asks, base)
	if got := c.getMedianDepthSize(true); got != 1.75 {
		t.Fatalf("median got %.2f want 1.75", got)
	}

	withWall := append(append([][]string{}, bids...), []string{"98500", "10.0"})
	c.UpdateDepth(withWall, asks, base.Add(100*time.Millisecond))
	if !c.IsSpoofing(98500, 10, true) {
		t.Fatal("fresh oversized wall should be treated as untrusted")
	}

	c.UpdateDepth(withWall, asks, base.Add(1300*time.Millisecond))
	if c.IsSpoofing(98500, 10, true) {
		t.Fatal("wall persisting >1s should no longer be classified as transient spoof")
	}
}

func TestWSStateAndFreshnessFallback(t *testing.T) {
	c := NewClient()
	c.UpdateFromTrade(64000, 1, time.Now().UTC(), true)
	c.SetWSState(true, false)
	now := time.Now().UTC()
	if c.ShouldRESTFallback(now, 3*time.Second) {
		t.Fatal("fresh connected WS should not use REST fallback")
	}
	if !c.ShouldRESTFallback(now.Add(4*time.Second), 3*time.Second) {
		t.Fatal("stale WS price must trigger REST fallback")
	}
	c.SetWSState(false, true)
	if !c.ShouldRESTFallback(time.Now().UTC(), 3*time.Second) {
		t.Fatal("disconnected WS must trigger REST fallback")
	}
}
