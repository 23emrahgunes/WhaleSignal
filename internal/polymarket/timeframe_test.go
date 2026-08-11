package polymarket

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestBTC15mWindowAndSlug(t *testing.T) {
	now := time.Date(2026, 7, 24, 21, 23, 17, 0, time.UTC)
	start := BTC15mWindowStart(now)
	want := time.Date(2026, 7, 24, 21, 15, 0, 0, time.UTC)
	if !start.Equal(want) {
		t.Fatalf("start=%v want=%v", start, want)
	}
	if got := BTC15mEventSlug(now); got != fmt.Sprintf("btc-updown-15m-%d", want.Unix()) {
		t.Fatalf("slug=%s", got)
	}
	if TimeframeFromSlug(BTC15mEventSlug(now)) != "15m" || TimeframeFromSlug(BTC5mEventSlug(now)) != "5m" {
		t.Fatal("timeframe slug detection failed")
	}
}

func TestFetchActiveBTC15mMarketAtAndReferenceVariant(t *testing.T) {
	now := time.Date(2026, 7, 24, 21, 23, 17, 0, time.UTC)
	start := BTC15mWindowStart(now)
	slug := BTC15mEventSlug(start)
	mux := http.NewServeMux()
	mux.HandleFunc("/events/slug/"+slug, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"slug":%q,"title":"Bitcoin Up or Down - 15 Minutes","resolutionSource":"https://data.chain.link/streams/btc-usd","active":true,"closed":false,"markets":[{"id":"m15","question":"BTC Up or Down","slug":%q,"endDate":"","endDateIso":"","clobTokenIds":"[\"up15\",\"down15\"]","outcomes":"[\"Up\",\"Down\"]","outcomePrices":"[\"0.48\",\"0.52\"]","active":true,"closed":false}]}`, slug, slug)
	})
	mux.HandleFunc("/crypto-price", func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("variant"); got != "fifteenminute" {
			t.Errorf("variant=%q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"openPrice":64000.25}`))
	})
	ts := httptest.NewServer(mux)
	defer ts.Close()
	c := NewClientWithBaseURLs(ts.URL, ts.URL+"/crypto-price", ts.Client())
	m, err := c.FetchActiveBTC15mMarketAt(now)
	if err != nil {
		t.Fatal(err)
	}
	if m.EventSlug != slug || !m.EndTime.Equal(start.Add(15*time.Minute)) || len(m.Tokens) != 2 {
		t.Fatalf("bad market: %+v", m)
	}
	ptb, err := c.FetchPriceToBeatForTimeframe(m, "15m")
	if err != nil {
		t.Fatal(err)
	}
	if ptb != 64000.25 {
		t.Fatalf("ptb=%f", ptb)
	}
}
