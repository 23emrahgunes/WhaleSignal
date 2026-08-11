package paper

import (
	"math"
	"strings"
	"time"

	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
)

type EntryGateSnapshot struct {
	Timeframe                string  `json:"timeframe"`
	MarketSlug               string  `json:"marketSlug"`
	Allowed                  bool    `json:"allowed"`
	Reason                   string  `json:"reason"`
	Decision                 string  `json:"decision"`
	DirectionPass            bool    `json:"directionPass"`
	Confidence               float64 `json:"confidence"`
	MinConfidence            float64 `json:"minConfidence"`
	ConfidencePass           bool    `json:"confidencePass"`
	SecondsRemaining         float64 `json:"secondsRemaining"`
	MinSeconds               float64 `json:"minSeconds"`
	MaxSeconds               float64 `json:"maxSeconds"`
	TimePass                 bool    `json:"timePass"`
	FreshPass                bool    `json:"freshPass"`
	CashBalance              float64 `json:"cashBalance"`
	Stake                    float64 `json:"stake"`
	BalancePass              bool    `json:"balancePass"`
	BestAsk                  float64 `json:"bestAsk"`
	AveragePrice             float64 `json:"averagePrice"`
	EstimatedShares          float64 `json:"estimatedShares"`
	MinOrderSize             float64 `json:"minOrderSize"`
	TotalCost                float64 `json:"totalCost"`
	QuotePass                bool    `json:"quotePass"`
	MinSharesPass            bool    `json:"minSharesPass"`
	PositionExists           bool    `json:"positionExists"`
	PTBTerminalReady         bool    `json:"ptbTerminalReady"`
	PTBTerminalDecision      string  `json:"ptbTerminalDecision"`
	PTBTerminalProbability   float64 `json:"ptbTerminalProbability"`
	PTBTerminalDirectionPass bool    `json:"ptbTerminalDirectionPass"`
	EffectiveCost            float64 `json:"effectiveCost"`
	MaxEffectiveEntry        float64 `json:"maxEffectiveEntry"`
	EffectivePricePass       bool    `json:"effectivePricePass"`
	EconomicEdge             float64 `json:"economicEdge"`
	MinEconomicEdge          float64 `json:"minEconomicEdge"`
	EconomicEdgePass         bool    `json:"economicEdgePass"`
}

type HedgeGateSnapshot struct {
	Timeframe           string  `json:"timeframe"`
	MarketSlug          string  `json:"marketSlug"`
	Allowed             bool    `json:"allowed"`
	Reason              string  `json:"reason"`
	HasOpenPosition     bool    `json:"hasOpenPosition"`
	OriginalSide        string  `json:"originalSide"`
	ReverseSide         string  `json:"reverseSide"`
	DecisionPass        bool    `json:"decisionPass"`
	WindowSamples       int     `json:"windowSamples"`
	WindowSize          int     `json:"windowSize"`
	ReverseVotes        int     `json:"reverseVotes"`
	MinVotes            int     `json:"minVotes"`
	Consecutive         int     `json:"consecutive"`
	MinConsecutive      int     `json:"minConsecutive"`
	Persistence         float64 `json:"persistence"`
	SmoothedScore       float64 `json:"smoothedScore"`
	ScoreThreshold      float64 `json:"scoreThreshold"`
	ScorePass           bool    `json:"scorePass"`
	ReverseProbability  float64 `json:"reverseProbability"`
	MinProbability      float64 `json:"minProbability"`
	ProbabilityPass     bool    `json:"probabilityPass"`
	PTBZ                float64 `json:"ptbZ"`
	MinAbsPTBZ          float64 `json:"minAbsPtbZ"`
	PTBZPass            bool    `json:"ptbZPass"`
	SecondsRemaining    float64 `json:"secondsRemaining"`
	MinSeconds          float64 `json:"minSeconds"`
	MaxSeconds          float64 `json:"maxSeconds"`
	TimePass            bool    `json:"timePass"`
	BestAsk             float64 `json:"bestAsk"`
	AveragePrice        float64 `json:"averagePrice"`
	Shares              float64 `json:"shares"`
	TotalCost           float64 `json:"totalCost"`
	QuotePass           bool    `json:"quotePass"`
	Edge                float64 `json:"edge"`
	MinEdge             float64 `json:"minEdge"`
	EdgePass            bool    `json:"edgePass"`
	LockedPnL           float64 `json:"lockedPnl"`
	ExpectedHoldPnL     float64 `json:"expectedHoldPnl"`
	ExpectedImprovement float64 `json:"expectedImprovement"`
	ImprovementPass     bool    `json:"improvementPass"`
	HedgeExists         bool    `json:"hedgeExists"`
}

func (e *Engine) EntryGateSnapshot(res *engine.EvaluationResult, market *polymarket.Market, now time.Time, quote BudgetQuoteFunc) EntryGateSnapshot {
	g := EntryGateSnapshot{Timeframe: storage.NormalizeTimeframe(e.cfg.Timeframe), Reason: "WAITING_FOR_DATA", MinConfidence: e.cfg.MinConfidence, MinSeconds: e.cfg.MinSecondsToEnd, MaxSeconds: e.cfg.MaxSecondsToEnd, Stake: e.cfg.Stake, MaxEffectiveEntry: e.cfg.MaxEffectiveEntry, MinEconomicEdge: e.cfg.MinEconomicEdge}
	if market != nil {
		g.MarketSlug = market.EventSlug
	}
	if !e.Enabled() {
		g.Reason = "PAPER_DISABLED"
		return g
	}
	if res == nil || market == nil {
		return g
	}
	g.Decision = res.Decision
	g.Confidence = res.Confidence
	g.SecondsRemaining = res.SecondsRemaining
	g.DirectionPass = res.Decision == "UP" || res.Decision == "DOWN"
	if !g.DirectionPass {
		g.Reason = "NEUTRAL_SIGNAL"
		return g
	}
	g.PTBTerminalReady = res.PTBTerminal.Ready
	g.PTBTerminalDecision = res.PTBTerminal.Decision
	g.PTBTerminalProbability = res.PTBTerminal.PBelow
	if res.Decision == "UP" {
		g.PTBTerminalProbability = res.PTBTerminal.PAbove
	}
	if !g.PTBTerminalReady {
		g.Reason = "PTB_TERMINAL_NOT_READY"
		return g
	}
	g.PTBTerminalDirectionPass = res.PTBTerminal.Decision == res.Decision
	if !g.PTBTerminalDirectionPass {
		g.Reason = "PTB_TERMINAL_DIRECTION_MISMATCH"
		return g
	}
	g.ConfidencePass = res.Confidence >= e.cfg.MinConfidence
	if !g.ConfidencePass {
		g.Reason = "CONFIDENCE_BELOW_THRESHOLD"
		return g
	}
	g.TimePass = res.SecondsRemaining >= e.cfg.MinSecondsToEnd && res.SecondsRemaining <= e.cfg.MaxSecondsToEnd
	if !g.TimePass {
		g.Reason = "OUTSIDE_ENTRY_WINDOW"
		return g
	}
	g.FreshPass = !market.MarketStale && market.Active && !market.Closed && !strings.Contains(strings.ToUpper(res.DataSource), "MOCK")
	if !g.FreshPass {
		g.Reason = "DATA_NOT_FRESH_OR_MARKET_INACTIVE"
		return g
	}
	if storage.TimeframeFromMarketSlug(market.EventSlug) != storage.NormalizeTimeframe(e.cfg.Timeframe) {
		g.Reason = "TIMEFRAME_MISMATCH"
		return g
	}
	exists, err := e.db.PaperTradeExists(market.EventSlug)
	if err == nil && exists {
		g.PositionExists = true
		g.Reason = "POSITION_ALREADY_RECORDED"
		return g
	}
	stats, err := e.db.GetPaperStatsByTimeframe(e.cfg.InitialBalance, e.cfg.Timeframe)
	if err != nil {
		g.Reason = "BALANCE_LOOKUP_ERROR"
		return g
	}
	g.CashBalance = stats.CashBalance
	g.BalancePass = stats.CashBalance+1e-9 >= e.cfg.Stake
	if !g.BalancePass {
		g.Reason = "INSUFFICIENT_PAPER_BALANCE"
		return g
	}
	if quote == nil {
		g.Reason = "CLOB_QUOTE_UNAVAILABLE"
		return g
	}
	tokenID, ok := polymarket.TokenIDForOutcome(market, res.Decision)
	if !ok {
		g.Reason = "TOKEN_ID_UNAVAILABLE"
		return g
	}
	q, qerr := quote(tokenID, e.cfg.Stake)
	g.BestAsk = q.BestAsk
	g.AveragePrice = q.AveragePrice
	g.EstimatedShares = q.Shares
	g.MinOrderSize = q.MinOrderSize
	g.TotalCost = q.TotalCost
	g.MinSharesPass = q.MinOrderSize <= 0 || q.Shares+1e-9 >= q.MinOrderSize
	if qerr != nil {
		if strings.Contains(strings.ToLower(qerr.Error()), "min_order_size") {
			g.Reason = "MIN_ORDER_SIZE_NOT_MET"
		} else {
			g.Reason = "CLOB_QUOTE_ERROR"
		}
		return g
	}
	g.QuotePass = q.AveragePrice > 0 && q.AveragePrice < 1 && q.Shares > 0 && q.TotalCost > 0
	if !g.QuotePass {
		g.Reason = "CLOB_QUOTE_INVALID"
		return g
	}
	if !g.MinSharesPass {
		g.Reason = "MIN_ORDER_SIZE_NOT_MET"
		return g
	}
	g.EffectiveCost = q.TotalCost / q.Shares
	g.EffectivePricePass = g.EffectiveCost <= e.cfg.MaxEffectiveEntry+1e-12
	if !g.EffectivePricePass {
		g.Reason = "EFFECTIVE_ENTRY_PRICE_TOO_HIGH"
		return g
	}
	g.EconomicEdge = g.PTBTerminalProbability - g.EffectiveCost
	g.EconomicEdgePass = g.EconomicEdge >= e.cfg.MinEconomicEdge-1e-12
	if !g.EconomicEdgePass {
		g.Reason = "ECONOMIC_EDGE_BELOW_THRESHOLD"
		return g
	}
	g.Allowed = true
	g.Reason = "ENTRY_READY"
	return g
}

func (e *Engine) HedgeGateSnapshot(res *engine.EvaluationResult, market *polymarket.Market, now time.Time, quote ShareQuoteFunc) HedgeGateSnapshot {
	g := HedgeGateSnapshot{Timeframe: storage.NormalizeTimeframe(e.cfg.Timeframe), Reason: "NO_OPEN_POSITION", WindowSize: e.cfg.HedgeWindow, MinVotes: e.cfg.HedgeMinVotes, MinConsecutive: e.cfg.HedgeMinConsecutive, ScoreThreshold: e.cfg.HedgeScoreThreshold, MinProbability: e.cfg.HedgeMinProbability, MinAbsPTBZ: e.cfg.HedgeMinAbsPTBZ, MinSeconds: e.cfg.HedgeMinSecondsToEnd, MaxSeconds: e.cfg.HedgeMaxSecondsToEnd, MinEdge: e.cfg.HedgeMinEdge}
	if market != nil {
		g.MarketSlug = market.EventSlug
	}
	if !e.Enabled() || !e.cfg.HedgeEnabled {
		g.Reason = "HEDGE_DISABLED"
		return g
	}
	if market == nil {
		return g
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	openTrade, err := e.db.GetOpenPaperTradeByMarket(market.EventSlug)
	if err != nil || openTrade == nil {
		return g
	}
	g.HasOpenPosition = true
	g.OriginalSide = openTrade.Side
	g.Shares = openTrade.Shares
	if existing, err := e.db.GetPaperHedgeByTradeID(openTrade.ID); err == nil && existing != nil {
		g.HedgeExists = true
		g.Reason = "HEDGE_ALREADY_OPEN"
		return g
	}
	if res == nil {
		g.Reason = "WAITING_FOR_DATA"
		return g
	}
	g.SecondsRemaining = res.SecondsRemaining
	g.TimePass = res.SecondsRemaining >= e.cfg.HedgeMinSecondsToEnd && res.SecondsRemaining <= e.cfg.HedgeMaxSecondsToEnd
	if !g.TimePass {
		g.Reason = "OUTSIDE_HEDGE_WINDOW"
		return g
	}
	g.ReverseSide = "DOWN"
	g.ReverseProbability = res.PDown
	if openTrade.Side == "DOWN" {
		g.ReverseSide = "UP"
		g.ReverseProbability = res.PUp
	}
	g.DecisionPass = res.Decision == g.ReverseSide
	if !g.DecisionPass {
		g.Reason = "NO_REVERSE_DECISION"
		return g
	}
	g.ProbabilityPass = g.ReverseProbability >= e.cfg.HedgeMinProbability
	if !g.ProbabilityPass {
		g.Reason = "REVERSE_PROBABILITY_BELOW_THRESHOLD"
		return g
	}
	g.PTBZ = res.PTBZ
	if g.ReverseSide == "DOWN" {
		g.PTBZPass = res.PTBZ <= -e.cfg.HedgeMinAbsPTBZ
	} else {
		g.PTBZPass = res.PTBZ >= e.cfg.HedgeMinAbsPTBZ
	}
	if !g.PTBZPass {
		g.Reason = "PTB_Z_NOT_CONFIRMED"
		return g
	}
	rows := e.regimes[market.EventSlug]
	g.WindowSamples = len(rows)
	persistence, consecutive, smoothedScore, ready := e.regimeMetrics(market.EventSlug, g.ReverseSide)
	g.Persistence = persistence
	g.Consecutive = consecutive
	g.SmoothedScore = smoothedScore
	g.ReverseVotes = int(math.Round(persistence * float64(len(rows))))
	if !ready {
		g.Reason = "REGIME_WINDOW_NOT_FULL"
		return g
	}
	if g.ReverseVotes < e.cfg.HedgeMinVotes {
		g.Reason = "REVERSE_VOTES_BELOW_THRESHOLD"
		return g
	}
	if g.Consecutive < e.cfg.HedgeMinConsecutive {
		g.Reason = "REVERSE_STREAK_BELOW_THRESHOLD"
		return g
	}
	if g.ReverseSide == "DOWN" {
		g.ScorePass = smoothedScore <= -e.cfg.HedgeScoreThreshold
	} else {
		g.ScorePass = smoothedScore >= e.cfg.HedgeScoreThreshold
	}
	if !g.ScorePass {
		g.Reason = "EWMA_SCORE_NOT_CONFIRMED"
		return g
	}
	if quote == nil {
		g.Reason = "CLOB_QUOTE_UNAVAILABLE"
		return g
	}
	tokenID, ok := polymarket.TokenIDForOutcome(market, g.ReverseSide)
	if !ok {
		g.Reason = "TOKEN_ID_UNAVAILABLE"
		return g
	}
	q, qerr := quote(tokenID, openTrade.Shares)
	g.BestAsk = q.BestAsk
	g.AveragePrice = q.AveragePrice
	g.TotalCost = q.TotalCost
	if qerr != nil || q.Shares+1e-9 < openTrade.Shares || q.TotalCost <= 0 {
		g.Reason = "HEDGE_CLOB_QUOTE_ERROR"
		return g
	}
	g.QuotePass = true
	effectiveCostPerShare := q.TotalCost / q.Shares
	g.Edge = g.ReverseProbability - effectiveCostPerShare
	g.EdgePass = g.Edge >= e.cfg.HedgeMinEdge
	if !g.EdgePass {
		g.Reason = "REVERSE_EDGE_BELOW_THRESHOLD"
		return g
	}
	g.LockedPnL = openTrade.Shares - openTrade.Stake - q.TotalCost
	originalWinProbability := 1.0 - g.ReverseProbability
	g.ExpectedHoldPnL = originalWinProbability*openTrade.Shares - openTrade.Stake
	g.ExpectedImprovement = g.LockedPnL - g.ExpectedHoldPnL
	g.ImprovementPass = g.ExpectedImprovement > 0
	if !g.ImprovementPass {
		g.Reason = "HEDGE_DOES_NOT_IMPROVE_EXPECTANCY"
		return g
	}
	g.Allowed = true
	g.Reason = "HEDGE_READY"
	return g
}
