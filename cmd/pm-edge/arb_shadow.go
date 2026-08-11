package main

import (
	"sync/atomic"

	"go.uber.org/zap"
	"pm-edge/internal/arb"
	"pm-edge/internal/config"
	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
	"pm-edge/internal/util"
)

type arbShadowRuntime struct {
	engine   *arb.Engine
	db       *storage.Database
	pmClient *polymarket.Client
	busy     atomic.Bool
}

func newArbShadowRuntime(tf string, cfg *config.Config, db *storage.Database, pmClient *polymarket.Client, enabled bool) *arbShadowRuntime {
	return &arbShadowRuntime{engine: arb.NewEngine(arb.Config{
		Timeframe: tf, Enabled: enabled && cfg.ArbShadowEnabled,
		TargetEdge: cfg.ArbTargetEdge, OperationalBuffer: cfg.ArbOperationalBuffer,
		UncertaintyPenalty: cfg.ArbUncertaintyPenalty, MaxStrandedUnits: cfg.ArbMaxStrandedUnits,
	}), db: db, pmClient: pmClient}
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
		upBook, err := r.pmClient.FetchBookSnapshot(upID)
		if err != nil {
			util.Logger.Warn("Maker arb UP book unavailable", zap.Error(err), zap.String("market", mc.Slug))
			return
		}
		downBook, err := r.pmClient.FetchBookSnapshot(downID)
		if err != nil {
			util.Logger.Warn("Maker arb DOWN book unavailable", zap.Error(err), zap.String("market", mc.Slug))
			return
		}
		snap := r.engine.Evaluate(&rc, &mc, upBook, downBook)
		if snap == nil {
			return
		}
		if err := r.db.InsertArbSnapshot(snap); err != nil {
			util.Logger.Error("Maker arb snapshot store failed", zap.Error(err))
			return
		}
		if snap.Status == arb.StatusCandidate {
			util.Logger.Info("MAKER ARB SHADOW CANDIDATE", zap.String("market", snap.MarketSlug), zap.Float64("up", snap.UpMakerPrice), zap.Float64("down", snap.DownMakerPrice), zap.Float64("netEdge", snap.NetEdge), zap.Float64("shares", snap.OrderSize), zap.String("safeFirstLeg", snap.FirstLeg), zap.Float64("upStrandedEV", snap.UpStrandedEV), zap.Float64("downStrandedEV", snap.DownStrandedEV))
		}
	}()
}
