package polymarket

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
)

const defaultCLOBBaseURL = "https://clob.polymarket.com"

type CLOBLevel struct {
	Price float64
	Size  float64
}

type BuyQuote struct {
	TokenID      string  `json:"tokenId"`
	BestAsk      float64 `json:"bestAsk"`
	AveragePrice float64 `json:"averagePrice"`
	Shares       float64 `json:"shares"`
	Notional     float64 `json:"notional"`
	Fee          float64 `json:"fee"`
	TotalCost    float64 `json:"totalCost"`
	MinOrderSize float64 `json:"minOrderSize"`
	LevelsUsed   int     `json:"levelsUsed"`
}

type clobBookResponse struct {
	Asks []struct {
		Price string `json:"price"`
		Size  string `json:"size"`
	} `json:"asks"`
	MinOrderSize string `json:"min_order_size"`
}

// FetchBuyQuoteForShares estimates the USDC cost needed to acquire a target
// number of shares by walking the ask book. This is used internally for shadow
// hedge sizing; it does not model a resting share-denominated limit order.
func (c *Client) FetchBuyQuoteForShares(tokenID string, targetShares, feeRate, latencyBuffer float64) (BuyQuote, error) {
	return c.fetchBuyQuote(defaultCLOBBaseURL, tokenID, targetShares, 0, feeRate, latencyBuffer)
}

// FetchBuyQuoteForBudget models a Polymarket market BUY. BUY amount is supplied
// in dollars/USDC and the resulting share count is an execution result. The
// orderbook min_order_size metadata applies to share-sized book orders and is
// intentionally not exposed as an execution gate for a dollar market BUY.
func (c *Client) FetchBuyQuoteForBudget(tokenID string, budget, feeRate, latencyBuffer float64) (BuyQuote, error) {
	q, err := c.fetchBuyQuote(defaultCLOBBaseURL, tokenID, 0, budget, feeRate, latencyBuffer)
	q.MinOrderSize = 0 // not applicable to USDC-denominated market BUY gating
	return q, err
}

func (c *Client) fetchBuyQuote(baseURL, tokenID string, targetShares, budget, feeRate, latencyBuffer float64) (BuyQuote, error) {
	q := BuyQuote{TokenID: tokenID}
	if strings.TrimSpace(tokenID) == "" {
		return q, fmt.Errorf("missing token id")
	}
	if targetShares <= 0 && budget <= 0 {
		return q, fmt.Errorf("target shares or budget must be positive")
	}
	if feeRate < 0 {
		feeRate = 0
	}
	if latencyBuffer < 0 {
		latencyBuffer = 0
	}
	endpoint := strings.TrimRight(baseURL, "/") + "/book?" + url.Values{"token_id": []string{tokenID}}.Encode()
	resp, err := c.httpClient.Get(endpoint)
	if err != nil {
		return q, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return q, fmt.Errorf("clob book status %d", resp.StatusCode)
	}
	var raw clobBookResponse
	if err := json.NewDecoder(resp.Body).Decode(&raw); err != nil {
		return q, err
	}
	if v, err := strconv.ParseFloat(raw.MinOrderSize, 64); err == nil && v > 0 {
		q.MinOrderSize = v
	}
	levels := make([]CLOBLevel, 0, len(raw.Asks))
	for _, row := range raw.Asks {
		p, ep := strconv.ParseFloat(row.Price, 64)
		s, es := strconv.ParseFloat(row.Size, 64)
		if ep == nil && es == nil && p > 0 && p < 1 && s > 0 {
			levels = append(levels, CLOBLevel{Price: p, Size: s})
		}
	}
	if len(levels) == 0 {
		return q, fmt.Errorf("no asks for token %s", tokenID)
	}
	sort.Slice(levels, func(i, j int) bool { return levels[i].Price < levels[j].Price })
	q.BestAsk = levels[0].Price

	remainingShares := targetShares
	remainingBudget := budget
	for _, level := range levels {
		execPrice := math.Min(0.9999, level.Price+latencyBuffer)
		feePerShare := feeRate * execPrice * (1.0 - execPrice)
		unitCost := execPrice + feePerShare
		if unitCost <= 0 {
			continue
		}
		fill := level.Size
		if targetShares > 0 && fill > remainingShares {
			fill = remainingShares
		}
		if budget > 0 {
			maxByBudget := remainingBudget / unitCost
			if fill > maxByBudget {
				fill = maxByBudget
			}
		}
		if fill <= 0 {
			continue
		}
		q.Shares += fill
		q.Notional += fill * execPrice
		q.Fee += fill * feePerShare
		q.TotalCost += fill * unitCost
		q.LevelsUsed++
		if targetShares > 0 {
			remainingShares -= fill
			if remainingShares <= 1e-9 {
				break
			}
		}
		if budget > 0 {
			remainingBudget -= fill * unitCost
			if remainingBudget <= 1e-9 {
				break
			}
		}
	}
	if q.Shares <= 0 {
		return q, fmt.Errorf("insufficient ask liquidity")
	}
	if targetShares > 0 && q.Shares+1e-9 < targetShares {
		return q, fmt.Errorf("insufficient ask liquidity: got %.6f of %.6f shares", q.Shares, targetShares)
	}
	q.AveragePrice = q.Notional / q.Shares
	return q, nil
}

func TokenIDForOutcome(market *Market, side string) (string, bool) {
	if market == nil {
		return "", false
	}
	wanted := strings.ToUpper(strings.TrimSpace(side))
	for _, token := range market.Tokens {
		if strings.ToUpper(strings.TrimSpace(token.Outcome)) == wanted && token.TokenID != "" {
			return token.TokenID, true
		}
	}
	return "", false
}
