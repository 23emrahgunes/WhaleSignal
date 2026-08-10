package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	"pm-edge/internal/engine"
	"pm-edge/internal/storage"
)

func TestAPIHandlers(t *testing.T) {
	tempDB := "data/test_api_db.sqlite"
	defer os.Remove(tempDB)

	db, err := storage.NewDatabase(tempDB)
	if err != nil {
		t.Fatalf("Failed to init db: %v", err)
	}
	defer db.Close()

	server := NewServer(db)

	// Test health handler
	req := httptest.NewRequest("GET", "/health", nil)
	rr := httptest.NewRecorder()
	server.handleHealth(rr, req)

	if rr.Code != http.StatusOK {
		t.Errorf("Expected health status 200, got %d", rr.Code)
	}
	if rr.Body.String() != "OK" {
		t.Errorf("Expected health response 'OK', got %q", rr.Body.String())
	}

	// Update state and test live handler
	res := &engine.EvaluationResult{
		PriceToBeat:  10000.0,
		CurrentPrice: 9900.0,
	}
	server.UpdateState(res, nil)

	reqLive := httptest.NewRequest("GET", "/api/live", nil)
	rrLive := httptest.NewRecorder()
	server.handleLive(rrLive, reqLive)

	if rrLive.Code != http.StatusOK {
		t.Errorf("Expected live status 200, got %d", rrLive.Code)
	}

	var liveResult map[string]interface{}
	if err := json.Unmarshal(rrLive.Body.Bytes(), &liveResult); err != nil {
		t.Fatalf("Failed to decode live payload: %v", err)
	}

	if liveResult["currentPrice"].(float64) != 9900.0 {
		t.Errorf("Expected currentPrice 9900.0, got %f", liveResult["currentPrice"])
	}
}
