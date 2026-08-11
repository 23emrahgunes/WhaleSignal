package main

import (
	"context"
	"strings"
	"time"

	"go.uber.org/zap"
	"pm-edge/internal/api"
	"pm-edge/internal/binance"
	"pm-edge/internal/chainlink"
	"pm-edge/internal/config"
	"pm-edge/internal/engine"
	"pm-edge/internal/paper"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
	"pm-edge/internal/util"
)

func startBTC15mRuntime(ctx context.Context, isMockMode bool, cfg *config.Config, db *storage.Database, server *api.Server, pmClient *polymarket.Client, bClient *binance.Client, clClient *chainlink.Client, microClient *binance.MicrostructureClient) {
	paperEngine := paper.NewEngine(db, paper.Config{
		Timeframe:            "15m",
		Enabled:              cfg.PaperEnabled && !isMockMode,
		InitialBalance:       cfg.PaperInitialBalance,
		Stake:                cfg.PaperStake,
		MinConfidence:        cfg.PaperMinConfidence,
		MinSecondsToEnd:      cfg.PaperMinSecondsToEnd * 3,
		MaxSecondsToEnd:      cfg.PaperMaxSecondsToEnd * 3,
		TakerFeeRate:         cfg.PaperTakerFeeRate,
		LatencyBuffer:        cfg.PaperLatencyBuffer,
		MaxEffectiveEntry:    cfg.PaperMaxEffectiveEntry,
		MinEconomicEdge:      cfg.PaperMinEconomicEdge,
		HedgeEnabled:         cfg.PaperHedgeEnabled && !isMockMode,
		HedgeWindow:          cfg.PaperHedgeWindow,
		HedgeMinVotes:        cfg.PaperHedgeMinVotes,
		HedgeMinConsecutive:  cfg.PaperHedgeMinConsecutive,
		HedgeScoreThreshold:  cfg.PaperHedgeScoreThreshold,
		HedgeMinProbability:  cfg.PaperHedgeMinProbability,
		HedgeMinEdge:         cfg.PaperHedgeMinEdge,
		HedgeMinAbsPTBZ:      cfg.PaperHedgeMinAbsPTBZ,
		HedgeMinSecondsToEnd: cfg.PaperHedgeMinSecondsToEnd * 3,
		HedgeMaxSecondsToEnd: cfg.PaperHedgeMaxSecondsToEnd * 3,
	})
	quoteBudget := func(tokenID string, budget float64) (polymarket.BuyQuote, error) {
		return pmClient.FetchBuyQuoteForBudget(tokenID, budget, cfg.PaperTakerFeeRate, cfg.PaperLatencyBuffer)
	}
	quoteShares := func(tokenID string, shares float64) (polymarket.BuyQuote, error) {
		return pmClient.FetchBuyQuoteForShares(tokenID, shares, cfg.PaperTakerFeeRate, cfg.PaperLatencyBuffer)
	}
	state := &marketState{}
	evaluator := engine.NewEvaluator(microClient)

	refreshMarket := func(now time.Time) {
		if isMockMode {
			start := polymarket.BTC15mWindowStart(now)
			ptb := bClient.GetPrice()
			if ptb <= 0 {
				return
			}
			state.Set(&polymarket.Market{ID: "mock-15m", Question: "MOCK BTC Up or Down - 15 Minutes", Slug: "mock-15m", EventSlug: polymarket.BTC15mEventSlug(start), Active: true, PriceToBeat: ptb, PriceToBeatSource: "MOCK", StartTime: start, EndTime: start.Add(15 * time.Minute), Outcomes: []string{"Up", "Down"}, Tokens: []polymarket.Token{{Outcome: "Up", Price: 0.5}, {Outcome: "Down", Price: 0.5}}})
			return
		}
		m, err := pmClient.FetchActiveBTC15mMarket()
		if err != nil {
			util.Logger.Warn("Polymarket active BTC 15m lookup failed; emitting NO_SIGNAL", zap.Error(err))
			state.Set(nil)
			return
		}
		snap := clClient.Snapshot(m.StartTime, now)
		if snap.Ready && snap.PriceToBeat > 0 {
			m.PriceToBeat = snap.PriceToBeat
			m.PriceToBeatSource = "CHAINLINK_RTDS_BOUNDARY"
		} else if ptb, fetchErr := pmClient.FetchPriceToBeatForTimeframe(m, "15m"); fetchErr == nil && ptb > 0 {
			m.PriceToBeat = ptb
			m.PriceToBeatSource = "POLYMARKET_REFERENCE_API"
		} else {
			m.PriceToBeat = 0
			m.PriceToBeatSource = "UNAVAILABLE"
			util.Logger.Warn("BTC 15m price-to-beat unavailable; signal disabled", zap.String("eventSlug", m.EventSlug))
		}
		state.Set(m)
	}

	refreshMarket(time.Now().UTC())
	go func() {
		poll := time.Duration(cfg.PolymarketPollSec) * time.Second
		if poll < time.Second {
			poll = time.Second
		}
		ticker := time.NewTicker(poll)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case now := <-ticker.C:
				refreshMarket(now.UTC())
			}
		}
	}()

	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case now := <-ticker.C:
				now = now.UTC()
				if !isMockMode && paperEngine.Enabled() {
					settled, err := paperEngine.SettleReady(now, clClient.BoundaryPrice)
					if err != nil {
						util.Logger.Error("BTC 15m paper settlement failed", zap.Error(err))
					} else if settled > 0 {
						util.Logger.Info("BTC 15m paper positions settled", zap.Int("count", settled))
					}
				}

				m := state.Get()
				if m == nil {
					server.UpdateStateFor("15m", nil, nil)
					server.UpdateGatesFor("15m", paperEngine.EntryGateSnapshot(nil, nil, now, quoteBudget), paperEngine.HedgeGateSnapshot(nil, nil, now, quoteShares))
					continue
				}
				var referencePrice float64
				var referenceFresh bool
				if isMockMode {
					referencePrice = bClient.GetPrice()
					referenceFresh = bClient.IsPriceFresh(3 * time.Second)
				} else {
					snap := clClient.Snapshot(m.StartTime, now)
					referencePrice, referenceFresh = snap.CurrentPrice, snap.Fresh
					if m.PriceToBeat <= 0 && snap.Ready && snap.PriceToBeat > 0 {
						m.PriceToBeat = snap.PriceToBeat
						m.PriceToBeatSource = "CHAINLINK_RTDS_BOUNDARY"
						state.Set(m)
					}
				}
				res := evaluator.Evaluate(bClient, m, referencePrice, referenceFresh, now.Format(time.RFC3339Nano))
				if res == nil {
					server.UpdateStateFor("15m", nil, m)
					server.UpdateGatesFor("15m", paperEngine.EntryGateSnapshot(nil, m, now, quoteBudget), paperEngine.HedgeGateSnapshot(nil, m, now, quoteShares))
					continue
				}
				server.UpdateStateFor("15m", res, m)
				if isMockMode || strings.Contains(res.DataSource, "MOCK") {
					server.UpdateGatesFor("15m", paperEngine.EntryGateSnapshot(res, m, now, quoteBudget), paperEngine.HedgeGateSnapshot(res, m, now, quoteShares))
					continue
				}
				if err := db.InsertSignalWithMicro(res); err != nil {
					util.Logger.Error("Failed to store BTC 15m signal", zap.Error(err))
					continue
				}
				if trade, opened, err := paperEngine.MaybeOpenWithQuote(res, m, now, quoteBudget); err != nil {
					util.Logger.Warn("BTC 15m paper entry skipped", zap.Error(err))
				} else if opened {
					util.Logger.Info("BTC 15m PAPER POSITION OPENED", zap.String("market", trade.MarketSlug), zap.String("side", trade.Side), zap.Float64("entryPrice", trade.EntryPrice), zap.Float64("totalCost", trade.Stake), zap.Float64("shares", trade.Shares), zap.Float64("confidence", trade.EntryConfidence))
				}
				if h, hedged, err := paperEngine.MaybeHedge(res, m, now, quoteShares); err != nil {
					util.Logger.Warn("BTC 15m paper hedge skipped", zap.Error(err))
				} else if hedged {
					util.Logger.Info("BTC 15m PAPER SHADOW HEDGE OPENED", zap.String("market", h.MarketSlug), zap.String("originalSide", h.OriginalSide), zap.String("hedgeSide", h.Side), zap.Float64("edge", h.Edge), zap.Float64("lockedPnL", h.LockedPnL))
				}
				server.UpdateGatesFor("15m", paperEngine.EntryGateSnapshot(res, m, now, quoteBudget), paperEngine.HedgeGateSnapshot(res, m, now, quoteShares))
				util.Logger.Info("Evaluated BTC 15m directional bias score", zap.String("eventSlug", m.EventSlug), zap.Float64("remaining_sec", res.SecondsRemaining), zap.Float64("pUp", res.PUp), zap.Float64("ptbZ", res.PTBZ), zap.Float64("finalScore", res.FinalScore), zap.String("decision", res.Decision), zap.Float64("confidence", res.Confidence), zap.String("shadowDecision", res.ShadowDecision), zap.Float64("shadowScore", res.ShadowModelBScore), zap.Float64("microScore", res.MicrostructureScore))
			}
		}
	}()
}
