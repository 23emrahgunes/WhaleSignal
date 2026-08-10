package chainlink

import (
	"testing"
	"time"
)

func TestAnchorCapturedAtBoundary(t *testing.T) {
	c := NewClientWithURL("ws://invalid")
	start := time.Unix(1_800_000_000, 0).UTC()
	start = time.Unix(start.Unix()-start.Unix()%300, 0).UTC()

	c.Observe(100000, start.Add(-500*time.Millisecond))
	c.Observe(100010, start.Add(200*time.Millisecond))

	s := c.Snapshot(start, start.Add(time.Second))
	if !s.Ready {
		t.Fatal("expected reference snapshot to be ready")
	}
	if s.PriceToBeat != 100010 && s.PriceToBeat != 100000 {
		t.Fatalf("unexpected price to beat: %f", s.PriceToBeat)
	}
	if !s.Fresh {
		t.Fatal("expected current Chainlink price to be fresh")
	}
}

func TestMidWindowStartupDoesNotInventAnchor(t *testing.T) {
	c := NewClientWithURL("ws://invalid")
	start := time.Unix(1_800_000_000, 0).UTC()
	start = time.Unix(start.Unix()-start.Unix()%300, 0).UTC()

	c.Observe(100500, start.Add(2*time.Minute))
	s := c.Snapshot(start, start.Add(2*time.Minute+time.Second))
	if s.Ready {
		t.Fatal("mid-window startup must not invent a price-to-beat")
	}
}

func TestSnapshotBecomesStale(t *testing.T) {
	c := NewClientWithURL("ws://invalid")
	start := time.Unix(1_800_000_000, 0).UTC()
	start = time.Unix(start.Unix()-start.Unix()%300, 0).UTC()
	c.Observe(99000, start.Add(time.Second))

	s := c.Snapshot(start, start.Add(10*time.Second))
	if s.Fresh {
		t.Fatal("expected snapshot to be stale")
	}
}
