package main

import (
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"

	"pm-edge/internal/paper"
	"pm-edge/internal/polymarket"
)

const defaultAdaptivePaperMaxStake = 5.25

// resolvePaperMaxStake keeps PAPER_STAKE as the nominal research stake while
// allowing a bounded top-up when Polymarket's market-specific min_order_size
// requires more capital to obtain the minimum executable number of shares.
func resolvePaperMaxStake(nominalStake float64) float64 {
	maxStake := math.Max(defaultAdaptivePaperMaxStake, nominalStake)
	raw := strings.TrimSpace(os.Getenv("PAPER_MAX_STAKE"))
	if raw == "" {
		return maxStake
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil || v <= 0 {
		return maxStake
	}
	if v < nominalStake {
		return nominalStake
	}
	return v
}

// makeAdaptivePaperBudgetQuote preserves the configured nominal USDC budget
// whenever it already satisfies the CLOB minimum. If the only failure is
// min_order-size, it requotes exactly the market minimum number of shares and
// accepts that realistic paper fill only when its total cost stays below the
// configured paper max-stake cap.
func makeAdaptivePaperBudgetQuote(budgetQuote paper.BudgetQuoteFunc, shareQuote paper.ShareQuoteFunc, maxStake float64) paper.BudgetQuoteFunc {
	return func(tokenID string, budget float64) (polymarket.BuyQuote, error) {
		q, err := budgetQuote(tokenID, budget)
		if err == nil {
			return q, nil
		}
		if q.MinOrderSize <= 0 || !strings.Contains(strings.ToLower(err.Error()), "min_order_size") {
			return q, err
		}
		if shareQuote == nil {
			return q, err
		}

		minimumQuote, minimumErr := shareQuote(tokenID, q.MinOrderSize)
		if minimumErr != nil {
			return minimumQuote, minimumErr
		}
		capStake := maxStake
		if capStake <= 0 {
			capStake = math.Max(defaultAdaptivePaperMaxStake, budget)
		}
		if capStake < budget {
			capStake = budget
		}
		if minimumQuote.TotalCost > capStake+1e-9 {
			return minimumQuote, fmt.Errorf(
				"min_order_size requires %.6f USDC but PAPER_MAX_STAKE is %.6f",
				minimumQuote.TotalCost,
				capStake,
			)
		}
		return minimumQuote, nil
	}
}
