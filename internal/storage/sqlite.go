package storage

import (
	"database/sql"
	"fmt"
	"os"
	"strings"

	_ "modernc.org/sqlite"
	"pm-edge/internal/engine"
)

type Database struct {
	db *sql.DB
}

func NewDatabase(dbPath string) (*Database, error) {
	dir := ""
	for i := len(dbPath) - 1; i >= 0; i-- {
		if dbPath[i] == '/' {
			dir = dbPath[:i]
			break
		}
	}
	if dir != "" {
		if err := os.MkdirAll(dir, 0755); err != nil {
			return nil, err
		}
	}

	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return nil, err
	}
	inst := &Database{db: db}
	if err := inst.migrate(); err != nil {
		_ = db.Close()
		return nil, err
	}
	return inst, nil
}

func (d *Database) Close() error {
	if d.db != nil {
		return d.db.Close()
	}
	return nil
}

func (d *Database) migrate() error {
	query := `
	CREATE TABLE IF NOT EXISTS signals (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		timestamp TEXT NOT NULL,
		question TEXT NOT NULL,
		slug TEXT NOT NULL,
		market_end_time TEXT NOT NULL,
		price_to_beat REAL NOT NULL,
		current_price REAL NOT NULL,
		spot_minus_price_to_beat REAL NOT NULL,
		seconds_remaining REAL NOT NULL,
		p_up REAL NOT NULL,
		p_down REAL NOT NULL,
		bid_vol REAL NOT NULL,
		ask_vol REAL NOT NULL,
		spoof_filtered_bid_vol REAL NOT NULL,
		spoof_filtered_ask_vol REAL NOT NULL,
		imbalance REAL NOT NULL,
		weighted_imbalance REAL NOT NULL,
		probability_score REAL NOT NULL,
		order_flow_score REAL NOT NULL,
		technical_score REAL NOT NULL,
		volatility REAL NOT NULL,
		drift REAL NOT NULL,
		composite_score REAL NOT NULL,
		final_score REAL NOT NULL,
		decision TEXT NOT NULL,
		confidence REAL NOT NULL,
		market_stale INTEGER NOT NULL,
		data_source TEXT NOT NULL
	);
	CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
	`
	if _, err := d.db.Exec(query); err != nil {
		return err
	}

	// REV-FIX-1 cleanup: older builds generated this exact synthetic fallback
	// whenever Polymarket discovery failed. Those rows are not valid research data.
	_, err := d.db.Exec(`
		DELETE FROM signals
		WHERE slug = 'btc-above-100k-1505'
		   OR question = 'BTC above $100,000 at 15:05?';
	`)
	return err
}

func validateSignal(r *engine.EvaluationResult) error {
	if r == nil {
		return fmt.Errorf("nil signal")
	}
	if !strings.HasPrefix(r.Slug, "btc-updown-5m-") {
		return fmt.Errorf("unverified market slug: %q", r.Slug)
	}
	if r.PriceToBeat <= 0 || r.CurrentPrice <= 0 {
		return fmt.Errorf("invalid reference prices")
	}
	if r.SecondsRemaining <= 0 || r.SecondsRemaining > 305 {
		return fmt.Errorf("invalid remaining time: %f", r.SecondsRemaining)
	}
	if !strings.HasPrefix(r.DataSource, "CHAINLINK_RTDS+") || strings.Contains(r.DataSource, "MOCK") {
		return fmt.Errorf("unverified data source: %q", r.DataSource)
	}
	if r.PUp < 0 || r.PUp > 1 || r.PDown < 0 || r.PDown > 1 {
		return fmt.Errorf("invalid probabilities")
	}
	return nil
}

func (d *Database) InsertSignal(r *engine.EvaluationResult) error {
	if err := validateSignal(r); err != nil {
		return err
	}

	query := `
	INSERT INTO signals (
		timestamp, question, slug, market_end_time, price_to_beat, current_price,
		spot_minus_price_to_beat, seconds_remaining, p_up, p_down, bid_vol, ask_vol,
		spoof_filtered_bid_vol, spoof_filtered_ask_vol, imbalance, weighted_imbalance,
		probability_score, order_flow_score, technical_score, volatility, drift,
		composite_score, final_score, decision, confidence, market_stale, data_source
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`
	staleInt := 0
	if r.MarketStale {
		staleInt = 1
	}

	_, err := d.db.Exec(query,
		r.Timestamp, r.Question, r.Slug, r.MarketEndTime, r.PriceToBeat, r.CurrentPrice,
		r.SpotMinusPriceToBeat, r.SecondsRemaining, r.PUp, r.PDown, r.BidVol, r.AskVol,
		r.SpoofFilteredBidVol, r.SpoofFilteredAskVol, r.Imbalance, r.WeightedImbalance,
		r.ProbabilityScore, r.OrderFlowScore, r.TechnicalScore, r.Volatility, r.Drift,
		r.CompositeScore, r.FinalScore, r.Decision, r.Confidence, staleInt, r.DataSource,
	)
	return err
}

func (d *Database) GetHistory(limit int) ([]engine.EvaluationResult, error) {
	if limit < 1 {
		limit = 1
	}
	if limit > 10000 {
		limit = 10000
	}

	query := `
	SELECT
		timestamp, question, slug, market_end_time, price_to_beat, current_price,
		spot_minus_price_to_beat, seconds_remaining, p_up, p_down, bid_vol, ask_vol,
		spoof_filtered_bid_vol, spoof_filtered_ask_vol, imbalance, weighted_imbalance,
		probability_score, order_flow_score, technical_score, volatility, drift,
		composite_score, final_score, decision, confidence, market_stale, data_source
	FROM signals
	ORDER BY id DESC
	LIMIT ?
	`
	rows, err := d.db.Query(query, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var results []engine.EvaluationResult
	for rows.Next() {
		var r engine.EvaluationResult
		var staleInt int
		if err := rows.Scan(
			&r.Timestamp, &r.Question, &r.Slug, &r.MarketEndTime, &r.PriceToBeat, &r.CurrentPrice,
			&r.SpotMinusPriceToBeat, &r.SecondsRemaining, &r.PUp, &r.PDown, &r.BidVol, &r.AskVol,
			&r.SpoofFilteredBidVol, &r.SpoofFilteredAskVol, &r.Imbalance, &r.WeightedImbalance,
			&r.ProbabilityScore, &r.OrderFlowScore, &r.TechnicalScore, &r.Volatility, &r.Drift,
			&r.CompositeScore, &r.FinalScore, &r.Decision, &r.Confidence, &staleInt, &r.DataSource,
		); err != nil {
			return nil, err
		}
		r.MarketStale = staleInt == 1
		results = append(results, r)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return results, nil
}
