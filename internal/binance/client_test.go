package binance

import (
	"math"
	"testing"
	"time"
)

func TestClientReturnsAreTimeNormalized(t *testing.T) {
	c := NewClient()
	t0 := time.Now().UTC().Truncate(time.Second)

	c.UpdateFromTrade(1000, 1, t0, true)
	c.UpdateFromTrade(1005, 1, t0.Add(200*time.Millisecond), true)
	if got := len(c.GetLogReturns()); got != 0 {
		t.Fatalf("same-second trade ticks must not create extra return samples, got %d", got)
	}

	c.UpdateFromTrade(1010, 1, t0.Add(time.Second), true)
	c.UpdateFromTrade(1020, 1, t0.Add(3*time.Second), true)
	returns := c.GetLogReturns()
	if len(returns) != 2 {
		t.Fatalf("expected 2 time-normalized returns, got %d", len(returns))
	}

	wantFirst := math.Log(1010.0 / 1005.0)
	if math.Abs(returns[0]-wantFirst) > 1e-9 {
		t.Fatalf("unexpected first return: got %f want %f", returns[0], wantFirst)
	}
	wantSecond := math.Log(1020.0/1010.0) / 2.0
	if math.Abs(returns[1]-wantSecond) > 1e-9 {
		t.Fatalf("gap-normalized return mismatch: got %f want %f", returns[1], wantSecond)
	}
}

func TestPriceFreshness(t *testing.T) {
	c := NewClient()
	now := time.Now().UTC()
	c.UpdateFromTrade(1000, 1, now, true)
	if !c.IsPriceFresh(3 * time.Second) {
		t.Fatal("new trade price should be fresh")
	}
}

func TestIsSpoofingFiltersLargeNewLevelUntilPersistent(t *testing.T) {
	c := NewClient()
	t1 := time.Now().UTC()
	bids := [][]string{
		{"99000", "1.0"},
		{"98900", "2.5"},
		{"98800", "3.0"},
		{"98700", "0.5"},
	}
	asks := [][]string{{"99100", "1.5"}, {"99200", "4.0"}}
	c.UpdateDepth(bids, asks, t1)

	if median := c.getMedianDepthSize(true); median != 1.75 {
		t.Fatalf("expected median 1.75, got %f", median)
	}

	withLargeNew := append(append([][]string{}, bids...), []string{"98500", "10.0"})
	c.UpdateDepth(withLargeNew, asks, t1.Add(100*time.Millisecond))
	if !c.IsSpoofing(98500, 10, true) {
		t.Fatal("large newly appeared level should be filtered as suspicious")
	}

	c.UpdateDepth(withLargeNew, asks, t1.Add(1500*time.Millisecond))
	if c.IsSpoofing(98500, 10, true) {
		t.Fatal("persistent level should no longer be classified as spoof")
	}
}
