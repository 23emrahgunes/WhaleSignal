package polymarket

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestBTC5mWindowAndSlug(t *testing.T) {
	start := time.Unix(1770000000, 0).UTC()
	now := start.Add(4*time.Minute + 59*time.Second)
	if got := BTC5mWindowStart(now); !got.Equal(start) {
		t.Fatalf("window start: got %s want %s", got, start)
	}
	wantSlug := "btc-updown-5m-1770000000"
	if got := BTC5mEventSlug(now); got != wantSlug {
		t.Fatalf("slug: got %q want %q", got, wantSlug)
	}
}

func TestFetchActiveBTC5mMarketAt(t *testing.T) {
	start := time.Unix(1770000000, 0).UTC()
	var requestedPath string

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestedPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"slug":             "btc-updown-5m-1770000000",
			"title":            "Bitcoin Up or Down - 5 Minutes",
			"resolutionSource": "https://data.chain.link/streams/btc-usd",
			"active":           true,
			"closed":           false,
			"markets": []map[string]interface{}{
				{
					"id": "m1", "question": "Bitcoin Up or Down - 5 Minutes",
					"slug": "btc-updown-5m-1770000000", "active": true, "closed": false,
					"endDate":       start.Add(5 * time.Minute).Format(time.RFC3339),
					"clobTokenIds":  `["up-token","down-token"]`,
					"outcomes":      `["Up","Down"]`,
					"outcomePrices": `["0.52","0.48"]`,
				},
			},
		})
	}))
	defer ts.Close()

	client := NewClientWithBaseURL(ts.URL, ts.Client())
	m, err := client.FetchActiveBTC5mMarketAt(start.Add(90 * time.Second))
	if err != nil {
		t.Fatalf("FetchActiveBTC5mMarketAt: %v", err)
	}
	if requestedPath != "/events/slug/btc-updown-5m-1770000000" {
		t.Fatalf("unexpected path %q", requestedPath)
	}
	if !m.StartTime.Equal(start) || !m.EndTime.Equal(start.Add(5*time.Minute)) {
		t.Fatalf("bad market window: %s - %s", m.StartTime, m.EndTime)
	}
	if len(m.Tokens) != 2 || m.Tokens[0].TokenID != "up-token" || m.Tokens[0].Price != 0.52 {
		t.Fatalf("unexpected token parsing: %#v", m.Tokens)
	}
	if m.PriceToBeat != 0 {
		t.Fatalf("market discovery must not invent PTB; got %.2f", m.PriceToBeat)
	}
}

func TestFetchPriceToBeat(t *testing.T) {
	start := time.Unix(1770000000, 0).UTC()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/crypto/crypto-price" {
			http.NotFound(w, r)
			return
		}
		if r.URL.Query().Get("symbol") != "BTC" || r.URL.Query().Get("variant") != "fiveminute" {
			t.Errorf("unexpected query: %s", r.URL.RawQuery)
		}
		if !strings.Contains(r.URL.Query().Get("eventStartTime"), "T") {
			t.Errorf("eventStartTime missing RFC3339: %s", r.URL.RawQuery)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"openPrice":"64000.25"}`))
	}))
	defer ts.Close()

	client := NewClientWithBaseURLs(ts.URL, ts.URL+"/api/crypto/crypto-price", ts.Client())
	m := &Market{StartTime: start, EndTime: start.Add(5 * time.Minute)}
	got, err := client.FetchPriceToBeat(m)
	if err != nil {
		t.Fatalf("FetchPriceToBeat: %v", err)
	}
	if got != 64000.25 {
		t.Fatalf("openPrice: got %.2f", got)
	}
}

func TestFetchActiveBTC5mMarketDoesNotFallbackToSynthetic(t *testing.T) {
	start := time.Unix(1770000000, 0).UTC()
	ts := httptest.NewServer(http.NotFoundHandler())
	defer ts.Close()
	client := NewClientWithBaseURL(ts.URL, ts.Client())
	if m, err := client.FetchActiveBTC5mMarketAt(start.Add(time.Minute)); err == nil || m != nil {
		t.Fatalf("expected hard failure/no synthetic market, got market=%#v err=%v", m, err)
	}
}
