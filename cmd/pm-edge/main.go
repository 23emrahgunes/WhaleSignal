package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
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

type marketState struct {
	mu     sync.RWMutex
	market *polymarket.Market
}

func (s *marketState) Set(m *polymarket.Market) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if m == nil {
		s.market = nil
		return
	}
	cp := *m
	cp.Tokens = append([]polymarket.Token(nil), m.Tokens...)
	cp.Outcomes = append([]string(nil), m.Outcomes...)
	s.market = &cp
}

func (s *marketState) Get() *polymarket.Market {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.market == nil {
		return nil
	}
	cp := *s.market
	cp.Tokens = append([]polymarket.Token(nil), s.market.Tokens...)
	cp.Outcomes = append([]string(nil), s.market.Outcomes...)
	return &cp
}

func main() {
	args := os.Args
	if len(args) < 2 || args[1] != "tv-direction" {
		fmt.Println("Usage: pm-edge tv-direction [--mock]")
		os.Exit(1)
	}
	isMockMode := false
	for _, arg := range args {
		if arg == "--mock" {
			isMockMode = true
		}
	}

	cfg, err := config.LoadConfig()
	if err != nil {
		fmt.Printf("Failed to load config: %v\n", err)
		os.Exit(1)
	}
	util.InitLogger(cfg.LogLevel)
	defer func() { _ = util.Logger.Sync() }()
	util.Logger.Info("Initializing PM-Edge TV-Direction Real-Time Research Engine",
		zap.Bool("mockMode", isMockMode), zap.Bool("paperEnabled", cfg.PaperEnabled),
		zap.Bool("paperHedgeEnabled", cfg.PaperHedgeEnabled), zap.Float64("paperStake", cfg.PaperStake),
		zap.Float64("paperMinConfidence", cfg.PaperMinConfidence))

	db, err := storage.NewDatabase(cfg.DBPath)
	if err != nil {
		util.Logger.Fatal("Database setup failed", zap.Error(err))
	}
	defer db.Close()
	if err := db.EnsurePaperHedgeSchema(); err != nil {
		util.Logger.Fatal("Paper hedge schema setup failed", zap.Error(err))
	}
	if err := db.EnsureMicrostructureSchema(); err != nil {
		util.Logger.Fatal("Deep microstructure schema setup failed", zap.Error(err))
	}

	server := api.NewServer(db, cfg.PaperInitialBalance)
	pmClient := polymarket.NewClient()
	bClient := binance.NewClient()
	clClient := chainlink.NewClient()
	microClient := binance.NewMicrostructureClient()
	paperEngine := paper.NewEngine(db, paper.Config{
		Timeframe:            "5m",
		Enabled:              cfg.PaperEnabled && !isMockMode,
		InitialBalance:       cfg.PaperInitialBalance,
		Stake:                cfg.PaperStake,
		MinConfidence:        cfg.PaperMinConfidence,
		MinSecondsToEnd:      cfg.PaperMinSecondsToEnd,
		MaxSecondsToEnd:      cfg.PaperMaxSecondsToEnd,
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
		HedgeMinSecondsToEnd: cfg.PaperHedgeMinSecondsToEnd,
		HedgeMaxSecondsToEnd: cfg.PaperHedgeMaxSecondsToEnd,
	})

	quoteBudget := func(tokenID string, budget float64) (polymarket.BuyQuote, error) {
		return pmClient.FetchBuyQuoteForBudget(tokenID, budget, cfg.PaperTakerFeeRate, cfg.PaperLatencyBuffer)
	}
	quoteShares := func(tokenID string, shares float64) (polymarket.BuyQuote, error) {
		return pmClient.FetchBuyQuoteForShares(tokenID, shares, cfg.PaperTakerFeeRate, cfg.PaperLatencyBuffer)
	}

	util.Logger.Info("Warming up Binance candlestick caches...")
	if err := bClient.WarmupCandles(); err != nil {
		util.Logger.Warn("Binance warmup failed", zap.Error(err))
	}

	wsManager := binance.NewWSManager(bClient, isMockMode)
	wsManager.Start()
	wsManager.StartFallbackRESTPoller()
	if isMockMode {
		util.Logger.Info("Mock mode enabled; synthetic Binance feed will not be persisted or paper-traded")
		wsManager.StartMockDataInjector()
	} else {
		clClient.Start()
		microClient.Start()
	}
	defer wsManager.Stop()
	if !isMockMode {
		defer clClient.Stop()
		defer microClient.Stop()
	}

	go func() {
		util.Logger.Info("Starting REST server", zap.String("port", cfg.Port))
		if err := server.Start(cfg.Port); err != nil {
			util.Logger.Fatal("REST server failed", zap.Error(err))
		}
	}()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	evaluator := engine.NewEvaluator(microClient)
	state := &marketState{}
	startBTC15mRuntime(ctx, isMockMode, cfg, db, server, pmClient, bClient, clClient, microClient)

	refreshMarket := func(now time.Time) {
		if isMockMode {
			start := polymarket.BTC5mWindowStart(now)
			ptb := bClient.GetPrice()
			if ptb <= 0 {
				return
			}
			state.Set(&polymarket.Market{ID: "mock", Question: "MOCK BTC Up or Down - 5 Minutes", Slug: "mock", EventSlug: polymarket.BTC5mEventSlug(start), Active: true, PriceToBeat: ptb, PriceToBeatSource: "MOCK", StartTime: start, EndTime: start.Add(5 * time.Minute), Outcomes: []string{"Up", "Down"}, Tokens: []polymarket.Token{{Outcome: "Up", Price: 0.5}, {Outcome: "Down", Price: 0.5}}})
			return
		}

		m, err := pmClient.FetchActiveBTC5mMarket()
		if err != nil {
			util.Logger.Warn("Polymarket active BTC 5m lookup failed; emitting NO_SIGNAL", zap.Error(err))
			state.Set(nil)
			return
		}
		snap := clClient.Snapshot(m.StartTime, now)
		if snap.Ready && snap.PriceToBeat > 0 {
			m.PriceToBeat = snap.PriceToBeat
			m.PriceToBeatSource = "CHAINLINK_RTDS_BOUNDARY"
		} else if ptb, fetchErr := pmClient.FetchPriceToBeat(m); fetchErr == nil && ptb > 0 {
			m.PriceToBeat = ptb
			m.PriceToBeatSource = "POLYMARKET_REFERENCE_API"
		} else {
			m.PriceToBeat = 0
			m.PriceToBeatSource = "UNAVAILABLE"
			util.Logger.Warn("Price-to-beat unavailable; market kept for metadata but signal disabled", zap.String("eventSlug", m.EventSlug))
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
					settled, settleErr := paperEngine.SettleReady(now, clClient.BoundaryPrice)
					if settleErr != nil {
						util.Logger.Error("Paper settlement failed", zap.Error(settleErr))
					} else if settled > 0 {
						util.Logger.Info("Paper positions settled", zap.Int("count", settled))
					}
				}

				m := state.Get()
				if m == nil {
					server.UpdateState(nil, nil)
					server.UpdateGatesFor("5m", paperEngine.EntryGateSnapshot(nil, nil, now, quoteBudget), paperEngine.HedgeGateSnapshot(nil, nil, now, quoteShares))
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
					server.UpdateState(nil, m)
					server.UpdateGatesFor("5m", paperEngine.EntryGateSnapshot(nil, m, now, quoteBudget), paperEngine.HedgeGateSnapshot(nil, m, now, quoteShares))
					continue
				}
				server.UpdateState(res, m)
				if isMockMode || strings.Contains(res.DataSource, "MOCK") {
					server.UpdateGatesFor("5m", paperEngine.EntryGateSnapshot(res, m, now, quoteBudget), paperEngine.HedgeGateSnapshot(res, m, now, quoteShares))
					continue
				}
				if err := db.InsertSignalWithMicro(res); err != nil {
					util.Logger.Error("Failed to store signal in SQLite", zap.Error(err))
					continue
				}
				if trade, opened, paperErr := paperEngine.MaybeOpenWithQuote(res, m, now, quoteBudget); paperErr != nil {
					util.Logger.Warn("Paper entry quote/evaluation skipped", zap.Error(paperErr))
				} else if opened {
					util.Logger.Info("PAPER POSITION OPENED", zap.String("market", trade.MarketSlug), zap.String("side", trade.Side), zap.Float64("entryPrice", trade.EntryPrice), zap.Float64("totalCost", trade.Stake), zap.Float64("shares", trade.Shares), zap.Float64("confidence", trade.EntryConfidence))
				}
				if h, hedged, hedgeErr := paperEngine.MaybeHedge(res, m, now, quoteShares); hedgeErr != nil {
					util.Logger.Warn("Paper hedge quote/evaluation skipped", zap.Error(hedgeErr))
				} else if hedged {
					util.Logger.Info("PAPER SHADOW HEDGE OPENED", zap.String("market", h.MarketSlug), zap.String("originalSide", h.OriginalSide), zap.String("hedgeSide", h.Side), zap.Float64("hedgePrice", h.EntryPrice), zap.Float64("shares", h.Shares), zap.Float64("edge", h.Edge), zap.Float64("persistence", h.Persistence), zap.Float64("lockedPnL", h.LockedPnL))
				}
				server.UpdateGatesFor("5m", paperEngine.EntryGateSnapshot(res, m, now, quoteBudget), paperEngine.HedgeGateSnapshot(res, m, now, quoteShares))
				util.Logger.Info("Evaluated directional bias score",
					zap.String("eventSlug", m.EventSlug), zap.String("ptbSource", m.PriceToBeatSource),
					zap.Float64("priceToBeat", res.PriceToBeat), zap.Float64("currentPrice", res.CurrentPrice),
					zap.Float64("binancePrice", res.BinancePrice), zap.Float64("basisBps", res.ChainlinkBinanceBasisBps),
					zap.Float64("remaining_sec", res.SecondsRemaining), zap.Float64("pUp", res.PUp),
					zap.Float64("ptbZ", res.PTBZ), zap.Float64("sigmaExpiryBps", res.ForecastSigmaExpiryBps),
					zap.Float64("finalScore", res.FinalScore), zap.String("decision", res.Decision),
					zap.Float64("confidence", res.Confidence), zap.String("shadowDecision", res.ShadowDecision),
					zap.Float64("shadowScore", res.ShadowModelBScore), zap.Float64("microScore", res.MicrostructureScore),
					zap.String("source", res.DataSource), zap.String("depthSource", res.DepthSource), zap.Int64("depthAgeMs", res.DepthAgeMs))
			}
		}
	}()

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	<-sigChan
	cancel()
	util.Logger.Info("Shutting down gracefully...")
}
