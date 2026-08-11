package api

import (
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"pm-edge/internal/arb"
	"pm-edge/internal/storage"
)

func TestArbLiveEndpoint(t *testing.T) {
	db, err := storage.NewDatabase(filepath.Join(t.TempDir(), "api.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.EnsureArbSchema(); err != nil {
		t.Fatal(err)
	}
	if err := db.InsertArbSnapshot(&arb.Snapshot{Timestamp: "2026-08-12T00:00:00Z", Timeframe: "5m", MarketSlug: "btc-updown-5m-1", Status: arb.StatusCandidate, Reason: "READY", NetEdge: .03, TargetEdge: .02, FirstLeg: "UP", OrderSize: 5}); err != nil {
		t.Fatal(err)
	}
	s := NewServer(db)
	req := httptest.NewRequest("GET", "/api/arb?tf=5m", nil)
	rec := httptest.NewRecorder()
	s.handleArbLive(rec, req)
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), `"netEdge":0.03`) {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
}
