package storage

import (
	"database/sql"
	"os"

	"pm-edge/internal/engine"
	_ "modernc.org/sqlite"
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
		_ = os.MkdirAll(dir, 0755)
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
	// Let's drop old table if columns mismatch, or recreate cleanly with updated fields.
	// We want standard clean migrations. Let's create with the new properties.
	queryDrop := `DROP TABLE IF EXISTS signals;`
	_, _ = d.db.Exec(queryDrop)

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
	`
	_, err := d.db.Exec(query)
	return err
}

func (d *Database) InsertSignal(r *engine.EvaluationResult) error {
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
		err := rows.Scan(
			&r.Timestamp, &r.Question, &r.Slug, &r.MarketEndTime, &r.PriceToBeat, &r.CurrentPrice,
			&r.SpotMinusPriceToBeat, &r.SecondsRemaining, &r.PUp, &r.PDown, &r.BidVol, &r.AskVol,
			&r.SpoofFilteredBidVol, &r.SpoofFilteredAskVol, &r.Imbalance, &r.WeightedImbalance,
			&r.ProbabilityScore, &r.OrderFlowScore, &r.TechnicalScore, &r.Volatility, &r.Drift,
			&r.CompositeScore, &r.FinalScore, &r.Decision, &r.Confidence, &staleInt, &r.DataSource,
		)
		if err != nil {
			return nil, err
		}
		r.MarketStale = (staleInt == 1)
		results = append(results, r)
	}

	return results, nil
}
