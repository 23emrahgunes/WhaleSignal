package paper

import (
	"fmt"
	"math"
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

	TakerFeeRate  float64
	LatencyBuffer float64

	HedgeEnabled         bool
	HedgeWindow          int
	HedgeMinVotes        int
	HedgeMinConsecutive  int
	HedgeScoreThreshold  float64
	HedgeMinProbability  float64
	HedgeMinEdge         float64
	HedgeMinAbsPTBZ      float64
	HedgeMinSecondsToEnd float64
	HedgeMaxSecondsToEnd float64
}

type regimeSample struct {
	Decision string
	Score    float64
}

type Engine struct {
	db      *storage.Database
	cfg     Config
	mu      sync.Mutex
	regimes map[string][]regimeSample
}

type BudgetQuoteFunc func(tokenID string, budget float64) (polymarket.BuyQuote, error)
type ShareQuoteFunc func(tokenID string, shares float64) (polymarket.BuyQuote, error)

func NewEngine(db *storage.Database, cfg Config) *Engine {
	if cfg.TakerFeeRate <= 0 {
		cfg.TakerFeeRate = 0.07
	}
	if cfg.LatencyBuffer < 0 {
		cfg.LatencyBuffer = 0
	}
	if cfg.HedgeWindow <= 0 {
		cfg.HedgeWindow = 8
	}
	if cfg.HedgeMinVotes <= 0 || cfg.HedgeMinVotes > cfg.HedgeWindow {
		cfg.HedgeMinVotes = int(math.Ceil(float64(cfg.HedgeWindow) * 0.75))
	}
	if cfg.HedgeMinConsecutive <= 0 {
		cfg.HedgeMinConsecutive = 3
	}
	if cfg.HedgeScoreThreshold <= 0 {
		cfg.HedgeScoreThreshold = 0.35
	}
	if cfg.HedgeMinProbability <= 0 {
		cfg.HedgeMinProbability = 0.65
	}
	if cfg.HedgeMinEdge <= 0 {
		cfg.HedgeMinEdge = 0.03
	}
	if cfg.HedgeMinAbsPTBZ <= 0 {
		cfg.HedgeMinAbsPTBZ = 0.50
	}
	if cfg.HedgeMinSecondsToEnd <= 0 {
		cfg.HedgeMinSecondsToEnd = 20
	}
	if cfg.HedgeMaxSecondsToEnd <= 0 {
		cfg.HedgeMaxSecondsToEnd = 120
	}
	return &Engine{db: db, cfg: cfg, regimes: make(map[string][]regimeSample)}
}

func (e *Engine) Enabled() bool { return e != nil && e.cfg.Enabled }

// MaybeOpen preserves the legacy midpoint-based path for deterministic unit
// tests and tooling. Production should call MaybeOpenWithQuote so paper fills
// use the real CLOB ask/VWAP, taker fee and min-order-size constraints.
func (e *Engine) MaybeOpen(res *engine.EvaluationResult, market *polymarket.Market, now time.Time) (*storage.PaperTrade, bool, error) {
	return e.maybeOpen(res, market, now, nil)
}

func (e *Engine) MaybeOpenWithQuote(res *engine.EvaluationResult, market *polymarket.Market, now time.Time, quote BudgetQuoteFunc) (*storage.PaperTrade, bool, error) {
	return e.maybeOpen(res, market, now, quote)
}

func (e *Engine) maybeOpen(res *engine.EvaluationResult, market *polymarket.Market, now time.Time, quote BudgetQuoteFunc) (*storage.PaperTrade, bool, error) {
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

	stats, err := e.db.GetPaperStats(e.cfg.InitialBalance)
	if err != nil {
		return nil, false, err
	}
	if stats.CashBalance+1e-9 < e.cfg.Stake {
		return nil, false, nil
	}

	entryPrice := 0.0
	stake := e.cfg.Stake
	shares := 0.0
	if quote != nil {
		tokenID, ok := polymarket.TokenIDForOutcome(market, res.Decision)
		if !ok {
			return nil, false, nil
		}
		q, err := quote(tokenID, e.cfg.Stake)
		if err != nil {
			return nil, false, err
		}
		entryPrice, stake, shares = q.AveragePrice, q.TotalCost, q.Shares
	} else {
		var ok bool
		entryPrice, ok = outcomePrice(market, res.Decision)
		if !ok || entryPrice <= 0 || entryPrice >= 1 {
			return nil, false, nil
		}
		shares = e.cfg.Stake / entryPrice
	}
	if entryPrice <= 0 || entryPrice >= 1 || stake <= 0 || shares <= 0 {
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
		Stake:               stake,
		Shares:              shares,
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

// MaybeHedge evaluates a full-share shadow hedge. It never uses a single last
// signal. A hedge requires an opposite persistent regime, hysteresis, terminal
// probability confirmation, PTB z-score confirmation and positive cost-adjusted
// edge after CLOB VWAP + taker fee + latency buffer.
func (e *Engine) MaybeHedge(res *engine.EvaluationResult, market *polymarket.Market, now time.Time, quote ShareQuoteFunc) (*storage.PaperHedge, bool, error) {
	if !e.Enabled() || !e.cfg.HedgeEnabled || res == nil || market == nil || quote == nil {
		return nil, false, nil
	}
	e.mu.Lock()
	defer e.mu.Unlock()

	e.observeRegime(market.EventSlug, res)
	openTrade, err := e.db.GetOpenPaperTradeByMarket(market.EventSlug)
	if err != nil || openTrade == nil {
		return nil, false, err
	}
	if existing, err := e.db.GetPaperHedgeByTradeID(openTrade.ID); err != nil {
		return nil, false, err
	} else if existing != nil {
		return existing, false, nil
	}
	if res.SecondsRemaining < e.cfg.HedgeMinSecondsToEnd || res.SecondsRemaining > e.cfg.HedgeMaxSecondsToEnd {
		return nil, false, nil
	}

	reverseSide := "DOWN"
	reverseProbability := res.PDown
	if openTrade.Side == "DOWN" {
		reverseSide = "UP"
		reverseProbability = res.PUp
	}
	if res.Decision != reverseSide || reverseProbability < e.cfg.HedgeMinProbability {
		return nil, false, nil
	}
	if reverseSide == "DOWN" {
		if res.PTBZ > -e.cfg.HedgeMinAbsPTBZ {
			return nil, false, nil
		}
	} else if res.PTBZ < e.cfg.HedgeMinAbsPTBZ {
		return nil, false, nil
	}

	persistence, consecutive, smoothedScore, ready := e.regimeMetrics(market.EventSlug, reverseSide)
	if !ready || consecutive < e.cfg.HedgeMinConsecutive {
		return nil, false, nil
	}
	minPersistence := float64(e.cfg.HedgeMinVotes) / float64(e.cfg.HedgeWindow)
	if persistence+1e-12 < minPersistence {
		return nil, false, nil
	}
	if reverseSide == "DOWN" && smoothedScore > -e.cfg.HedgeScoreThreshold {
		return nil, false, nil
	}
	if reverseSide == "UP" && smoothedScore < e.cfg.HedgeScoreThreshold {
		return nil, false, nil
	}

	tokenID, ok := polymarket.TokenIDForOutcome(market, reverseSide)
	if !ok {
		return nil, false, nil
	}
	q, err := quote(tokenID, openTrade.Shares)
	if err != nil {
		return nil, false, err
	}
	if q.Shares+1e-9 < openTrade.Shares || q.TotalCost <= 0 {
		return nil, false, nil
	}
	effectiveCostPerShare := q.TotalCost / q.Shares
	edge := reverseProbability - effectiveCostPerShare
	if edge < e.cfg.HedgeMinEdge {
		return nil, false, nil
	}

	lockedPnL := openTrade.Shares - openTrade.Stake - q.TotalCost
	originalWinProbability := 1.0 - reverseProbability
	expectedHoldPnL := originalWinProbability*openTrade.Shares - openTrade.Stake
	expectedImprovement := lockedPnL - expectedHoldPnL
	if expectedImprovement <= 0 {
		return nil, false, nil
	}

	h := &storage.PaperHedge{
		PaperTradeID:        openTrade.ID,
		MarketSlug:          market.EventSlug,
		OriginalSide:        openTrade.Side,
		Side:                reverseSide,
		HedgeTime:           now.UTC().Format(time.RFC3339Nano),
		EntryPrice:          q.AveragePrice,
		Shares:              q.Shares,
		Notional:            q.Notional,
		Fee:                 q.Fee,
		TotalCost:           q.TotalCost,
		ReverseProbability:  reverseProbability,
		Edge:                edge,
		Persistence:         persistence,
		SmoothedScore:       smoothedScore,
		PTBZ:                res.PTBZ,
		LockedPnL:           lockedPnL,
		ExpectedHoldPnL:     expectedHoldPnL,
		ExpectedImprovement: expectedImprovement,
		Status:              "OPEN",
	}
	created, err := e.db.CreatePaperHedge(h)
	if err != nil || !created {
		return h, false, err
	}
	return h, true, nil
}

func (e *Engine) observeRegime(slug string, res *engine.EvaluationResult) {
	decision := res.Decision
	if decision != "UP" && decision != "DOWN" {
		decision = "NEUTRAL"
	}
	rows := append(e.regimes[slug], regimeSample{Decision: decision, Score: res.FinalScore})
	if len(rows) > e.cfg.HedgeWindow {
		rows = rows[len(rows)-e.cfg.HedgeWindow:]
	}
	e.regimes[slug] = rows
}

func (e *Engine) regimeMetrics(slug, reverseSide string) (persistence float64, consecutive int, smoothedScore float64, ready bool) {
	rows := e.regimes[slug]
	if len(rows) < e.cfg.HedgeWindow {
		return 0, 0, 0, false
	}
	votes := 0
	for _, row := range rows {
		if row.Decision == reverseSide {
			votes++
		}
	}
	persistence = float64(votes) / float64(len(rows))
	for i := len(rows) - 1; i >= 0; i-- {
		if rows[i].Decision != reverseSide {
			break
		}
		consecutive++
	}
	const alpha = 0.35
	smoothedScore = rows[0].Score
	for i := 1; i < len(rows); i++ {
		smoothedScore = alpha*rows[i].Score + (1-alpha)*smoothedScore
	}
	return persistence, consecutive, smoothedScore, true
}

// SettleReady closes OPEN original positions and any attached shadow hedge at
// the exact Chainlink five-minute boundary. Original PnL remains untouched so
// A/B comparison (hold vs hedge) is measurable.
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
		settlementTime := now.UTC().Format(time.RFC3339Nano)
		if err := e.db.SettlePaperTrade(trade.ID, settlementTime, closePrice, outcome, won, payout, pnl); err != nil {
			return settled, err
		}

		if h, err := e.db.GetPaperHedgeByTradeID(trade.ID); err != nil {
			return settled, err
		} else if h != nil && h.Status == "OPEN" {
			hPayout := 0.0
			if outcome == h.Side {
				hPayout = h.Shares
			}
			hPnL := hPayout - h.TotalCost
			combined := pnl + hPnL
			if err := e.db.SettlePaperHedge(trade.ID, settlementTime, outcome, hPayout, hPnL, combined); err != nil {
				return settled, err
			}
		}
		delete(e.regimes, trade.MarketSlug)
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
