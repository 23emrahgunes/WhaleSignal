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

func TestMarketBuyBudgetIsUSDAndNotBlockedByBookMinOrderSize(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"asks":[{"price":"0.70","size":"100"}],"min_order_size":"5"}`)
	}))
	defer ts.Close()
	c := NewClientWithBaseURL(ts.URL, ts.Client())
	q, err := c.fetchBuyQuote(ts.URL, "up-token", 0, 2.50, 0.07, 0)
	if err != nil {
		t.Fatalf("USD-denominated market BUY should not be blocked by share min_order_size: %v", err)
	}
	if q.TotalCost > 2.5000001 {
		t.Fatalf("market BUY exceeded USDC budget: %+v", q)
	}
	if q.Shares <= 0 || q.Shares >= 5 {
		t.Fatalf("expected a valid sub-5-share result from a $2.50 market BUY at 0.70: %+v", q)
	}
	if q.MinOrderSize != 5 {
		t.Fatalf("book min_order_size should remain available as metadata: %+v", q)
	}
}

func TestOneDollarMarketBuyBudgetCanProduceSubShareMinimumFill(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"asks":[{"price":"0.80","size":"100"}],"min_order_size":"5"}`)
	}))
	defer ts.Close()
	c := NewClientWithBaseURL(ts.URL, ts.Client())
	q, err := c.fetchBuyQuote(ts.URL, "down-token", 0, 1.00, 0.07, 0)
	if err != nil {
		t.Fatalf("$1 market BUY quote failed: %v", err)
	}
	if q.TotalCost > 1.0000001 || q.Shares <= 0 || q.Shares >= 5 {
		t.Fatalf("unexpected $1 market BUY quote: %+v", q)
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
		t.Fatalf("expected more than 5 shares at low prices, got %+v", q)
	}
	if q.TotalCost > 2.5000001 {
		t.Fatalf("budget exceeded: %+v", q)
	}
}
