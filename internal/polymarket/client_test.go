package polymarket

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestBTC5mWindowAndSlug(t *testing.T) {
	tm := time.Unix(1_800_000_123, 0).UTC()
	start := BTC5mWindowStart(tm)
	if start.Unix()%300 != 0 {
		t.Fatalf("window start is not aligned: %v", start)
	}
	want := "btc-updown-5m-" + time.Unix(start.Unix(), 0).Format("__never__")
	_ = want // explicit slug assertion below avoids locale/time formatting concerns
	if got := BTC5mEventSlug(tm); got != "btc-updown-5m-"+formatInt(start.Unix()) {
		t.Fatalf("unexpected slug: %s", got)
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
					"id":             "market-1",
					"question":       "BTC Up or Down - 5 Minutes",
					"slug":           eventSlug,
					"eventStartTime": start.Format(time.RFC3339),
					"endDate":        start.Add(5 * time.Minute).Format(time.RFC3339),
					"endDateIso":     start.Add(5 * time.Minute).Format(time.RFC3339),
					"clobTokenIds":   `["111","222"]`,
					"outcomes":       `["Up","Down"]`,
					"outcomePrices":  `["0.52","0.48"]`,
					"active":         true,
					"closed":         false,
				},
			},
		})
	}))
	defer server.Close()

	client := NewClientWithBaseURL(server.URL, server.Client())
	market, err := client.FetchActiveBTC5mMarket(now)
	if err != nil {
		t.Fatalf("FetchActiveBTC5mMarket failed: %v", err)
	}
	if market.EventSlug != eventSlug {
		t.Fatalf("expected event slug %q, got %q", eventSlug, market.EventSlug)
	}
	if !market.StartTime.Equal(start) {
		t.Fatalf("expected start %v, got %v", start, market.StartTime)
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

	_, err := client.FetchActiveBTC5mMarket(time.Now().UTC())
	if err == nil {
		t.Fatal("expected error; fake fallback markets are forbidden")
	}
}

func formatInt(v int64) string {
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	buf := make([]byte, 0, 20)
	for v > 0 {
		buf = append(buf, byte('0'+v%10))
		v /= 10
	}
	if neg {
		buf = append(buf, '-')
	}
	for i, j := 0, len(buf)-1; i < j; i, j = i+1, j-1 {
		buf[i], buf[j] = buf[j], buf[i]
	}
	return string(buf)
}
