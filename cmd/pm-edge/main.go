package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"go.uber.org/zap"
	"pm-edge/internal/api"
	"pm-edge/internal/binance"
	"pm-edge/internal/config"
	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
	"pm-edge/internal/util"
)

func main() {
	// Parse CLI commands
	args := os.Args
	if len(args) < 2 || args[1] != "tv-direction" {
		fmt.Println("Usage: go run ./cmd/pm-edge tv-direction")
		os.Exit(1)
	}

	// 1. Load configuration
	cfg, err := config.LoadConfig()
	if err != nil {
		fmt.Printf("Failed to load config: %v\n", err)
		os.Exit(1)
	}

	// 2. Initialize global logger
	util.InitLogger(cfg.LogLevel)
	defer func() { _ = util.Logger.Sync() }()

	util.Logger.Info("Initializing PM-Edge TV-Direction Real-Time Research Engine...")

	// 3. Initialize SQLite DB
	db, err := storage.NewDatabase(cfg.DBPath)
	if err != nil {
		util.Logger.Fatal("Database setup failed", zap.Error(err))
	}
	defer db.Close()

	// 4. Initialize API and Web Server
	server := api.NewServer(db)

	// 5. Initialize Polymarket REST Client
	pmClient := polymarket.NewClient()

	// 6. Initialize Binance WebSocket and In-Memory Clients
	bClient := binance.NewClient()

	// Run warm-up
	util.Logger.Info("Warming up Binance candlestick caches...")
	if err := bClient.WarmupCandles(); err != nil {
		util.Logger.Warn("Binance warmup failed (using dynamic memory generation fallback)", zap.Error(err))
	}

	wsManager := binance.NewWSManager(bClient)
	wsManager.Start()
	wsManager.StartFallbackRESTPoller()
	wsManager.StartMockDataInjector() // guarantees ticks for headless test modes
	defer wsManager.Stop()

	// 7. Start REST HTTP Server
	go func() {
		util.Logger.Info("Starting REST Server...", zap.String("port", cfg.Port))
		if err := server.Start(cfg.Port); err != nil {
			util.Logger.Fatal("REST server failed", zap.Error(err))
		}
	}()

	// 8. Background Loops
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	evaluator := engine.NewEvaluator()

	var activeMarket *polymarket.Market
	var marketMutex time.Ticker // dummy poll

	// Polymarket active market polling routine (Every 15s)
	go func() {
		ticker := time.NewTicker(time.Duration(cfg.PolymarketPollSec) * time.Second)
		defer ticker.Stop()

		// Initial lookup
		market, err := pmClient.FetchActiveBTC5mMarket()
		if err == nil {
			activeMarket = market
		} else {
			util.Logger.Warn("Initial Polymarket active market query failed", zap.Error(err))
		}

		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				market, err := pmClient.FetchActiveBTC5mMarket()
				if err == nil {
					activeMarket = market
				} else {
					util.Logger.Warn("Failed to poll Polymarket, falling back to cached value", zap.Error(err))
					if activeMarket != nil {
						activeMarket.MarketStale = true
					}
				}
			}
		}
	}()

	_ = marketMutex

	// Olasılık & Değerlendirme Loop (Every 1 second)
	go func() {
		ticker := time.NewTicker(1 * time.Second)
		defer ticker.Stop()

		var elapsed float64

		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				elapsed++
				nowStr := time.Now().UTC().Format(time.RFC3339)
				res := evaluator.Evaluate(bClient, activeMarket, nowStr, elapsed)
				if res != nil {
					// State update to server for GET /api/live
					server.UpdateState(res, activeMarket)

					// Persistent SQL storage
					if err := db.InsertSignal(res); err != nil {
						util.Logger.Error("Failed to store signal in SQLite", zap.Error(err))
					}

					// Real-time stdout logging
					util.Logger.Info("Evaluated directional bias score",
						zap.Float64("priceToBeat", res.PriceToBeat),
						zap.Float64("currentPrice", res.CurrentPrice),
						zap.Float64("remaining_sec", res.SecondsRemaining),
						zap.Float64("pUp", res.PUp),
						zap.Float64("finalScore", res.FinalScore),
						zap.String("decision", res.Decision),
						zap.Float64("confidence", res.Confidence),
					)
				}
			}
		}
	}()

	// Graceful Shutdown listeners
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	<-sigChan
	util.Logger.Info("Shutting down gracefully...")
}
