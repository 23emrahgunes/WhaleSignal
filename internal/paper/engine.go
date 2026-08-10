package paper

import (
	"fmt"
	"strings"
	"sync"
	"time"

	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
)

type Config struct {
	Enabled         bool
	InitialBalance  float64
	Stake           float64
	MinConfidence   float64
	MinSecondsToEnd float64
	MaxSecondsToEnd float64
}

type Engine struct {
	db  *storage.Database
	cfg Config
	mu  sync.Mutex
}

func NewEngine(db *storage.Database, cfg Config) *Engine {
	return &Engine{db: db, cfg: cfg}
}

func (e *Engine) Enabled() bool { return e != nil && e.cfg.Enabled }

func (e *Engine) MaybeOpen(res *engine.EvaluationResult, market *polymarket.Market, now time.Time) (*storage.PaperTrade, bool, error) {
	if !e.Enabled() || res == nil || market == nil {
		return nil, false, nil
	}
	e.mu.Lock()
	defer e.mu.Unlock()

	if res.Decision != "UP" && res.Decision != "DOWN" {
		return nil, false, nil
	}
	if market.MarketStale || !market.Active || market.Closed {
		return nil, false, nil
	}
	if res.Confidence < e.cfg.MinConfidence {
		return nil, false, nil
	}
	if res.SecondsRemaining < e.cfg.MinSecondsToEnd || res.SecondsRemaining > e.cfg.MaxSecondsToEnd {
		return nil, false, nil
	}
	if strings.Contains(strings.ToUpper(res.DataSource), "MOCK") {
		return nil, false, nil
	}

	entryPrice, ok := outcomePrice(market, res.Decision)
	if !ok || entryPrice <= 0 || entryPrice >= 1 {
		return nil, false, nil
	}
	stats, err := e.db.GetPaperStats(e.cfg.InitialBalance)
	if err != nil {
		return nil, false, err
	}
	if stats.CashBalance+1e-9 < e.cfg.Stake {
		return nil, false, nil
	}
	entryProbability := res.PDown
	if res.Decision == "UP" {
		entryProbability = res.PUp
	}
	trade := &storage.PaperTrade{
		MarketSlug:          market.EventSlug,
		Question:            market.Question,
		Side:                res.Decision,
		EntryTime:           now.UTC().Format(time.RFC3339Nano),
		MarketEndTime:       market.EndTime.UTC().Format(time.RFC3339),
		EntryConfidence:     res.Confidence,
		EntryFinalScore:     res.FinalScore,
		EntryProbability:    entryProbability,
		EntryPrice:          entryPrice,
		Stake:               e.cfg.Stake,
		Shares:              e.cfg.Stake / entryPrice,
		PriceToBeat:         res.PriceToBeat,
		EntryReferencePrice: res.CurrentPrice,
		Status:              "OPEN",
		Source:              res.DataSource,
	}
	created, err := e.db.CreatePaperTrade(trade)
	if err != nil || !created {
		return trade, false, err
	}
	return trade, true, nil
}

// SettleReady closes any OPEN position once Chainlink has captured the exact
// five-minute end-boundary price. The BTC 5m rule is UP on equality or above.
func (e *Engine) SettleReady(now time.Time, boundaryPrice func(time.Time) (float64, bool)) (int, error) {
	if !e.Enabled() || boundaryPrice == nil {
		return 0, nil
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	openTrades, err := e.db.GetOpenPaperTrades()
	if err != nil {
		return 0, err
	}
	settled := 0
	for _, trade := range openTrades {
		endTime, err := time.Parse(time.RFC3339, trade.MarketEndTime)
		if err != nil {
			return settled, fmt.Errorf("paper trade %d invalid end time: %w", trade.ID, err)
		}
		if now.UTC().Before(endTime.UTC()) {
			continue
		}
		closePrice, ok := boundaryPrice(endTime.UTC())
		if !ok || closePrice <= 0 {
			continue
		}
		outcome := "DOWN"
		if closePrice >= trade.PriceToBeat {
			outcome = "UP"
		}
		won := outcome == trade.Side
		payout := 0.0
		if won {
			payout = trade.Shares
		}
		pnl := payout - trade.Stake
		if err := e.db.SettlePaperTrade(trade.ID, now.UTC().Format(time.RFC3339Nano), closePrice, outcome, won, payout, pnl); err != nil {
			return settled, err
		}
		settled++
	}
	return settled, nil
}

func outcomePrice(market *polymarket.Market, side string) (float64, bool) {
	wanted := strings.ToUpper(strings.TrimSpace(side))
	for _, token := range market.Tokens {
		if strings.ToUpper(strings.TrimSpace(token.Outcome)) == wanted && token.Price > 0 {
			return token.Price, true
		}
	}
	for i, outcome := range market.Outcomes {
		if strings.ToUpper(strings.TrimSpace(outcome)) != wanted || i >= len(market.Tokens) {
			continue
		}
		if market.Tokens[i].Price > 0 {
			return market.Tokens[i].Price, true
		}
	}
	return 0, false
}
