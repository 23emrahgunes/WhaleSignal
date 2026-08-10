package polymarket

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"pm-edge/internal/util"
	"go.uber.org/zap"
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
	EndDate       string    `json:"endDate"`       // ISO format end date (e.g. 2026-12-31T00:00:00Z)
	EndDateIso    string    `json:"endDateIso"`    // Simple date (e.g. 2026-12-31)
	ClobTokenIds  string    `json:"clobTokenIds"`  // String representation of token IDs
	Tokens        []Token   `json:"tokens"`        // Custom tokens populated or parsed
	Active        bool      `json:"active"`
	Closed        bool      `json:"closed"`
	PriceToBeat   float64   `json:"priceToBeat"`
	EndTime       time.Time `json:"endTime"`
	Outcomes      []string  `json:"outcomes"`
	MarketStale   bool      `json:"marketStale"`
}

type Client struct {
	httpClient *http.Client
	regexes    []*regexp.Regexp
}

func NewClient() *Client {
	patterns := []string{
		`(?i)bitcoin\s+above\s+\$?([0-9,]+(?:\.[0-9]+)?)(?:\s+at\s+([0-9:]+))?`,
		`(?i)btc\s+above\s+\$?([0-9,]+(?:\.[0-9]+)?)`,
		`(?i)btc\s*>\s*\$?([0-9,]+(?:\.[0-9]+)?)`,
		`(?i)bitcoin\s+over\s+\$?([0-9,]+(?:\.[0-9]+)?)`,
	}

	compiled := make([]*regexp.Regexp, 0, len(patterns))
	for _, p := range patterns {
		compiled = append(compiled, regexp.MustCompile(p))
	}

	return &Client{
		httpClient: &http.Client{Timeout: 10 * time.Second},
		regexes:    compiled,
	}
}

// ParsePriceToBeat extracts the target BTC price from Polymarket questions.
func (c *Client) ParsePriceToBeat(question string) (float64, bool) {
	for _, re := range c.regexes {
		matches := re.FindStringSubmatch(question)
		if len(matches) >= 2 {
			rawVal := matches[1]
			// Clean $ and commas
			cleaned := strings.ReplaceAll(rawVal, "$", "")
			cleaned = strings.ReplaceAll(cleaned, ",", "")
			cleaned = strings.TrimSpace(cleaned)

			val, err := strconv.ParseFloat(cleaned, 64)
			if err == nil {
				return val, true
			}
		}
	}
	return 0.0, false
}

// Is5MinMarket checks if a question refers to a 5-minute interval.
func (c *Client) Is5MinMarket(question string) bool {
	reTime := regexp.MustCompile(`(?i)(?:at|by)\s+([0-9]{1,2}:[0-9]{2})`)
	return reTime.MatchString(question)
}

// FetchActiveBTC5mMarket queries Gamma API and returns the closest active 5m BTC market.
func (c *Client) FetchActiveBTC5mMarket() (*Market, error) {
	url := "https://gamma-api.polymarket.com/markets?limit=100&active=true"
	resp, err := c.httpClient.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("unexpected Gamma API status: %d", resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	var rawMarkets []struct {
		ID           string          `json:"id"`
		Question     string          `json:"question"`
		Slug         string          `json:"slug"`
		EndDate      string          `json:"endDate"`
		EndDateIso   string          `json:"endDateIso"`
		ClobTokenIds string          `json:"clobTokenIds"`
		Closed       bool            `json:"closed"`
		Active       bool            `json:"active"`
		Outcomes     json.RawMessage `json:"outcomes"`
	}

	if err := json.Unmarshal(body, &rawMarkets); err != nil {
		return nil, err
	}

	var candidateMarkets []Market
	now := time.Now().UTC()

	for _, m := range rawMarkets {
		lowerQ := strings.ToLower(m.Question)
		if m.Closed || !m.Active {
			continue
		}
		if !strings.Contains(lowerQ, "btc") && !strings.Contains(lowerQ, "bitcoin") {
			continue
		}

		if !c.Is5MinMarket(m.Question) {
			continue
		}

		price, ok := c.ParsePriceToBeat(m.Question)
		if !ok {
			continue
		}

		var endTime time.Time
		var parseErr error
		if m.EndDate != "" {
			endTime, parseErr = time.Parse(time.RFC3339, m.EndDate)
			if parseErr != nil {
				endTime, parseErr = time.Parse("2006-01-02T15:04:05Z", m.EndDate)
			}
		}

		if parseErr != nil || endTime.Before(now) {
			continue
		}

		remaining := endTime.Sub(now)
		if remaining < 5*time.Second {
			continue
		}

		// Defensively parse Outcomes
		var outcomes []string
		if len(m.Outcomes) > 0 {
			if err := json.Unmarshal(m.Outcomes, &outcomes); err != nil {
				var str string
				if err := json.Unmarshal(m.Outcomes, &str); err == nil {
					_ = json.Unmarshal([]byte(str), &outcomes)
				}
			}
		}

		var clobTokens []Token
		if m.ClobTokenIds != "" {
			var ids []string
			if err := json.Unmarshal([]byte(m.ClobTokenIds), &ids); err == nil {
				for i, id := range ids {
					outcome := ""
					if i < len(outcomes) {
						outcome = outcomes[i]
					}
					clobTokens = append(clobTokens, Token{
						Outcome: outcome,
						TokenID: id,
					})
				}
			}
		}

		candidateMarkets = append(candidateMarkets, Market{
			ID:           m.ID,
			Question:     m.Question,
			Slug:         m.Slug,
			EndDate:      m.EndDate,
			EndDateIso:   m.EndDateIso,
			ClobTokenIds: m.ClobTokenIds,
			Tokens:       clobTokens,
			Active:       m.Active,
			Closed:       m.Closed,
			PriceToBeat:   price,
			EndTime:      endTime,
			Outcomes:     outcomes,
		})
	}

	if len(candidateMarkets) == 0 {
		return nil, fmt.Errorf("no active 5m BTC markets found")
	}

	bestIdx := 0
	minRemaining := candidateMarkets[0].EndTime.Sub(now)
	for i := 1; i < len(candidateMarkets); i++ {
		rem := candidateMarkets[i].EndTime.Sub(now)
		if rem < minRemaining {
			minRemaining = rem
			bestIdx = i
		}
	}

	util.Logger.Debug("Selected active BTC 5m market",
		zap.String("question", candidateMarkets[bestIdx].Question),
		zap.Float64("priceToBeat", candidateMarkets[bestIdx].PriceToBeat),
		zap.Time("endTime", candidateMarkets[bestIdx].EndTime),
	)

	return &candidateMarkets[bestIdx], nil
}
