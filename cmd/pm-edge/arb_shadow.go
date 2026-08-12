package main

import (
	"context"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.uber.org/zap"
	"pm-edge/internal/arb"
	"pm-edge/internal/binance"
	"pm-edge/internal/config"
	"pm-edge/internal/dual40"
	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
	"pm-edge/internal/util"
)

type arbShadowRuntime struct {
	engine              *arb.Engine
	db                  *storage.Database
	pmClient            *polymarket.Client
	tradeStream         *polymarket.MarketTradeStream
	busy                atomic.Bool
	paperEnabled        bool
	paperCfg            arb.PaperConfig
	paperInitialBalance float64
	maxBookFetchMs      int64
	tradeStreamMaxAge   time.Duration
	active              *arb.PaperCycle
	completionPolicy    arb.CompletionPolicy
	dual40              *dual40Runtime
}

func newArbShadowRuntime(tf string, cfg *config.Config, db *storage.Database, pmClient *polymarket.Client, enabled bool) *arbShadowRuntime {
	stream := polymarket.NewMarketTradeStream()
	r := &arbShadowRuntime{engine: arb.NewEngine(arb.Config{
		Timeframe: tf, Enabled: enabled && cfg.ArbShadowEnabled,
		TargetEdge: cfg.ArbTargetEdge, PaperMinEdge: cfg.ArbPaperMinEdge,
		OperationalBuffer:  cfg.ArbOperationalBuffer,
		UncertaintyPenalty: cfg.ArbUncertaintyPenalty, MaxStrandedUnits: cfg.ArbMaxStrandedUnits,
	}), db: db, pmClient: pmClient, tradeStream: stream, paperEnabled: enabled && cfg.ArbPaperEnabled,
		paperCfg:            arb.PaperConfig{Enabled: enabled && cfg.ArbPaperEnabled, OrderTTL: time.Duration(cfg.ArbPaperOrderTTLSec) * time.Second, SoftCompletion: time.Duration(cfg.ArbPaperSoftCompletionSec) * time.Second, MaxStranded: time.Duration(cfg.ArbPaperMaxStrandedSec) * time.Second, StopBeforeEnd: time.Duration(cfg.ArbPaperStopBeforeEndSec) * time.Second},
		paperInitialBalance: cfg.PaperInitialBalance, maxBookFetchMs: int64(cfg.ArbMaxBookFetchMs), tradeStreamMaxAge: time.Duration(cfg.ArbTradeStreamMaxAgeSec) * time.Second,
		completionPolicy: arb.CompletionPolicy{Lookback: cfg.ArbCompletionLookback, MinSamples: cfg.ArbCompletionMinSamples, MinStrandedSamples: cfg.ArbCompletionMinStrandedSamples, MinPComplete5sLower95: cfg.ArbCompletionMinP5Lower, MinCycleEV: cfg.ArbCompletionMinCycleEV, MaxStrandedLossMultiple: cfg.ArbCompletionMaxLossMultiple}}
	if r.maxBookFetchMs <= 0 {
		r.maxBookFetchMs = 1000
	}
	if r.tradeStreamMaxAge <= 0 {
		r.tradeStreamMaxAge = 20 * time.Second
	}
	// Dual40 is a BTC 5m-only experiment. The old constructor attached it to
	// both 5m and 15m arb runtimes, contaminating the 5m table with 15m markets.
	if strings.EqualFold(tf, "5m") {
		dualCfg, dualEnabled := dual40.LoadConfigFromEnv()
		r.dual40 = newDual40Runtime(tf, dualCfg, db, pmClient, enabled && dualEnabled, cfg.PaperTakerFeeRate, cfg.PaperLatencyBuffer, r.tradeStreamMaxAge)
	}
	if enabled && cfg.ArbShadowEnabled {
		stream.Start()
	}
	if r.paperEnabled {
		if open, err := db.GetOpenArbPaperCycle(tf); err != nil {
			util.Logger.Warn("Maker arb paper open-cycle restore failed", zap.String("tf", tf), zap.Error(err))
		} else if open != nil {
			r.active = open
			util.Logger.Info("MAKER ARB PAPER CYCLE RESTORED", zap.String("tf", tf), zap.Int64("id", open.ID), zap.String("market", open.MarketSlug), zap.String("status", open.Status))
		}
	}
	return r
}

func (r *arbShadowRuntime) StartDual40Observer(ctx context.Context, bClient *binance.Client, microClient *binance.MicrostructureClient) {
	if r == nil || r.dual40 == nil {
		return
	}
	// FAZ 0 SINIRI: dual40 SHADOW (paper) motorudur — gercek emir YURUTMEZ.
	// DUAL40_LIVE=true istense bile canli yurutme Faz 3'tur (yalniz istatistiksel
	// kanit sonrasi). Buraya (StartObserver) canli execution seam'i baglanacak.
	if strings.EqualFold(strings.TrimSpace(os.Getenv("DUAL40_LIVE")), "true") {
		util.Logger.Warn("DUAL40_LIVE istendi ANCAK Faz 0 SHADOW-only; canli yurutme Faz 3 (kanit sonrasi). SHADOW olarak devam.")
	}
	r.dual40.StartObserver(ctx, bClient, microClient)
}

func (r *arbShadowRuntime) Submit(res *engine.EvaluationResult, market *polymarket.Market) {
	if r == nil || res == nil || market == nil {
		return
	}
	if r.engine == nil || !r.engine.Enabled() {
		return
	}
	if !r.busy.CompareAndSwap(false, true) {
		return
	}
	rc := *res
	mc := *market
	mc.Tokens = append([]polymarket.Token(nil), market.Tokens...)
	go func() {
		defer r.busy.Store(false)
		upID, ok := polymarket.TokenIDForOutcome(&mc, "UP")
		if !ok {
			util.Logger.Warn("Maker arb missing UP token", zap.String("market", mc.Slug))
			return
		}
		downID, ok := polymarket.TokenIDForOutcome(&mc, "DOWN")
		if !ok {
			util.Logger.Warn("Maker arb missing DOWN token", zap.String("market", mc.Slug))
			return
		}
		r.tradeStream.SetAssets([]string{upID, downID})

		started := time.Now()
		upBook, downBook, err := r.fetchPairBooks(upID, downID)
		fetchMs := time.Since(started).Milliseconds()
		if err != nil {
			util.Logger.Warn("Maker arb pair book unavailable", zap.Error(err), zap.String("market", mc.Slug))
			return
		}
		snap := r.engine.Evaluate(&rc, &mc, upBook, downBook)
		if snap == nil {
			return
		}
		snap.BookFetchMs = fetchMs
		training, trainErr := r.db.GetArbPaperCyclesByTimeframe(r.completionPolicy.Lookback, snap.Timeframe)
		if trainErr != nil {
			util.Logger.Warn("Maker arb completion training read failed", zap.String("tf", snap.Timeframe), zap.Error(trainErr))
		}
		arb.ApplyCompletionModel(snap, training, r.completionPolicy)
		if snap.Status != arb.StatusBlocked && fetchMs > r.maxBookFetchMs {
			snap.Status = arb.StatusBlocked
			snap.Reason = "BOOK_FETCH_TOO_SLOW"
		}
		if err := r.db.InsertArbSnapshot(snap); err != nil {
			util.Logger.Error("Maker arb snapshot store failed", zap.Error(err))
			return
		}

		now := time.Now().UTC()
		r.processPaper(snap, upBook, downBook, &mc, now)
		if snap.Status == arb.StatusCandidate || snap.Status == arb.StatusPaperCandidate {
			util.Logger.Info("MAKER ARB SAFE-FIRST SHADOW", zap.String("market", snap.MarketSlug), zap.String("status", snap.Status), zap.String("firstLeg", snap.FirstLeg), zap.Float64("firstQueueAhead", snap.FirstLegQueueAhead), zap.Float64("up", snap.UpMakerPrice), zap.Float64("down", snap.DownMakerPrice), zap.Float64("netEdge", snap.NetEdge), zap.Float64("paperMinEdge", snap.PaperMinEdge), zap.Float64("liveTargetEdge", snap.TargetEdge), zap.Bool("completionReady", snap.CompletionModelReady), zap.Float64("pComplete5sLower95", snap.PComplete5sLower95), zap.Float64("cycleEV", snap.ConservativeCycleEV), zap.Float64("opportunityEV", snap.OpportunityEV), zap.Int64("bookFetchMs", snap.BookFetchMs))
		}
	}()
}

func (r *arbShadowRuntime) fetchPairBooks(upID, downID string) (polymarket.BookSnapshot, polymarket.BookSnapshot, error) {
	type result struct {
		book polymarket.BookSnapshot
		err  error
	}
	var wg sync.WaitGroup
	wg.Add(2)
	upCh, downCh := make(chan result, 1), make(chan result, 1)
	go func() { defer wg.Done(); b, e := r.pmClient.FetchBookSnapshot(upID); upCh <- result{b, e} }()
	go func() { defer wg.Done(); b, e := r.pmClient.FetchBookSnapshot(downID); downCh <- result{b, e} }()
	wg.Wait()
	close(upCh)
	close(downCh)
	u, d := <-upCh, <-downCh
	if u.err != nil {
		return polymarket.BookSnapshot{}, polymarket.BookSnapshot{}, u.err
	}
	if d.err != nil {
		return polymarket.BookSnapshot{}, polymarket.BookSnapshot{}, d.err
	}
	return u.book, d.book, nil
}

func (r *arbShadowRuntime) processPaper(snap *arb.Snapshot, upBook, downBook polymarket.BookSnapshot, market *polymarket.Market, now time.Time) {
	if !r.paperEnabled || snap == nil || market == nil {
		return
	}
	if r.active != nil && r.active.MarketSlug != market.Slug {
		if arb.ClosePaperCycleForMarketChange(r.active, now) {
			if err := r.db.UpdateArbPaperCycle(r.active); err != nil {
				util.Logger.Error("Maker arb paper market-change close failed", zap.Error(err))
				return
			}
		}
		r.logPaperTerminal(r.active)
		r.active = nil
	}

	if r.active != nil {
		if r.tradeStream.GapCount() != r.active.StreamGapCount {
			if arb.InvalidatePaperCycleDataGap(r.active, now) {
				if err := r.db.UpdateArbPaperCycle(r.active); err != nil {
					util.Logger.Error("Maker arb data-gap invalidation failed", zap.Error(err))
					return
				}
			}
		} else if r.tradeStream.Healthy(r.tradeStreamMaxAge) {
			trades, latest := r.tradeStream.TradesAfter(r.active.LastTradeSeq)
			if arb.AdvancePaperCycle(r.active, upBook, downBook, trades, latest, now, market.EndTime, r.paperCfg) {
				if err := r.db.UpdateArbPaperCycle(r.active); err != nil {
					util.Logger.Error("Maker arb paper update failed", zap.Error(err))
					return
				}
			}
		}
		if r.active.IsTerminal() {
			r.logPaperTerminal(r.active)
			r.active = nil
		}
	}
	if r.active != nil || snap.Status == arb.StatusBlocked || !snap.PaperEdgePass || !snap.PTBReady {
		return
	}
	if !r.tradeStream.Healthy(r.tradeStreamMaxAge) {
		return
	}

	stats, err := r.db.GetArbPaperStatsByTimeframe(r.paperInitialBalance, snap.Timeframe)
	if err != nil {
		util.Logger.Warn("Maker arb paper balance unavailable", zap.Error(err))
		return
	}
	required := snap.OrderSize * (snap.UpMakerPrice + snap.DownMakerPrice)
	if stats.CashBalance+1e-9 < required {
		util.Logger.Warn("Maker arb paper insufficient balance", zap.Float64("cash", stats.CashBalance), zap.Float64("required", required))
		return
	}
	c := arb.NewPaperCycle(snap, upBook, downBook, now, r.tradeStream.LastSeq(), r.tradeStream.GapCount())
	if c == nil {
		return
	}
	if err := r.db.InsertArbPaperCycle(c); err != nil {
		util.Logger.Error("Maker arb paper cycle create failed", zap.Error(err))
		return
	}
	r.active = c
	util.Logger.Info("MAKER ARB PAPER SAFE-FIRST POSTED", zap.Int64("id", c.ID), zap.String("market", c.MarketSlug), zap.String("firstSide", c.FirstOrderSide), zap.Float64("firstPrice", c.FirstOrderPrice), zap.Float64("queueAhead", c.FirstQueueAhead), zap.String("completionSide", c.SecondOrderSide), zap.Float64("plannedCompletion", c.SecondOrderPrice), zap.Float64("shares", c.OrderSize), zap.Float64("entryNetEdge", c.EntryNetEdge))
}

func (r *arbShadowRuntime) logPaperTerminal(c *arb.PaperCycle) {
	if c == nil {
		return
	}
	util.Logger.Info("MAKER ARB PAPER CYCLE CLOSED", zap.Int64("id", c.ID), zap.String("market", c.MarketSlug), zap.String("status", c.Status), zap.String("firstLeg", c.ActualFirstLeg), zap.Float64("firstFilled", c.FirstFilledShares), zap.Float64("secondFilled", c.SecondFilledShares), zap.Int64("completionMs", c.CompletionMs), zap.Float64("lockedPnL", c.LockedPnL), zap.Float64("paperPnL", c.PaperPnL), zap.Int("reprices", c.Reprices), zap.String("reason", c.Reason))
}
