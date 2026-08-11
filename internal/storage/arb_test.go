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
