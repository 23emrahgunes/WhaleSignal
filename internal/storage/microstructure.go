package storage

import (
	"fmt"

	"pm-edge/internal/engine"
)

type MicrostructureSnapshot struct {
	Timestamp           string  `json:"timestamp"`
	Slug                string  `json:"slug"`
	Timeframe           string  `json:"timeframe"`
	Ready               bool    `json:"ready"`
	Synchronized        bool    `json:"synchronized"`
	Source              string  `json:"source"`
	AgeMs               int64   `json:"ageMs"`
	BidLevels           int     `json:"bidLevels"`
	AskLevels           int     `json:"askLevels"`
	Band10BidUSD        float64 `json:"band10BidUsd"`
	Band10AskUSD        float64 `json:"band10AskUsd"`
	Band10Imbalance     float64 `json:"band10Imbalance"`
	Band25BidUSD        float64 `json:"band25BidUsd"`
	Band25AskUSD        float64 `json:"band25AskUsd"`
	Band25Imbalance     float64 `json:"band25Imbalance"`
	Band50BidUSD        float64 `json:"band50BidUsd"`
	Band50AskUSD        float64 `json:"band50AskUsd"`
	Band50Imbalance     float64 `json:"band50Imbalance"`
	Band75BidUSD        float64 `json:"band75BidUsd"`
	Band75AskUSD        float64 `json:"band75AskUsd"`
	Band75Imbalance     float64 `json:"band75Imbalance"`
	Trade5BuyUSD        float64 `json:"trade5BuyUsd"`
	Trade5SellUSD       float64 `json:"trade5SellUsd"`
	Trade5Imbalance     float64 `json:"trade5Imbalance"`
	Trade15BuyUSD       float64 `json:"trade15BuyUsd"`
	Trade15SellUSD      float64 `json:"trade15SellUsd"`
	Trade15Imbalance    float64 `json:"trade15Imbalance"`
	Trade30BuyUSD       float64 `json:"trade30BuyUsd"`
	Trade30SellUSD      float64 `json:"trade30SellUsd"`
	Trade30Imbalance    float64 `json:"trade30Imbalance"`
	Trade60BuyUSD       float64 `json:"trade60BuyUsd"`
	Trade60SellUSD      float64 `json:"trade60SellUsd"`
	Trade60Imbalance    float64 `json:"trade60Imbalance"`
	TradeAcceleration   float64 `json:"tradeAcceleration"`
	BidWallScore        float64 `json:"bidWallScore"`
	AskWallScore        float64 `json:"askWallScore"`
	BidDepletionScore   float64 `json:"bidDepletionScore"`
	AskDepletionScore   float64 `json:"askDepletionScore"`
	PTBPathBidUSD       float64 `json:"ptbPathBidUsd"`
	PTBPathAskUSD       float64 `json:"ptbPathAskUsd"`
	PTBBeyondUSD        float64 `json:"ptbBeyondUsd"`
	PTBBarrierScore     float64 `json:"ptbBarrierScore"`
	DeepBookScore       float64 `json:"deepBookScore"`
	TradeFlowScore      float64 `json:"tradeFlowScore"`
	WallDynamicsScore   float64 `json:"wallDynamicsScore"`
	MicrostructureScore float64 `json:"microstructureScore"`
	ShadowModelBScore   float64 `json:"shadowModelBScore"`
	ShadowDecision      string  `json:"shadowDecision"`
	ShadowConfidence    float64 `json:"shadowConfidence"`
}

func (d *Database) EnsureMicrostructureSchema() error {
	_, err := d.db.Exec(`
	CREATE TABLE IF NOT EXISTS microstructure_snapshots (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		timestamp TEXT NOT NULL,
		slug TEXT NOT NULL,
		timeframe TEXT NOT NULL,
		ready INTEGER NOT NULL,
		synchronized INTEGER NOT NULL,
		source TEXT NOT NULL,
		age_ms INTEGER NOT NULL,
		bid_levels INTEGER NOT NULL,
		ask_levels INTEGER NOT NULL,
		band10_bid_usd REAL NOT NULL, band10_ask_usd REAL NOT NULL, band10_imbalance REAL NOT NULL,
		band25_bid_usd REAL NOT NULL, band25_ask_usd REAL NOT NULL, band25_imbalance REAL NOT NULL,
		band50_bid_usd REAL NOT NULL, band50_ask_usd REAL NOT NULL, band50_imbalance REAL NOT NULL,
		band75_bid_usd REAL NOT NULL, band75_ask_usd REAL NOT NULL, band75_imbalance REAL NOT NULL,
		trade5_buy_usd REAL NOT NULL, trade5_sell_usd REAL NOT NULL, trade5_imbalance REAL NOT NULL,
		trade15_buy_usd REAL NOT NULL, trade15_sell_usd REAL NOT NULL, trade15_imbalance REAL NOT NULL,
		trade30_buy_usd REAL NOT NULL, trade30_sell_usd REAL NOT NULL, trade30_imbalance REAL NOT NULL,
		trade60_buy_usd REAL NOT NULL, trade60_sell_usd REAL NOT NULL, trade60_imbalance REAL NOT NULL,
		trade_acceleration REAL NOT NULL,
		bid_wall_score REAL NOT NULL, ask_wall_score REAL NOT NULL,
		bid_depletion_score REAL NOT NULL, ask_depletion_score REAL NOT NULL,
		ptb_path_bid_usd REAL NOT NULL, ptb_path_ask_usd REAL NOT NULL, ptb_beyond_usd REAL NOT NULL, ptb_barrier_score REAL NOT NULL,
		deep_book_score REAL NOT NULL, trade_flow_score REAL NOT NULL, wall_dynamics_score REAL NOT NULL, microstructure_score REAL NOT NULL,
		shadow_model_b_score REAL NOT NULL, shadow_decision TEXT NOT NULL, shadow_confidence REAL NOT NULL,
		UNIQUE(timestamp, slug)
	);
	CREATE INDEX IF NOT EXISTS idx_microstructure_tf_time ON microstructure_snapshots(timeframe, timestamp DESC);
	CREATE INDEX IF NOT EXISTS idx_microstructure_slug ON microstructure_snapshots(slug);
	`)
	return err
}

func (d *Database) InsertMicrostructureSnapshot(r *engine.EvaluationResult) error {
	if r == nil || r.Slug == "" || r.Timestamp == "" {
		return fmt.Errorf("invalid microstructure signal")
	}
	m := snapshotFromEvaluation(r)
	_, err := d.db.Exec(`
	INSERT OR REPLACE INTO microstructure_snapshots (
		timestamp, slug, timeframe, ready, synchronized, source, age_ms, bid_levels, ask_levels,
		band10_bid_usd, band10_ask_usd, band10_imbalance,
		band25_bid_usd, band25_ask_usd, band25_imbalance,
		band50_bid_usd, band50_ask_usd, band50_imbalance,
		band75_bid_usd, band75_ask_usd, band75_imbalance,
		trade5_buy_usd, trade5_sell_usd, trade5_imbalance,
		trade15_buy_usd, trade15_sell_usd, trade15_imbalance,
		trade30_buy_usd, trade30_sell_usd, trade30_imbalance,
		trade60_buy_usd, trade60_sell_usd, trade60_imbalance,
		trade_acceleration, bid_wall_score, ask_wall_score, bid_depletion_score, ask_depletion_score,
		ptb_path_bid_usd, ptb_path_ask_usd, ptb_beyond_usd, ptb_barrier_score,
		deep_book_score, trade_flow_score, wall_dynamics_score, microstructure_score,
		shadow_model_b_score, shadow_decision, shadow_confidence
	) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		m.Timestamp, m.Slug, m.Timeframe, boolInt(m.Ready), boolInt(m.Synchronized), m.Source, m.AgeMs, m.BidLevels, m.AskLevels,
		m.Band10BidUSD, m.Band10AskUSD, m.Band10Imbalance,
		m.Band25BidUSD, m.Band25AskUSD, m.Band25Imbalance,
		m.Band50BidUSD, m.Band50AskUSD, m.Band50Imbalance,
		m.Band75BidUSD, m.Band75AskUSD, m.Band75Imbalance,
		m.Trade5BuyUSD, m.Trade5SellUSD, m.Trade5Imbalance,
		m.Trade15BuyUSD, m.Trade15SellUSD, m.Trade15Imbalance,
		m.Trade30BuyUSD, m.Trade30SellUSD, m.Trade30Imbalance,
		m.Trade60BuyUSD, m.Trade60SellUSD, m.Trade60Imbalance,
		m.TradeAcceleration, m.BidWallScore, m.AskWallScore, m.BidDepletionScore, m.AskDepletionScore,
		m.PTBPathBidUSD, m.PTBPathAskUSD, m.PTBBeyondUSD, m.PTBBarrierScore,
		m.DeepBookScore, m.TradeFlowScore, m.WallDynamicsScore, m.MicrostructureScore,
		m.ShadowModelBScore, m.ShadowDecision, m.ShadowConfidence)
	return err
}

func (d *Database) GetMicrostructureSnapshots(limit int, timeframe string) ([]MicrostructureSnapshot, error) {
	if limit <= 0 {
		return []MicrostructureSnapshot{}, nil
	}
	if limit > 10000 {
		limit = 10000
	}
	rows, err := d.db.Query(`SELECT timestamp, slug, timeframe, ready, synchronized, source, age_ms, bid_levels, ask_levels,
		band10_bid_usd, band10_ask_usd, band10_imbalance,
		band25_bid_usd, band25_ask_usd, band25_imbalance,
		band50_bid_usd, band50_ask_usd, band50_imbalance,
		band75_bid_usd, band75_ask_usd, band75_imbalance,
		trade5_buy_usd, trade5_sell_usd, trade5_imbalance,
		trade15_buy_usd, trade15_sell_usd, trade15_imbalance,
		trade30_buy_usd, trade30_sell_usd, trade30_imbalance,
		trade60_buy_usd, trade60_sell_usd, trade60_imbalance,
		trade_acceleration, bid_wall_score, ask_wall_score, bid_depletion_score, ask_depletion_score,
		ptb_path_bid_usd, ptb_path_ask_usd, ptb_beyond_usd, ptb_barrier_score,
		deep_book_score, trade_flow_score, wall_dynamics_score, microstructure_score,
		shadow_model_b_score, shadow_decision, shadow_confidence
		FROM microstructure_snapshots WHERE timeframe=? ORDER BY id DESC LIMIT ?`, NormalizeTimeframe(timeframe), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]MicrostructureSnapshot, 0)
	for rows.Next() {
		var m MicrostructureSnapshot
		var ready, synced int
		if err := rows.Scan(&m.Timestamp, &m.Slug, &m.Timeframe, &ready, &synced, &m.Source, &m.AgeMs, &m.BidLevels, &m.AskLevels,
			&m.Band10BidUSD, &m.Band10AskUSD, &m.Band10Imbalance,
			&m.Band25BidUSD, &m.Band25AskUSD, &m.Band25Imbalance,
			&m.Band50BidUSD, &m.Band50AskUSD, &m.Band50Imbalance,
			&m.Band75BidUSD, &m.Band75AskUSD, &m.Band75Imbalance,
			&m.Trade5BuyUSD, &m.Trade5SellUSD, &m.Trade5Imbalance,
			&m.Trade15BuyUSD, &m.Trade15SellUSD, &m.Trade15Imbalance,
			&m.Trade30BuyUSD, &m.Trade30SellUSD, &m.Trade30Imbalance,
			&m.Trade60BuyUSD, &m.Trade60SellUSD, &m.Trade60Imbalance,
			&m.TradeAcceleration, &m.BidWallScore, &m.AskWallScore, &m.BidDepletionScore, &m.AskDepletionScore,
			&m.PTBPathBidUSD, &m.PTBPathAskUSD, &m.PTBBeyondUSD, &m.PTBBarrierScore,
			&m.DeepBookScore, &m.TradeFlowScore, &m.WallDynamicsScore, &m.MicrostructureScore,
			&m.ShadowModelBScore, &m.ShadowDecision, &m.ShadowConfidence); err != nil {
			return nil, err
		}
		m.Ready = ready == 1
		m.Synchronized = synced == 1
		out = append(out, m)
	}
	return out, rows.Err()
}

func snapshotFromEvaluation(r *engine.EvaluationResult) MicrostructureSnapshot {
	m := MicrostructureSnapshot{Timestamp: r.Timestamp, Slug: r.Slug, Timeframe: TimeframeFromMarketSlug(r.Slug), Ready: r.DeepMicrostructure.Ready, Synchronized: r.DeepMicrostructure.Synchronized, Source: r.DeepMicrostructure.Source, AgeMs: r.DeepMicrostructure.AgeMs, BidLevels: r.DeepMicrostructure.BidLevels, AskLevels: r.DeepMicrostructure.AskLevels, TradeAcceleration: r.DeepMicrostructure.TradeAcceleration, BidWallScore: r.DeepMicrostructure.BidWallScore, AskWallScore: r.DeepMicrostructure.AskWallScore, BidDepletionScore: r.DeepMicrostructure.BidDepletionScore, AskDepletionScore: r.DeepMicrostructure.AskDepletionScore, PTBPathBidUSD: r.DeepMicrostructure.PTBPathBidUSD, PTBPathAskUSD: r.DeepMicrostructure.PTBPathAskUSD, PTBBeyondUSD: r.DeepMicrostructure.PTBBeyondUSD, PTBBarrierScore: r.PTBBarrierScore, DeepBookScore: r.DeepBookScore, TradeFlowScore: r.TradeFlowScore, WallDynamicsScore: r.WallDynamicsScore, MicrostructureScore: r.MicrostructureScore, ShadowModelBScore: r.ShadowModelBScore, ShadowDecision: r.ShadowDecision, ShadowConfidence: r.ShadowConfidence}
	for _, b := range r.DeepMicrostructure.Bands {
		switch int(b.DistanceUSD) {
		case 10:
			m.Band10BidUSD, m.Band10AskUSD, m.Band10Imbalance = b.BidUSD, b.AskUSD, b.Imbalance
		case 25:
			m.Band25BidUSD, m.Band25AskUSD, m.Band25Imbalance = b.BidUSD, b.AskUSD, b.Imbalance
		case 50:
			m.Band50BidUSD, m.Band50AskUSD, m.Band50Imbalance = b.BidUSD, b.AskUSD, b.Imbalance
		case 75:
			m.Band75BidUSD, m.Band75AskUSD, m.Band75Imbalance = b.BidUSD, b.AskUSD, b.Imbalance
		}
	}
	for _, w := range r.DeepMicrostructure.Trades {
		switch w.Seconds {
		case 5:
			m.Trade5BuyUSD, m.Trade5SellUSD, m.Trade5Imbalance = w.BuyUSD, w.SellUSD, w.Imbalance
		case 15:
			m.Trade15BuyUSD, m.Trade15SellUSD, m.Trade15Imbalance = w.BuyUSD, w.SellUSD, w.Imbalance
		case 30:
			m.Trade30BuyUSD, m.Trade30SellUSD, m.Trade30Imbalance = w.BuyUSD, w.SellUSD, w.Imbalance
		case 60:
			m.Trade60BuyUSD, m.Trade60SellUSD, m.Trade60Imbalance = w.BuyUSD, w.SellUSD, w.Imbalance
		}
	}
	return m
}

func boolInt(v bool) int {
	if v {
		return 1
	}
	return 0
}
