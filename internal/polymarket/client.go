package polymarket

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const (
	defaultGammaBaseURL = "https://gamma-api.polymarket.com"
	btc5mWindow         = 5 * time.Minute
)

type Token struct {
	Outcome string  `json:"outcome"`
	Price   float64 `json:"price"`
	TokenID string  `json:"tokenId"`
}

type Market struct {
	ID            string    `json:"id"`
	Question      string    `json:"question"`
	Slug          string    `json:"slug"`
	EventSlug     string    `json:"eventSlug"`
	EndDate       string    `json:"endDate"`
	EndDateIso    string    `json:"endDateIso"`
	ClobTokenIds  string    `json:"clobTokenIds"`
	Tokens        []Token   `json:"tokens"`
	Active        bool      `json:"active"`
	Closed        bool      `json:"closed"`
	PriceToBeat   float64   `json:"priceToBeat"`
	StartTime     time.Time `json:"startTime"`
	EndTime       time.Time `json:"endTime"`
	Outcomes      []string  `json:"outcomes"`
	MarketStale   bool      `json:"marketStale"`
	ResolutionURL string    `json:"resolutionUrl"`
}

type Client struct {
	httpClient *http.Client
	baseURL    string
}

func NewClient() *Client {
	return NewClientWithBaseURL(defaultGammaBaseURL, &http.Client{Timeout: 10 * time.Second})
}

func NewClientWithBaseURL(baseURL string, httpClient *http.Client) *Client {
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 10 * time.Second}
	}
	return &Client{
		httpClient: httpClient,
		baseURL:    strings.TrimRight(baseURL, "/"),
	}
}

// BTC5mWindowStart returns the UTC start boundary for the 5-minute market containing t.
func BTC5mWindowStart(t time.Time) time.Time {
	unix := t.UTC().Unix()
	start := unix - (unix % int64(btc5mWindow/time.Second))
	return time.Unix(start, 0).UTC()
}

// BTC5mEventSlug returns the canonical Polymarket event slug for a 5-minute BTC window.
func BTC5mEventSlug(start time.Time) string {
	return fmt.Sprintf("btc-updown-5m-%d", BTC5mWindowStart(start).Unix())
}

type gammaEvent struct {
	ID               string        `json:"id"`
	Slug             string        `json:"slug"`
	Title            string        `json:"title"`
	ResolutionSource string        `json:"resolutionSource"`
	Active           bool          `json:"active"`
	Closed           bool          `json:"closed"`
	Markets          []gammaMarket `json:"markets"`
}

type gammaMarket struct {
	ID             string `json:"id"`
	Question       string `json:"question"`
	Slug           string `json:"slug"`
	EndDate        string `json:"endDate"`
	EndDateIso     string `json:"endDateIso"`
	EventStartTime string `json:"eventStartTime"`
	ClobTokenIds   string `json:"clobTokenIds"`
	Outcomes       string `json:"outcomes"`
	OutcomePrices  string `json:"outcomePrices"`
	Active         bool   `json:"active"`
	Closed         bool   `json:"closed"`
}

// FetchActiveBTC5mMarket resolves the current BTC Up/Down 5-minute event by its canonical slug.
// It deliberately does not invent a fallback market: callers must treat an error as NO_SIGNAL.
func (c *Client) FetchActiveBTC5mMarket(now time.Time) (*Market, error) {
	windowStart := BTC5mWindowStart(now)
	eventSlug := BTC5mEventSlug(windowStart)
	url := fmt.Sprintf("%s/events/slug/%s", c.baseURL, eventSlug)

	req, err := http.NewRequest(http.MethodGet, url, nil)
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

	var event gammaEvent
	if err := json.NewDecoder(resp.Body).Decode(&event); err != nil {
		return nil, fmt.Errorf("decode gamma event %s: %w", eventSlug, err)
	}
	if event.Closed || !event.Active {
		return nil, fmt.Errorf("event %s is not active", eventSlug)
	}
	if len(event.Markets) == 0 {
		return nil, fmt.Errorf("event %s has no markets", eventSlug)
	}

	var gm *gammaMarket
	for i := range event.Markets {
		if event.Markets[i].Active && !event.Markets[i].Closed {
			gm = &event.Markets[i]
			break
		}
	}
	if gm == nil {
		return nil, fmt.Errorf("event %s has no active market", eventSlug)
	}

	startTime := windowStart
	if parsed, err := parseRFC3339(gm.EventStartTime); err == nil {
		// Use Gamma's eventStartTime only when it agrees with the canonical 5-minute window.
		if absDuration(parsed.Sub(windowStart)) <= 30*time.Second {
			startTime = parsed
		}
	}

	endTime := windowStart.Add(btc5mWindow)
	if parsed, err := parseRFC3339(gm.EndDate); err == nil {
		if absDuration(parsed.Sub(endTime)) <= time.Minute {
			endTime = parsed
		}
	}

	if !now.UTC().Before(endTime) {
		return nil, fmt.Errorf("event %s has already ended", eventSlug)
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

	question := gm.Question
	if question == "" {
		question = event.Title
	}

	return &Market{
		ID:            gm.ID,
		Question:      question,
		Slug:          gm.Slug,
		EventSlug:     eventSlug,
		EndDate:       gm.EndDate,
		EndDateIso:    gm.EndDateIso,
		ClobTokenIds:  gm.ClobTokenIds,
		Tokens:        tokens,
		Active:        gm.Active,
		Closed:        gm.Closed,
		StartTime:     startTime.UTC(),
		EndTime:       endTime.UTC(),
		Outcomes:      outcomes,
		ResolutionURL: event.ResolutionSource,
	}, nil
}

func parseJSONStringSlice(raw string) []string {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	var out []string
	if err := json.Unmarshal([]byte(raw), &out); err == nil {
		return out
	}
	return nil
}

func parseJSONFloatSlice(raw string) []float64 {
	vals := parseJSONStringSlice(raw)
	out := make([]float64, 0, len(vals))
	for _, v := range vals {
		f, err := strconv.ParseFloat(v, 64)
		if err == nil {
			out = append(out, f)
		}
	}
	return out
}

func parseRFC3339(s string) (time.Time, error) {
	if strings.TrimSpace(s) == "" {
		return time.Time{}, fmt.Errorf("empty time")
	}
	return time.Parse(time.RFC3339, s)
}

func absDuration(d time.Duration) time.Duration {
	if d < 0 {
		return -d
	}
	return d
}
