package main

import (
	"sync"
	"sync/atomic"
	"time"

	"go.uber.org/zap"
	"pm-edge/internal/arb"
	"pm-edge/internal/config"
	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
	"pm-edge/internal/util"
)

type arbShadowRuntime struct {
	engine              *arb.Engine
	db                  *storage.Database
	pmClient            *polymarket.Client
	busy                atomic.Bool
	paperEnabled        bool
	paperCfg            arb.PaperConfig
	paperInitialBalance float64
	maxBookFetchMs      int64
	active              *arb.PaperCycle
}

func newArbShadowRuntime(tf string, cfg *config.Config, db *storage.Database, pmClient *polymarket.Client, enabled bool) *arbShadowRuntime {
	r := &arbShadowRuntime{engine: arb.NewEngine(arb.Config{
		Timeframe: tf, Enabled: enabled && cfg.ArbShadowEnabled,
		TargetEdge: cfg.ArbTargetEdge, OperationalBuffer: cfg.ArbOperationalBuffer,
		UncertaintyPenalty: cfg.ArbUncertaintyPenalty, MaxStrandedUnits: cfg.ArbMaxStrandedUnits,
	}), db: db, pmClient: pmClient, paperEnabled: enabled && cfg.ArbPaperEnabled,
		paperCfg:            arb.PaperConfig{Enabled: enabled && cfg.ArbPaperEnabled, OrderTTL: time.Duration(cfg.ArbPaperOrderTTLSec) * time.Second, MaxStranded: time.Duration(cfg.ArbPaperMaxStrandedSec) * time.Second, StopBeforeEnd: time.Duration(cfg.ArbPaperStopBeforeEndSec) * time.Second},
		paperInitialBalance: cfg.PaperInitialBalance, maxBookFetchMs: int64(cfg.ArbMaxBookFetchMs)}
	if r.maxBookFetchMs <= 0 {
		r.maxBookFetchMs = 1000
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

func (r *arbShadowRuntime) Submit(res *engine.EvaluationResult, market *polymarket.Market) {
	if r == nil || r.engine == nil || !r.engine.Enabled() || res == nil || market == nil {
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
		if snap.Status == arb.StatusCandidate && fetchMs > r.maxBookFetchMs {
			snap.Status = arb.StatusBlocked
			snap.Reason = "BOOK_FETCH_TOO_SLOW"
		}
		if err := r.db.InsertArbSnapshot(snap); err != nil {
			util.Logger.Error("Maker arb snapshot store failed", zap.Error(err))
			return
		}

		now := time.Now().UTC()
		r.processPaper(snap, upBook, downBook, &mc, now)
		if snap.Status == arb.StatusCandidate {
			util.Logger.Info("MAKER ARB SHADOW CANDIDATE", zap.String("market", snap.MarketSlug), zap.Float64("up", snap.UpMakerPrice), zap.Float64("down", snap.DownMakerPrice), zap.Float64("netEdge", snap.NetEdge), zap.Float64("shares", snap.OrderSize), zap.String("safeFirstLeg", snap.FirstLeg), zap.Int64("bookFetchMs", snap.BookFetchMs))
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
		if arb.AdvancePaperCycle(r.active, upBook, downBook, now, market.EndTime, r.paperCfg) {
			if err := r.db.UpdateArbPaperCycle(r.active); err != nil {
				util.Logger.Error("Maker arb paper update failed", zap.Error(err))
				return
			}
		}
		if r.active.IsTerminal() {
			r.logPaperTerminal(r.active)
			r.active = nil
		}
	}
	if r.active != nil || snap.Status != arb.StatusCandidate {
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
	c := arb.NewPaperCycle(snap, now)
	if c == nil {
		return
	}
	if err := r.db.InsertArbPaperCycle(c); err != nil {
		util.Logger.Error("Maker arb paper cycle create failed", zap.Error(err))
		return
	}
	r.active = c
	util.Logger.Info("MAKER ARB PAPER PAIR POSTED", zap.Int64("id", c.ID), zap.String("market", c.MarketSlug), zap.Float64("up", c.UpOrderPrice), zap.Float64("down", c.DownOrderPrice), zap.Float64("shares", c.OrderSize), zap.String("preferredFirst", c.PreferredFirstLeg))
}

func (r *arbShadowRuntime) logPaperTerminal(c *arb.PaperCycle) {
	if c == nil {
		return
	}
	util.Logger.Info("MAKER ARB PAPER CYCLE CLOSED", zap.Int64("id", c.ID), zap.String("market", c.MarketSlug), zap.String("status", c.Status), zap.String("firstLeg", c.ActualFirstLeg), zap.Bool("preferredMatched", c.PreferredFirstMatched), zap.Int64("completionMs", c.CompletionMs), zap.Float64("lockedPnL", c.LockedPnL), zap.Float64("paperPnL", c.PaperPnL), zap.Int("reprices", c.Reprices), zap.String("reason", c.Reason))
}
