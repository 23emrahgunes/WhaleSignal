package chainlink

import (
	"testing"
	"time"
)

func TestBoundaryPrice(t *testing.T) {
	c := NewClientWithURL("ws://unused")
	boundary := time.Unix(1786390200, 0).UTC()
	c.Observe(64000, boundary.Add(-time.Second))
	c.Observe(64005, boundary.Add(time.Second))
	price, ok := c.BoundaryPrice(boundary)
	if !ok {
		t.Fatal("expected boundary anchor")
	}
	if price != 64000 && price != 64005 {
		t.Fatalf("unexpected boundary price %.2f", price)
	}
	if _, ok := c.BoundaryPrice(boundary.Add(5 * time.Minute)); ok {
		t.Fatal("unexpected future boundary anchor")
	}
}
