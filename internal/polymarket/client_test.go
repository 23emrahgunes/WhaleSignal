package polymarket

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"
)

func TestBTC5mWindowAndSlug(t *testing.T) {
	tm := time.Unix(1_800_000_123, 0).UTC()
	start := BTC5mWindowStart(tm)
	if start.Unix()%300 != 0 {
		t.Fatalf("window start is not aligned: %v", start)
	}
	want := "btc-updown-5m-" + strconv.FormatInt(start.Unix(), 10)
	if got := BTC5mEventSlug(tm); got != want {
		t.Fatalf("unexpected slug: got %s want %s", got, want)
	}
}

func TestFetchActiveBTC5mMarket(t *testing.T) {
	start := time.Unix(1_800_000_000, 0).UTC()
	start = time.Unix(start.Unix()-start.Unix()%300, 0).UTC()
	now := start.Add(90 * time.Second)
	eventSlug := BTC5mEventSlug(start)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/events/slug/"+eventSlug {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"id":               "event-1",
			"slug":             eventSlug,
			"title":            "Bitcoin Up or Down - 5 Minutes",
			"resolutionSource": "https://data.chain.link/streams/btc-usd",
			"active":           true,
			"closed":           false,
			"markets": []map[string]interface{}{
				{
					"id":            "market-1",
					"question":      "BTC Up or Down - 5 Minutes",
					"slug":          eventSlug,
					"endDate":       start.Add(5 * time.Minute).Format(time.RFC3339),
					"endDateIso":    start.Add(5 * time.Minute).Format(time.RFC3339),
					"clobTokenIds":  `["111","222"]`,
					"outcomes":      `["Up","Down"]`,
					"outcomePrices": `["0.52","0.48"]`,
					"active":        true,
					"closed":        false,
				},
			},
		})
	}))
	defer server.Close()

	client := NewClientWithBaseURL(server.URL, server.Client())
	market, err := client.FetchActiveBTC5mMarketAt(now)
	if err != nil {
		t.Fatalf("FetchActiveBTC5mMarketAt failed: %v", err)
	}
	if market.EventSlug != eventSlug {
		t.Fatalf("expected event slug %q, got %q", eventSlug, market.EventSlug)
	}
	if !market.StartTime.Equal(start) {
		t.Fatalf("expected canonical start %v, got %v", start, market.StartTime)
	}
	if !market.EndTime.Equal(start.Add(5 * time.Minute)) {
		t.Fatalf("unexpected end time: %v", market.EndTime)
	}
	if len(market.Tokens) != 2 || market.Tokens[0].Outcome != "Up" || market.Tokens[0].Price != 0.52 {
		t.Fatalf("unexpected token parsing: %+v", market.Tokens)
	}
	if market.PriceToBeat != 0 {
		t.Fatalf("Gamma discovery must not invent Chainlink price-to-beat: %f", market.PriceToBeat)
	}
}

func TestFetchActiveBTC5mMarketNoFallbackOn404(t *testing.T) {
	server := httptest.NewServer(http.NotFoundHandler())
	defer server.Close()
	client := NewClientWithBaseURL(server.URL, server.Client())

	_, err := client.FetchActiveBTC5mMarketAt(time.Now().UTC())
	if err == nil {
		t.Fatal("expected error; fake fallback markets are forbidden")
	}
}

func TestFetchPriceToBeatParsesReadOnlyReference(t *testing.T) {
	priceServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("symbol") != "BTC" || r.URL.Query().Get("variant") != "fiveminute" {
			t.Fatalf("unexpected reference query: %s", r.URL.RawQuery)
		}
		_ = json.NewEncoder(w).Encode(map[string]interface{}{"openPrice": "100123.45"})
	}))
	defer priceServer.Close()

	client := NewClientWithBaseURLs("http://gamma.invalid", priceServer.URL, priceServer.Client())
	start := time.Unix(1_800_000_000, 0).UTC()
	market := &Market{StartTime: start, EndTime: start.Add(5 * time.Minute)}
	got, err := client.FetchPriceToBeat(market)
	if err != nil {
		t.Fatalf("FetchPriceToBeat failed: %v", err)
	}
	if got != 100123.45 {
		t.Fatalf("unexpected reference price: %f", got)
	}
}
