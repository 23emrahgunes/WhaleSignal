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
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
	"pm-edge/internal/util"
)

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

	util.Logger.Info("Initializing PM-Edge TV-Direction Research Engine",
		zap.Bool("mockMode", isMockMode),
		zap.String("referenceSource", "POLYMARKET_CHAINLINK_RTDS"),
	)

	db, err := storage.NewDatabase(cfg.DBPath)
	if err != nil {
		util.Logger.Fatal("Database setup failed", zap.Error(err))
	}
	defer db.Close()

	server := api.NewServer(db)
	pmClient := polymarket.NewClient()
	bClient := binance.NewClient()
	chainlinkClient := chainlink.NewClient()

	util.Logger.Info("Warming up Binance candlestick caches...")
	if err := bClient.WarmupCandles(); err != nil {
		util.Logger.Warn("Binance warmup failed; engine will wait for fresh feed data", zap.Error(err))
	}

	wsManager := binance.NewWSManager(bClient, isMockMode)
	wsManager.Start()
	wsManager.StartFallbackRESTPoller()
	if isMockMode {
		wsManager.StartMockDataInjector()
	}
	defer wsManager.Stop()

	chainlinkClient.Start()
	defer chainlinkClient.Stop()

	go func() {
		util.Logger.Info("Starting REST server", zap.String("port", cfg.Port))
		if err := server.Start(cfg.Port); err != nil {
			util.Logger.Fatal("REST server failed", zap.Error(err))
		}
	}()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	evaluator := engine.NewEvaluator()
	var marketMu sync.RWMutex
	var activeMarket *polymarket.Market

	setMarket := func(m *polymarket.Market) {
		marketMu.Lock()
		defer marketMu.Unlock()
		activeMarket = m
	}

	getMarketCopy := func() *polymarket.Market {
		marketMu.RLock()
		defer marketMu.RUnlock()
		if activeMarket == nil {
			return nil
		}
		copyMarket := *activeMarket
		copyMarket.Tokens = append([]polymarket.Token(nil), activeMarket.Tokens...)
		copyMarket.Outcomes = append([]string(nil), activeMarket.Outcomes...)
		return &copyMarket
	}

	pollMarket := func() {
		now := time.Now().UTC()
		wantedStart := polymarket.BTC5mWindowStart(now)
		market, err := pmClient.FetchActiveBTC5mMarket(now)
		if err != nil {
			cached := getMarketCopy()
			if cached != nil && cached.StartTime.Equal(wantedStart) && now.Before(cached.EndTime) {
				cached.MarketStale = true
				setMarket(cached)
				util.Logger.Warn("Polymarket market refresh failed; retaining same-window cached metadata",
					zap.String("eventSlug", cached.EventSlug), zap.Error(err))
				return
			}
			setMarket(nil)
			server.UpdateState(nil, nil)
			util.Logger.Warn("No verified active Polymarket BTC 5m market; NO_SIGNAL", zap.Error(err))
			return
		}
		market.MarketStale = false
		setMarket(market)
		util.Logger.Info("Verified active Polymarket BTC 5m market",
			zap.String("eventSlug", market.EventSlug),
			zap.Time("start", market.StartTime),
			zap.Time("end", market.EndTime),
		)
	}

	go func() {
		pollMarket()
		ticker := time.NewTicker(time.Duration(cfg.PolymarketPollSec) * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				pollMarket()
			}
		}
	}()

	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		var lastWaitingReason string
		var lastWaitingLog time.Time

		logWaiting := func(reason string) {
			now := time.Now().UTC()
			if reason != lastWaitingReason || now.Sub(lastWaitingLog) >= 30*time.Second {
				util.Logger.Info("Decision gate waiting", zap.String("reason", reason))
				lastWaitingReason = reason
				lastWaitingLog = now
			}
		}

		for {
			select {
			case <-ctx.Done():
				return
			case now := <-ticker.C:
				now = now.UTC()
				market := getMarketCopy()
				if market == nil {
					server.UpdateState(nil, nil)
					logWaiting("no_verified_polymarket_market")
					continue
				}
				if !now.Before(market.EndTime) {
					server.UpdateState(nil, nil)
					logWaiting("market_window_ended")
					continue
				}

				ref := chainlinkClient.Snapshot(market.StartTime, now)
				if !ref.Ready {
					server.UpdateState(nil, market)
					logWaiting("chainlink_opening_reference_not_captured_wait_next_5m_boundary")
					continue
				}
				if !ref.Fresh {
					server.UpdateState(nil, market)
					logWaiting("chainlink_feed_stale")
					continue
				}

				market.PriceToBeat = ref.PriceToBeat
				nowStr := now.Format(time.RFC3339)
				res := evaluator.Evaluate(bClient, market, ref.CurrentPrice, ref.Fresh, nowStr)
				if res == nil {
					server.UpdateState(nil, market)
					logWaiting("decision_inputs_not_fresh_or_complete")
					continue
				}
				if !isMockMode && strings.Contains(res.DataSource, "MOCK") {
					server.UpdateState(nil, market)
					logWaiting("mock_data_rejected")
					continue
				}

				server.UpdateState(res, market)
				if err := db.InsertSignal(res); err != nil {
					util.Logger.Error("Failed to store verified signal in SQLite", zap.Error(err))
					continue
				}

				lastWaitingReason = ""
				util.Logger.Info("Evaluated verified BTC 5m directional bias",
					zap.String("eventSlug", res.Slug),
					zap.Float64("priceToBeat", res.PriceToBeat),
					zap.Float64("chainlinkPrice", res.CurrentPrice),
					zap.Float64("remaining_sec", res.SecondsRemaining),
					zap.Float64("pUp", res.PUp),
					zap.Float64("finalScore", res.FinalScore),
					zap.String("decision", res.Decision),
					zap.Float64("confidence", res.Confidence),
					zap.String("source", res.DataSource),
				)
			}
		}
	}()

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	<-sigChan
	cancel()
	util.Logger.Info("Shutting down gracefully...")
}
