package api

import (
	"encoding/json"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"pm-edge/internal/storage"
)

func TestPaperStatsAndTradesHandlers(t *testing.T) {
	db, err := storage.NewDatabase(filepath.Join(t.TempDir(), "api-paper.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	s := NewServer(db, 1000)

	rr := httptest.NewRecorder()
	s.handlePaperStats(rr, httptest.NewRequest("GET", "/api/paper/stats", nil))
	if rr.Code != 200 {
		t.Fatalf("stats status %d", rr.Code)
	}
	var stats storage.PaperStats
	if err := json.Unmarshal(rr.Body.Bytes(), &stats); err != nil {
		t.Fatal(err)
	}
	if stats.InitialBalance != 1000 || stats.CashBalance != 1000 || stats.TotalTrades != 0 {
		t.Fatalf("unexpected stats %#v", stats)
	}

	rr = httptest.NewRecorder()
	s.handlePaperTrades(rr, httptest.NewRequest("GET", "/api/paper/trades?limit=20", nil))
	if rr.Code != 200 {
		t.Fatalf("trades status %d", rr.Code)
	}
	var trades []storage.PaperTrade
	if err := json.Unmarshal(rr.Body.Bytes(), &trades); err != nil {
		t.Fatal(err)
	}
	if len(trades) != 0 {
		t.Fatalf("expected empty trades, got %d", len(trades))
	}
}
