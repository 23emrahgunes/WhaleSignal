package main

import (
	"errors"
	"strings"
	"testing"

	"pm-edge/internal/paper"
	"pm-edge/internal/polymarket"
)

func TestAdaptivePaperBudgetQuoteKeepsNominalBudgetWhenExecutable(t *testing.T) {
	budgetCalls := 0
	shareCalls := 0
	budgetQuote := paper.BudgetQuoteFunc(func(tokenID string, budget float64) (polymarket.BuyQuote, error) {
		budgetCalls++
		return polymarket.BuyQuote{TokenID: tokenID, Shares: 6, MinOrderSize: 5, TotalCost: budget, AveragePrice: 0.40}, nil
	})
	shareQuote := paper.ShareQuoteFunc(func(tokenID string, shares float64) (polymarket.BuyQuote, error) {
		shareCalls++
		return polymarket.BuyQuote{}, nil
	})

	quote := makeAdaptivePaperBudgetQuote(budgetQuote, shareQuote, 5.25)
	q, err := quote("up", 2.50)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if budgetCalls != 1 || shareCalls != 0 {
		t.Fatalf("unexpected quote calls budget=%d shares=%d", budgetCalls, shareCalls)
	}
	if q.TotalCost != 2.50 || q.Shares != 6 {
		t.Fatalf("nominal quote changed: %+v", q)
	}
}

func TestAdaptivePaperBudgetQuoteTopUpsExactlyToMinShares(t *testing.T) {
	budgetQuote := paper.BudgetQuoteFunc(func(tokenID string, budget float64) (polymarket.BuyQuote, error) {
		return polymarket.BuyQuote{TokenID: tokenID, BestAsk: 0.70, Shares: 3.4, MinOrderSize: 5, TotalCost: budget}, errors.New("quote 3.400000 shares below min_order_size 5.000000")
	})
	shareCalls := 0
	shareQuote := paper.ShareQuoteFunc(func(tokenID string, shares float64) (polymarket.BuyQuote, error) {
		shareCalls++
		if shares != 5 {
			t.Fatalf("wanted exact market minimum 5 shares, got %.6f", shares)
		}
		return polymarket.BuyQuote{TokenID: tokenID, BestAsk: 0.70, AveragePrice: 0.70, Shares: 5, MinOrderSize: 5, TotalCost: 3.57}, nil
	})

	quote := makeAdaptivePaperBudgetQuote(budgetQuote, shareQuote, 5.25)
	q, err := quote("up", 2.50)
	if err != nil {
		t.Fatalf("adaptive quote failed: %v", err)
	}
	if shareCalls != 1 || q.Shares != 5 || q.TotalCost != 3.57 {
		t.Fatalf("unexpected adaptive quote: %+v calls=%d", q, shareCalls)
	}
}

func TestAdaptivePaperBudgetQuoteHonorsMaxStake(t *testing.T) {
	budgetQuote := paper.BudgetQuoteFunc(func(tokenID string, budget float64) (polymarket.BuyQuote, error) {
		return polymarket.BuyQuote{TokenID: tokenID, Shares: 2, MinOrderSize: 10, TotalCost: budget}, errors.New("below min_order_size")
	})
	shareQuote := paper.ShareQuoteFunc(func(tokenID string, shares float64) (polymarket.BuyQuote, error) {
		return polymarket.BuyQuote{TokenID: tokenID, Shares: shares, MinOrderSize: 10, TotalCost: 7.20}, nil
	})

	quote := makeAdaptivePaperBudgetQuote(budgetQuote, shareQuote, 5.25)
	_, err := quote("up", 2.50)
	if err == nil || !strings.Contains(err.Error(), "PAPER_MAX_STAKE") || !strings.Contains(err.Error(), "min_order_size") {
		t.Fatalf("expected bounded min-order error, got %v", err)
	}
}

func TestAdaptivePaperBudgetQuoteDoesNotMaskOtherErrors(t *testing.T) {
	original := errors.New("clob book status 503")
	budgetQuote := paper.BudgetQuoteFunc(func(tokenID string, budget float64) (polymarket.BuyQuote, error) {
		return polymarket.BuyQuote{}, original
	})
	quote := makeAdaptivePaperBudgetQuote(budgetQuote, nil, 5.25)
	_, err := quote("up", 2.50)
	if !errors.Is(err, original) {
		t.Fatalf("wanted original error, got %v", err)
	}
}
