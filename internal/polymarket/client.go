package polymarket

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const (
	defaultGammaBaseURL       = "https://gamma-api.polymarket.com"
	defaultCryptoPriceBaseURL = "https://polymarket.com/api/crypto/crypto-price"
	btc5mWindow                = 5 * time.Minute
)

type Token struct {
	Outcome string  `json:"outcome"`
	Price   float64 `json:"price"`
	TokenID string  `json:"tokenId"`
}

type Market struct {
	ID                string    `json:"id"`
	Question          string    `json:"question"`
	Slug              string    `json:"slug"`
	EventSlug         string    `json:"eventSlug"`
	EndDate           string    `json:"endDate"`
	EndDateIso        string    `json:"endDateIso"`
	ClobTokenIds      string    `json:"clobTokenIds"`
	Tokens            []Token   `json:"tokens"`
	Active            bool      `json:"active"`
	Closed            bool      `json:"closed"`
	PriceToBeat       float64   `json:"priceToBeat"`
	PriceToBeatSource string    `json:"priceToBeatSource"`
	StartTime         time.Time `json:"startTime"`
	EndTime           time.Time `json:"endTime"`
	Outcomes          []string  `json:"outcomes"`
	MarketStale       bool      `json:"marketStale"`
	ResolutionURL     string    `json:"resolutionUrl"`
}

type Client struct {
	httpClient         *http.Client
	baseURL            string
	cryptoPriceBaseURL string
}

func NewClient() *Client {
	return NewClientWithBaseURLs(defaultGammaBaseURL, defaultCryptoPriceBaseURL, &http.Client{Timeout: 10 * time.Second})
}

func NewClientWithBaseURL(baseURL string, httpClient *http.Client) *Client {
	return NewClientWithBaseURLs(baseURL, defaultCryptoPriceBaseURL, httpClient)
}

func NewClientWithBaseURLs(baseURL, cryptoPriceBaseURL string, httpClient *http.Client) *Client {
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 10 * time.Second}
	}
	return &Client{httpClient: httpClient, baseURL: strings.TrimRight(baseURL, "/"), cryptoPriceBaseURL: strings.TrimRight(cryptoPriceBaseURL, "/")}
}

func BTC5mWindowStart(t time.Time) time.Time {
	u := t.UTC().Unix()
	return time.Unix(u-(u%300), 0).UTC()
}

func BTC5mEventSlug(start time.Time) string {
	return fmt.Sprintf("btc-updown-5m-%d", BTC5mWindowStart(start).Unix())
}

type gammaEvent struct {
	Slug             string        `json:"slug"`
	Title            string        `json:"title"`
	ResolutionSource string        `json:"resolutionSource"`
	Active           bool          `json:"active"`
	Closed           bool          `json:"closed"`
	Markets          []gammaMarket `json:"markets"`
}

type gammaMarket struct {
	ID            string `json:"id"`
	Question      string `json:"question"`
	Slug          string `json:"slug"`
	EndDate       string `json:"endDate"`
	EndDateIso    string `json:"endDateIso"`
	ClobTokenIds  string `json:"clobTokenIds"`
	Outcomes      string `json:"outcomes"`
	OutcomePrices string `json:"outcomePrices"`
	Active        bool   `json:"active"`
	Closed        bool   `json:"closed"`
}

func (c *Client) FetchActiveBTC5mMarket() (*Market, error) {
	return c.FetchActiveBTC5mMarketAt(time.Now().UTC())
}

func (c *Client) FetchActiveBTC5mMarketAt(now time.Time) (*Market, error) {
	start := BTC5mWindowStart(now)
	eventSlug := BTC5mEventSlug(start)
	req, err := http.NewRequest(http.MethodGet, c.baseURL+"/events/slug/"+eventSlug, nil)
	if err != nil { return nil, err }
	req.Header.Set("Accept", "application/json")
	resp, err := c.httpClient.Do(req)
	if err != nil { return nil, err }
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK { return nil, fmt.Errorf("gamma event lookup %s returned status %d", eventSlug, resp.StatusCode) }

	var ev gammaEvent
	if err := json.NewDecoder(resp.Body).Decode(&ev); err != nil { return nil, fmt.Errorf("decode gamma event %s: %w", eventSlug, err) }
	if ev.Closed || !ev.Active { return nil, fmt.Errorf("event %s is not active", eventSlug) }

	var gm *gammaMarket
	for i := range ev.Markets {
		if ev.Markets[i].Active && !ev.Markets[i].Closed { gm = &ev.Markets[i]; break }
	}
	if gm == nil { return nil, fmt.Errorf("event %s has no active market", eventSlug) }

	end := start.Add(btc5mWindow)
	if !now.UTC().Before(end) { return nil, fmt.Errorf("event %s has ended", eventSlug) }
	outcomes := parseJSONStringSlice(gm.Outcomes)
	ids := parseJSONStringSlice(gm.ClobTokenIds)
	prices := parseJSONFloatSlice(gm.OutcomePrices)
	tokens := make([]Token, 0, len(ids))
	for i, id := range ids {
		t := Token{TokenID: id}
		if i < len(outcomes) { t.Outcome = outcomes[i] }
		if i < len(prices) { t.Price = prices[i] }
		tokens = append(tokens, t)
	}
	q := gm.Question; if q == "" { q = ev.Title }
	return &Market{ID: gm.ID, Question: q, Slug: gm.Slug, EventSlug: eventSlug, EndDate: gm.EndDate, EndDateIso: gm.EndDateIso, ClobTokenIds: gm.ClobTokenIds, Tokens: tokens, Active: gm.Active, Closed: gm.Closed, StartTime: start, EndTime: end, Outcomes: outcomes, ResolutionURL: ev.ResolutionSource}, nil
}

// FetchPriceToBeat reads Polymarket's read-only crypto reference-price endpoint.
// On any failure the caller must emit NO_SIGNAL instead of inventing a price.
func (c *Client) FetchPriceToBeat(m *Market) (float64, error) {
	if m == nil || m.StartTime.IsZero() || m.EndTime.IsZero() { return 0, fmt.Errorf("market window incomplete") }
	q := url.Values{}
	q.Set("symbol", "BTC")
	q.Set("eventStartTime", m.StartTime.UTC().Format(time.RFC3339))
	q.Set("variant", "fiveminute")
	q.Set("endDate", m.EndTime.UTC().Format(time.RFC3339))
	req, err := http.NewRequest(http.MethodGet, c.cryptoPriceBaseURL+"?"+q.Encode(), nil)
	if err != nil { return 0, err }
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "Mozilla/5.0 PM-Edge-Research/1.0")
	req.Header.Set("Referer", "https://polymarket.com/")
	resp, err := c.httpClient.Do(req)
	if err != nil { return 0, err }
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK { return 0, fmt.Errorf("reference-price status %d", resp.StatusCode) }
	var p struct{ OpenPrice json.RawMessage `json:"openPrice"` }
	if err := json.NewDecoder(resp.Body).Decode(&p); err != nil { return 0, err }
	v, ok := parseNumber(p.OpenPrice); if !ok || v <= 0 { return 0, fmt.Errorf("reference-price response missing openPrice") }
	return v, nil
}

func parseJSONStringSlice(raw string) []string {
	if strings.TrimSpace(raw) == "" { return nil }
	var out []string; if json.Unmarshal([]byte(raw), &out) == nil { return out }; return nil
}
func parseJSONFloatSlice(raw string) []float64 {
	vals := parseJSONStringSlice(raw); out := make([]float64, 0, len(vals))
	for _, s := range vals { if v, err := strconv.ParseFloat(s, 64); err == nil { out = append(out, v) } }
	return out
}
func parseNumber(raw json.RawMessage) (float64, bool) {
	var v float64; if json.Unmarshal(raw, &v) == nil { return v, true }
	var s string; if json.Unmarshal(raw, &s) == nil { v, err := strconv.ParseFloat(s, 64); return v, err == nil }
	return 0, false
}
