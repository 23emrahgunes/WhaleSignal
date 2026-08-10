package binance

import (
	"testing"
	"time"
)

func TestDepthFreshnessAndSourceTracking(t *testing.T) {
	c := NewClient()
	if c.IsDepthFreshAt(time.Now().UTC(), 3*time.Second) {
		t.Fatal("empty orderbook must not be fresh")
	}
	now := time.Now().UTC()
	c.UpdateDepthWithSource(
		[][]string{{"64000", "1.25"}, {"63999", "2.50"}},
		[][]string{{"64001", "1.50"}, {"64002", "3.00"}},
		now,
		"BINANCE_REST_DEPTH20",
	)
	if !c.IsDepthFreshAt(time.Now().UTC(), 3*time.Second) {
		t.Fatal("new REST depth snapshot should be fresh")
	}
	if got := c.GetDepthDataSource(); got != "BINANCE_REST_DEPTH20" {
		t.Fatalf("depth source %q", got)
	}
	bids, asks := c.GetLastBidsAndAsks()
	if len(bids) != 2 || len(asks) != 2 {
		t.Fatalf("unexpected depth sizes bids=%d asks=%d", len(bids), len(asks))
	}
	if !c.IsDepthFreshAt(time.Now().UTC().Add(4*time.Second), 3*time.Second) {
		// LastDepthUpdateTime uses wall-clock receipt time. Use a future point to
		// prove stale detection without sleeping.
	} else {
		t.Fatal("depth snapshot should become stale")
	}
}
