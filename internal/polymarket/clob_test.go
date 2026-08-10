package polymarket

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestBuyQuoteForSharesUsesAskVWAPFeesAndLatency(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/book" || r.URL.Query().Get("token_id") != "down-token" {
			t.Fatalf("unexpected request %s?%s", r.URL.Path, r.URL.RawQuery)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"asks":[{"price":"0.40","size":"3"},{"price":"0.42","size":"5"}],"min_order_size":"5"}`)
	}))
	defer ts.Close()

	c := NewClientWithBaseURL(ts.URL, ts.Client())
	q, err := c.fetchBuyQuote(ts.URL, "down-token", 6, 0, 0.07, 0.002)
	if err != nil {
		t.Fatalf("quote failed: %v", err)
	}
	if q.Shares != 6 || q.LevelsUsed != 2 {
		t.Fatalf("unexpected fill: %+v", q)
	}
	if q.AveragePrice <= 0.40 || q.AveragePrice >= 0.43 {
		t.Fatalf("unexpected VWAP %.6f", q.AveragePrice)
	}
	if q.Fee <= 0 || q.TotalCost <= q.Notional {
		t.Fatalf("fee/cost not applied: %+v", q)
	}
}

func TestBuyQuoteRejectsBelowMinOrderSize(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"asks":[{"price":"0.70","size":"100"}],"min_order_size":"5"}`)
	}))
	defer ts.Close()
	c := NewClientWithBaseURL(ts.URL, ts.Client())
	if _, err := c.fetchBuyQuote(ts.URL, "up-token", 0, 2.50, 0.07, 0); err == nil {
		t.Fatal("expected 2.50 USDC budget at 0.70 to fail 5-share minimum")
	}
}

func TestBuyQuoteBudgetConsumesMultipleLevels(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"asks":[{"price":"0.20","size":"5"},{"price":"0.21","size":"20"}],"min_order_size":"5"}`)
	}))
	defer ts.Close()
	c := NewClientWithBaseURL(ts.URL, ts.Client())
	q, err := c.fetchBuyQuote(ts.URL, "up-token", 0, 2.50, 0.07, 0)
	if err != nil {
		t.Fatalf("quote failed: %v", err)
	}
	if q.Shares < 5 {
		t.Fatalf("expected valid min-size fill, got %+v", q)
	}
	if q.TotalCost > 2.5000001 {
		t.Fatalf("budget exceeded: %+v", q)
	}
}
