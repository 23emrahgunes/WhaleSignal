package polymarket

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const btc15mWindow = 15 * time.Minute

func NormalizeTimeframe(tf string) string {
	switch strings.ToLower(strings.TrimSpace(tf)) {
	case "15m", "15min", "15minute", "15minutes":
		return "15m"
	default:
		return "5m"
	}
}

func BTCWindowDuration(tf string) time.Duration {
	if NormalizeTimeframe(tf) == "15m" {
		return btc15mWindow
	}
	return btc5mWindow
}

func BTCWindowStart(tf string, t time.Time) time.Time {
	seconds := int64(BTCWindowDuration(tf) / time.Second)
	u := t.UTC().Unix()
	return time.Unix(u-(u%seconds), 0).UTC()
}

func BTCEventSlug(tf string, start time.Time) string {
	tf = NormalizeTimeframe(tf)
	return fmt.Sprintf("btc-updown-%s-%d", tf, BTCWindowStart(tf, start).Unix())
}

func BTC15mWindowStart(t time.Time) time.Time { return BTCWindowStart("15m", t) }
func BTC15mEventSlug(start time.Time) string   { return BTCEventSlug("15m", start) }

func TimeframeFromSlug(slug string) string {
	slug = strings.ToLower(strings.TrimSpace(slug))
	if strings.HasPrefix(slug, "btc-updown-15m-") {
		return "15m"
	}
	if strings.HasPrefix(slug, "btc-updown-5m-") {
		return "5m"
	}
	return ""
}

func IsCanonicalBTCUpDownSlug(slug string) bool { return TimeframeFromSlug(slug) != "" }

func (c *Client) FetchActiveBTC15mMarket() (*Market, error) {
	return c.FetchActiveBTC15mMarketAt(time.Now().UTC())
}

func (c *Client) FetchActiveBTC15mMarketAt(now time.Time) (*Market, error) {
	start := BTC15mWindowStart(now)
	eventSlug := BTC15mEventSlug(start)
	req, err := http.NewRequest(http.MethodGet, c.baseURL+"/events/slug/"+eventSlug, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("gamma event lookup %s returned status %d", eventSlug, resp.StatusCode)
	}

	var ev gammaEvent
	if err := json.NewDecoder(resp.Body).Decode(&ev); err != nil {
		return nil, fmt.Errorf("decode gamma event %s: %w", eventSlug, err)
	}
	if ev.Closed || !ev.Active {
		return nil, fmt.Errorf("event %s is not active", eventSlug)
	}
	var gm *gammaMarket
	for i := range ev.Markets {
		if ev.Markets[i].Active && !ev.Markets[i].Closed {
			gm = &ev.Markets[i]
			break
		}
	}
	if gm == nil {
		return nil, fmt.Errorf("event %s has no active market", eventSlug)
	}
	end := start.Add(btc15mWindow)
	if !now.UTC().Before(end) {
		return nil, fmt.Errorf("event %s has ended", eventSlug)
	}
	outcomes := parseJSONStringSlice(gm.Outcomes)
	ids := parseJSONStringSlice(gm.ClobTokenIds)
	prices := parseJSONFloatSlice(gm.OutcomePrices)
	tokens := make([]Token, 0, len(ids))
	for i, id := range ids {
		t := Token{TokenID: id}
		if i < len(outcomes) {
			t.Outcome = outcomes[i]
		}
		if i < len(prices) {
			t.Price = prices[i]
		}
		tokens = append(tokens, t)
	}
	q := gm.Question
	if q == "" {
		q = ev.Title
	}
	return &Market{ID: gm.ID, Question: q, Slug: gm.Slug, EventSlug: eventSlug, EndDate: gm.EndDate, EndDateIso: gm.EndDateIso, ClobTokenIds: gm.ClobTokenIds, Tokens: tokens, Active: gm.Active, Closed: gm.Closed, StartTime: start, EndTime: end, Outcomes: outcomes, ResolutionURL: ev.ResolutionSource}, nil
}

// FetchPriceToBeatForTimeframe is the read-only restart fallback. The primary
// reference remains the exact Chainlink RTDS boundary captured by the engine.
func (c *Client) FetchPriceToBeatForTimeframe(m *Market, tf string) (float64, error) {
	if m == nil || m.StartTime.IsZero() || m.EndTime.IsZero() {
		return 0, fmt.Errorf("market window incomplete")
	}
	variant := "fiveminute"
	if NormalizeTimeframe(tf) == "15m" {
		variant = "fifteenminute"
	}
	q := url.Values{}
	q.Set("symbol", "BTC")
	q.Set("eventStartTime", m.StartTime.UTC().Format(time.RFC3339))
	q.Set("variant", variant)
	q.Set("endDate", m.EndTime.UTC().Format(time.RFC3339))
	req, err := http.NewRequest(http.MethodGet, c.cryptoPriceBaseURL+"?"+q.Encode(), nil)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "Mozilla/5.0 PM-Edge-Research/1.0")
	req.Header.Set("Referer", "https://polymarket.com/")
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("reference-price status %d", resp.StatusCode)
	}
	var p struct {
		OpenPrice json.RawMessage `json:"openPrice"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&p); err != nil {
		return 0, err
	}
	v, ok := parseNumber(p.OpenPrice)
	if !ok || v <= 0 {
		return 0, fmt.Errorf("reference-price response missing openPrice")
	}
	return v, nil
}
