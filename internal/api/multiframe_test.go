package api

import (
	"encoding/json"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"pm-edge/internal/engine"
	"pm-edge/internal/storage"
)

func TestTimeframeAwareLiveAPIAndComparison(t *testing.T) {
	db, err := storage.NewDatabase(filepath.Join(t.TempDir(), "api-multiframe.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	s := NewServer(db, 1000)
	s.UpdateStateFor("5m", &engine.EvaluationResult{CurrentPrice: 50001, PriceToBeat: 50000}, nil)
	s.UpdateStateFor("15m", &engine.EvaluationResult{CurrentPrice: 60001, PriceToBeat: 60000}, nil)

	for _, tc := range []struct {
		tf   string
		want float64
	}{{"5m", 50001}, {"15m", 60001}} {
		rr := httptest.NewRecorder()
		req := httptest.NewRequest("GET", "/api/live?tf="+tc.tf, nil)
		s.handleLive(rr, req)
		var payload map[string]interface{}
		if err := json.Unmarshal(rr.Body.Bytes(), &payload); err != nil {
			t.Fatal(err)
		}
		if payload["currentPrice"].(float64) != tc.want {
			t.Fatalf("tf=%s current=%v want=%v", tc.tf, payload["currentPrice"], tc.want)
		}
	}

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/api/comparison", nil)
	s.handleComparison(rr, req)
	var cmp comparisonPayload
	if err := json.Unmarshal(rr.Body.Bytes(), &cmp); err != nil {
		t.Fatal(err)
	}
	if cmp.Status != "collecting" || cmp.MinSettled != 30 || cmp.Leader != "none" {
		t.Fatalf("unexpected bootstrap comparison: %+v", cmp)
	}
}
