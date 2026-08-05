package binance

import (
	"math"
	"testing"
	"time"
)

func TestClientUpdateFromTradeAndReturns(t *testing.T) {
	c := NewClient()

	c.UpdateFromTrade(1000.0, 1.0, time.Now().UTC())
	c.UpdateFromTrade(1010.0, 1.5, time.Now().UTC())
	c.UpdateFromTrade(1020.1, 2.0, time.Now().UTC())

	price := c.GetPrice()
	if price != 1020.1 {
		t.Errorf("Expected current price 1020.1, got %f", price)
	}

	returns := c.GetLogReturns()
	if len(returns) != 2 {
		t.Errorf("Expected 2 log returns, got %d", len(returns))
	}

	expectedRet := math.Log(1020.1 / 1010.0)
	if math.Abs(returns[1]-expectedRet) > 1e-6 {
		t.Errorf("Expected log return %f, got %f", expectedRet, returns[1])
	}
}

func TestIsSpoofingAndMedian(t *testing.T) {
	c := NewClient()

	bids := [][]string{
		{"99000", "1.0"},
		{"98900", "2.5"},
		{"98800", "3.0"},
		{"98700", "0.5"},
	}
	asks := [][]string{
		{"99100", "1.5"},
		{"99200", "4.0"},
	}

	t1 := time.Now().UTC()
	c.UpdateDepth(bids, asks, t1)

	median := c.getMedianDepthSize(true)
	// sorted bids sizes: 0.5, 1.0, 2.5, 3.0. Even count (4), median of 1.0 and 2.5 = 1.75
	if median != 1.75 {
		t.Errorf("Expected median 1.75, got %f", median)
	}

	// Dynamic threshold = max(2.0, 1.75 * 3) = 5.25.
	// Now, spoof attempt at price 98500 with size 10.0 (greater than 5.25)
	spoofPrice := 98500.0
	spoofSize := 10.0

	// Order gets inserted
	t2 := t1.Add(100 * time.Millisecond)
	bidsWithSpoof := append(bids, []string{"98500", "10.0"})
	c.UpdateDepth(bidsWithSpoof, asks, t2)

	// Order gets cancelled completely within 1 sec
	t3 := t2.Add(200 * time.Millisecond)
	bidsCancelled := append(bids, []string{"98500", "0.0"})
	c.UpdateDepth(bidsCancelled, asks, t3)

	isSpoof := c.IsSpoofing(spoofPrice, spoofSize, true)
	if !isSpoof {
		t.Errorf("Expected IsSpoofing to be true, got false")
	}
}
