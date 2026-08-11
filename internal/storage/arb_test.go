package storage

import (
	"path/filepath"
	"testing"

	"pm-edge/internal/arb"
)

func TestArbStorageRoundTripAndStats(t *testing.T) {
	db, err := NewDatabase(filepath.Join(t.TempDir(), "arb.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.EnsureArbSchema(); err != nil {
		t.Fatal(err)
	}
	s := &arb.Snapshot{Timestamp: "2026-08-12T00:00:00Z", Timeframe: "5m", MarketSlug: "btc-updown-5m-1", Status: arb.StatusCandidate, Reason: "READY", NetEdge: .031, TargetEdge: .02, FirstLeg: "UP", OrderSize: 5, PairEdgePass: true, PTBReady: true}
	if err := db.InsertArbSnapshot(s); err != nil {
		t.Fatal(err)
	}
	got, err := db.GetLatestArbSnapshot("5m")
	if err != nil {
		t.Fatal(err)
	}
	if got == nil || got.NetEdge != .031 || got.OrderSize != 5 {
		t.Fatalf("bad roundtrip %+v", got)
	}
	stats, err := db.GetArbStatsByTimeframe("5m")
	if err != nil {
		t.Fatal(err)
	}
	if stats.TotalSnapshots != 1 || stats.Candidates != 1 || stats.UpFirst != 1 {
		t.Fatalf("bad stats %+v", stats)
	}
}

func TestArbPaperCycleStorageAndStats(t *testing.T) {
	db, err := NewDatabase(filepath.Join(t.TempDir(), "arb-paper.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.EnsureArbSchema(); err != nil {
		t.Fatal(err)
	}
	c := &arb.PaperCycle{Timeframe: "5m", MarketSlug: "btc-updown-5m-1", CreatedAt: "2026-08-12T00:00:00Z", UpdatedAt: "2026-08-12T00:00:00Z", Status: arb.PaperStatusRestingPair, OrderSize: 5, PreferredFirstLeg: "UP"}
	if err := db.InsertArbPaperCycle(c); err != nil {
		t.Fatal(err)
	}
	if c.ID <= 0 {
		t.Fatal("missing id")
	}
	open, err := db.GetOpenArbPaperCycle("5m")
	if err != nil || open == nil || open.ID != c.ID {
		t.Fatalf("open=%+v err=%v", open, err)
	}
	c.Status = arb.PaperStatusCompleted
	c.ActualFirstLeg = "UP"
	c.PreferredFirstMatched = true
	c.LockedPnL = .20
	c.PaperPnL = .20
	c.DeployedCost = 4.80
	c.CompletionMs = 1200
	c.UpdatedAt = "2026-08-12T00:00:02Z"
	if err := db.UpdateArbPaperCycle(c); err != nil {
		t.Fatal(err)
	}
	stats, err := db.GetArbPaperStatsByTimeframe(1000, "5m")
	if err != nil {
		t.Fatal(err)
	}
	if stats.CompletedCycles != 1 || stats.OpenCycles != 0 || stats.NetPaperPnL != .20 || stats.CashBalance != 1000.20 || stats.PairCompletionRate != 1 {
		t.Fatalf("stats=%+v", stats)
	}
}
