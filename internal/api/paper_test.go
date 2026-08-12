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
	if err := db.EnsurePaperInverseSchema(); err != nil {
		t.Fatal(err)
	}
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

	rr = httptest.NewRecorder()
	s.handlePaperInverseStats(rr, httptest.NewRequest("GET", "/api/paper/inverse/stats?tf=5m", nil))
	if rr.Code != 200 {
		t.Fatalf("inverse stats status %d", rr.Code)
	}
	var inverseStats storage.PaperInverseStats
	if err := json.Unmarshal(rr.Body.Bytes(), &inverseStats); err != nil {
		t.Fatal(err)
	}
	if inverseStats.TotalTrades != 0 || inverseStats.RealizedPnL != 0 {
		t.Fatalf("unexpected inverse stats %#v", inverseStats)
	}

	rr = httptest.NewRecorder()
	s.handlePaperInverseTrades(rr, httptest.NewRequest("GET", "/api/paper/inverse/trades?limit=20&tf=5m", nil))
	if rr.Code != 200 {
		t.Fatalf("inverse trades status %d", rr.Code)
	}
	var inverseTrades []storage.PaperInverseTrade
	if err := json.Unmarshal(rr.Body.Bytes(), &inverseTrades); err != nil {
		t.Fatal(err)
	}
	if len(inverseTrades) != 0 {
		t.Fatalf("expected empty inverse trades, got %d", len(inverseTrades))
	}
}
