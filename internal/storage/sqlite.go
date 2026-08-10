package storage

import (
	"database/sql"
	"fmt"
	"os"
	"strings"

	_ "modernc.org/sqlite"
	"pm-edge/internal/engine"
)

type Database struct{ db *sql.DB }

type PaperTrade struct {
	ID                  int64   `json:"id"`
	MarketSlug          string  `json:"marketSlug"`
	Question            string  `json:"question"`
	Side                string  `json:"side"`
	EntryTime           string  `json:"entryTime"`
	MarketEndTime       string  `json:"marketEndTime"`
	EntryConfidence     float64 `json:"entryConfidence"`
	EntryFinalScore     float64 `json:"entryFinalScore"`
	EntryProbability    float64 `json:"entryProbability"`
	EntryPrice          float64 `json:"entryPrice"`
	Stake               float64 `json:"stake"`
	Shares              float64 `json:"shares"`
	PriceToBeat         float64 `json:"priceToBeat"`
	EntryReferencePrice float64 `json:"entryReferencePrice"`
	Status              string  `json:"status"`
	SettlementTime      string  `json:"settlementTime"`
	SettlementPrice     float64 `json:"settlementPrice"`
	Outcome             string  `json:"outcome"`
	Won                 bool    `json:"won"`
	Payout              float64 `json:"payout"`
	PnL                 float64 `json:"pnl"`
	Source              string  `json:"source"`
}

type PaperStats struct {
	InitialBalance          float64 `json:"initialBalance"`
	CashBalance             float64 `json:"cashBalance"`
	Equity                  float64 `json:"equity"`
	RealizedPnL             float64 `json:"realizedPnl"`
	OpenStake               float64 `json:"openStake"`
	TotalTrades             int     `json:"totalTrades"`
	SettledTrades           int     `json:"settledTrades"`
	OpenTrades              int     `json:"openTrades"`
	Wins                    int     `json:"wins"`
	Losses                  int     `json:"losses"`
	WinRate                 float64 `json:"winRate"`
	CalibrationN            int     `json:"calibrationN"`
	AverageEntryProbability float64 `json:"averageEntryProbability"`
	ActualWinProbability    float64 `json:"actualWinProbability"`
	CalibrationGap          float64 `json:"calibrationGap"`
	BrierScore              float64 `json:"brierScore"`
	ExpectedWins            float64 `json:"expectedWins"`
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

	CREATE TABLE IF NOT EXISTS paper_trades (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		market_slug TEXT NOT NULL UNIQUE,
		question TEXT NOT NULL,
		side TEXT NOT NULL,
		entry_time TEXT NOT NULL,
		market_end_time TEXT NOT NULL,
		entry_confidence REAL NOT NULL,
		entry_final_score REAL NOT NULL,
		entry_probability REAL NOT NULL,
		entry_price REAL NOT NULL,
		stake REAL NOT NULL,
		shares REAL NOT NULL,
		price_to_beat REAL NOT NULL,
		entry_reference_price REAL NOT NULL,
		status TEXT NOT NULL DEFAULT 'OPEN',
		settlement_time TEXT NOT NULL DEFAULT '',
		settlement_price REAL NOT NULL DEFAULT 0,
		outcome TEXT NOT NULL DEFAULT '',
		won INTEGER NOT NULL DEFAULT 0,
		payout REAL NOT NULL DEFAULT 0,
		pnl REAL NOT NULL DEFAULT 0,
		source TEXT NOT NULL
	);
	CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
	CREATE INDEX IF NOT EXISTS idx_paper_trades_entry_time ON paper_trades(entry_time);

	-- Remove only the exact synthetic fallback rows produced by the old evaluator.
	DELETE FROM signals WHERE slug = 'btc-above-100k-1505' AND price_to_beat = 100000;
	`
	if _, err := d.db.Exec(query); err != nil {
		return err
	}
	return d.ensureSignalResearchColumns()
}

func (d *Database) ensureSignalResearchColumns() error {
	columns := []struct {
		name    string
		typeSQL string
	}{
		{"binance_price", "REAL NOT NULL DEFAULT 0"},
		{"chainlink_binance_basis_bps", "REAL NOT NULL DEFAULT 0"},
		{"forecast_samples", "INTEGER NOT NULL DEFAULT 0"},
		{"forecast_price", "REAL NOT NULL DEFAULT 0"},
		{"forecast_mean_price", "REAL NOT NULL DEFAULT 0"},
		{"forecast_low68", "REAL NOT NULL DEFAULT 0"},
		{"forecast_high68", "REAL NOT NULL DEFAULT 0"},
		{"forecast_low95", "REAL NOT NULL DEFAULT 0"},
		{"forecast_high95", "REAL NOT NULL DEFAULT 0"},
		{"ptb_z", "REAL NOT NULL DEFAULT 0"},
		{"required_move_bps", "REAL NOT NULL DEFAULT 0"},
		{"expected_move_bps", "REAL NOT NULL DEFAULT 0"},
		{"forecast_sigma_expiry_bps", "REAL NOT NULL DEFAULT 0"},
		{"forecast_confidence", "REAL NOT NULL DEFAULT 0"},
		{"micro_volatility_annual", "REAL NOT NULL DEFAULT 0"},
		{"volatility_floor_annual", "REAL NOT NULL DEFAULT 0"},
		{"basis_volatility_annual", "REAL NOT NULL DEFAULT 0"},
	}
	rows, err := d.db.Query("PRAGMA table_info(signals)")
	if err != nil {
		return err
	}
	existing := make(map[string]bool)
	for rows.Next() {
		var cid int
		var name, typ string
		var notnull, pk int
		var defaultValue interface{}
		if err := rows.Scan(&cid, &name, &typ, &notnull, &defaultValue, &pk); err != nil {
			rows.Close()
			return err
		}
		existing[name] = true
	}
	if err := rows.Close(); err != nil {
		return err
	}
	for _, column := range columns {
		if existing[column.name] {
			continue
		}
		if _, err := d.db.Exec(fmt.Sprintf("ALTER TABLE signals ADD COLUMN %s %s", column.name, column.typeSQL)); err != nil {
			return fmt.Errorf("add signals.%s: %w", column.name, err)
		}
	}
	return nil
}

func (d *Database) InsertSignal(r *engine.EvaluationResult) error {
	if r == nil {
		return fmt.Errorf("refusing nil signal")
	}
	if r.PriceToBeat <= 0 || r.CurrentPrice <= 0 {
		return fmt.Errorf("refusing signal with invalid prices")
	}
	if r.SecondsRemaining <= 0 || r.SecondsRemaining > 305 {
		return fmt.Errorf("refusing signal with invalid remaining time %.3f", r.SecondsRemaining)
	}
	if r.MarketStale {
		return fmt.Errorf("refusing stale market signal")
	}
	if !strings.HasPrefix(r.Slug, "btc-updown-5m-") {
		return fmt.Errorf("refusing non-canonical BTC 5m slug %q", r.Slug)
	}
	if strings.Contains(strings.ToUpper(r.DataSource), "MOCK") {
		return fmt.Errorf("refusing mock signal")
	}

	query := `
	INSERT INTO signals (
		timestamp, question, slug, market_end_time, price_to_beat, current_price,
		binance_price, chainlink_binance_basis_bps, spot_minus_price_to_beat, seconds_remaining, p_up, p_down,
		forecast_samples, forecast_price, forecast_mean_price, forecast_low68, forecast_high68, forecast_low95, forecast_high95,
		ptb_z, required_move_bps, expected_move_bps, forecast_sigma_expiry_bps, forecast_confidence,
		micro_volatility_annual, volatility_floor_annual, basis_volatility_annual,
		bid_vol, ask_vol, spoof_filtered_bid_vol, spoof_filtered_ask_vol, imbalance, weighted_imbalance,
		probability_score, order_flow_score, technical_score, volatility, drift,
		composite_score, final_score, decision, confidence, market_stale, data_source
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
	_, err := d.db.Exec(query,
		r.Timestamp, r.Question, r.Slug, r.MarketEndTime, r.PriceToBeat, r.CurrentPrice,
		r.BinancePrice, r.ChainlinkBinanceBasisBps, r.SpotMinusPriceToBeat, r.SecondsRemaining, r.PUp, r.PDown,
		r.ForecastSamples, r.ForecastPrice, r.ForecastMeanPrice, r.ForecastLow68, r.ForecastHigh68, r.ForecastLow95, r.ForecastHigh95,
		r.PTBZ, r.RequiredMoveBps, r.ExpectedMoveBps, r.ForecastSigmaExpiryBps, r.ForecastConfidence,
		r.MicroVolatilityAnnual, r.VolatilityFloorAnnual, r.BasisVolatilityAnnual,
		r.BidVol, r.AskVol, r.SpoofFilteredBidVol, r.SpoofFilteredAskVol, r.Imbalance, r.WeightedImbalance,
		r.ProbabilityScore, r.OrderFlowScore, r.TechnicalScore, r.Volatility, r.Drift,
		r.CompositeScore, r.FinalScore, r.Decision, r.Confidence, 0, r.DataSource)
	return err
}

func (d *Database) GetHistory(limit int) ([]engine.EvaluationResult, error) {
	if limit <= 0 {
		return []engine.EvaluationResult{}, nil
	}
	if limit > 10000 {
		limit = 10000
	}
	query := `
	SELECT timestamp, question, slug, market_end_time, price_to_beat, current_price,
		binance_price, chainlink_binance_basis_bps, spot_minus_price_to_beat, seconds_remaining, p_up, p_down,
		forecast_samples, forecast_price, forecast_mean_price, forecast_low68, forecast_high68, forecast_low95, forecast_high95,
		ptb_z, required_move_bps, expected_move_bps, forecast_sigma_expiry_bps, forecast_confidence,
		micro_volatility_annual, volatility_floor_annual, basis_volatility_annual,
		bid_vol, ask_vol, spoof_filtered_bid_vol, spoof_filtered_ask_vol, imbalance, weighted_imbalance,
		probability_score, order_flow_score, technical_score, volatility, drift,
		composite_score, final_score, decision, confidence, market_stale, data_source
	FROM signals ORDER BY id DESC LIMIT ?`
	rows, err := d.db.Query(query, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	results := make([]engine.EvaluationResult, 0)
	for rows.Next() {
		var r engine.EvaluationResult
		var stale int
		if err := rows.Scan(
			&r.Timestamp, &r.Question, &r.Slug, &r.MarketEndTime, &r.PriceToBeat, &r.CurrentPrice,
			&r.BinancePrice, &r.ChainlinkBinanceBasisBps, &r.SpotMinusPriceToBeat, &r.SecondsRemaining, &r.PUp, &r.PDown,
			&r.ForecastSamples, &r.ForecastPrice, &r.ForecastMeanPrice, &r.ForecastLow68, &r.ForecastHigh68, &r.ForecastLow95, &r.ForecastHigh95,
			&r.PTBZ, &r.RequiredMoveBps, &r.ExpectedMoveBps, &r.ForecastSigmaExpiryBps, &r.ForecastConfidence,
			&r.MicroVolatilityAnnual, &r.VolatilityFloorAnnual, &r.BasisVolatilityAnnual,
			&r.BidVol, &r.AskVol, &r.SpoofFilteredBidVol, &r.SpoofFilteredAskVol, &r.Imbalance, &r.WeightedImbalance,
			&r.ProbabilityScore, &r.OrderFlowScore, &r.TechnicalScore, &r.Volatility, &r.Drift,
			&r.CompositeScore, &r.FinalScore, &r.Decision, &r.Confidence, &stale, &r.DataSource); err != nil {
			return nil, err
		}
		r.MarketStale = stale == 1
		results = append(results, r)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return results, nil
}

func (d *Database) CreatePaperTrade(t *PaperTrade) (bool, error) {
	if t == nil {
		return false, fmt.Errorf("nil paper trade")
	}
	if !strings.HasPrefix(t.MarketSlug, "btc-updown-5m-") {
		return false, fmt.Errorf("invalid paper market slug %q", t.MarketSlug)
	}
	if t.Side != "UP" && t.Side != "DOWN" {
		return false, fmt.Errorf("invalid paper side %q", t.Side)
	}
	if t.EntryPrice <= 0 || t.EntryPrice >= 1 || t.Stake <= 0 || t.Shares <= 0 || t.PriceToBeat <= 0 || t.EntryReferencePrice <= 0 {
		return false, fmt.Errorf("invalid paper trade economics")
	}
	if strings.Contains(strings.ToUpper(t.Source), "MOCK") {
		return false, fmt.Errorf("refusing mock paper trade")
	}
	res, err := d.db.Exec(`
		INSERT OR IGNORE INTO paper_trades (
			market_slug, question, side, entry_time, market_end_time, entry_confidence,
			entry_final_score, entry_probability, entry_price, stake, shares, price_to_beat,
			entry_reference_price, status, source
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)`,
		t.MarketSlug, t.Question, t.Side, t.EntryTime, t.MarketEndTime, t.EntryConfidence,
		t.EntryFinalScore, t.EntryProbability, t.EntryPrice, t.Stake, t.Shares, t.PriceToBeat,
		t.EntryReferencePrice, t.Source)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n == 1, err
}

func (d *Database) SettlePaperTrade(id int64, settlementTime string, settlementPrice float64, outcome string, won bool, payout float64, pnl float64) error {
	if id <= 0 || settlementPrice <= 0 || (outcome != "UP" && outcome != "DOWN") {
		return fmt.Errorf("invalid paper settlement")
	}
	wonInt := 0
	if won {
		wonInt = 1
	}
	res, err := d.db.Exec(`
		UPDATE paper_trades SET status='SETTLED', settlement_time=?, settlement_price=?, outcome=?, won=?, payout=?, pnl=?
		WHERE id=? AND status='OPEN'`, settlementTime, settlementPrice, outcome, wonInt, payout, pnl, id)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return fmt.Errorf("paper trade %d was not open", id)
	}
	return nil
}

func (d *Database) GetOpenPaperTrades() ([]PaperTrade, error) {
	return d.queryPaperTrades(`WHERE status='OPEN' ORDER BY id ASC`, nil)
}

func (d *Database) GetPaperTrades(limit int) ([]PaperTrade, error) {
	if limit <= 0 {
		return []PaperTrade{}, nil
	}
	if limit > 1000 {
		limit = 1000
	}
	return d.queryPaperTrades(`ORDER BY id DESC LIMIT ?`, []interface{}{limit})
}

func (d *Database) queryPaperTrades(suffix string, args []interface{}) ([]PaperTrade, error) {
	query := `SELECT id, market_slug, question, side, entry_time, market_end_time, entry_confidence,
		entry_final_score, entry_probability, entry_price, stake, shares, price_to_beat,
		entry_reference_price, status, settlement_time, settlement_price, outcome, won, payout, pnl, source
		FROM paper_trades ` + suffix
	rows, err := d.db.Query(query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	trades := make([]PaperTrade, 0)
	for rows.Next() {
		var t PaperTrade
		var won int
		if err := rows.Scan(&t.ID, &t.MarketSlug, &t.Question, &t.Side, &t.EntryTime, &t.MarketEndTime,
			&t.EntryConfidence, &t.EntryFinalScore, &t.EntryProbability, &t.EntryPrice, &t.Stake,
			&t.Shares, &t.PriceToBeat, &t.EntryReferencePrice, &t.Status, &t.SettlementTime,
			&t.SettlementPrice, &t.Outcome, &won, &t.Payout, &t.PnL, &t.Source); err != nil {
			return nil, err
		}
		t.Won = won == 1
		trades = append(trades, t)
	}
	return trades, rows.Err()
}

func (d *Database) GetPaperStats(initialBalance float64) (PaperStats, error) {
	stats := PaperStats{InitialBalance: initialBalance}
	var realizedPnL, openStake float64
	var total, settled, open, wins int
	var avgEntryProbability, brierScore, expectedWins float64
	err := d.db.QueryRow(`SELECT
		COALESCE(SUM(CASE WHEN status='SETTLED' THEN pnl ELSE 0 END), 0),
		COALESCE(SUM(CASE WHEN status='OPEN' THEN stake ELSE 0 END), 0),
		COUNT(*),
		COALESCE(SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END), 0),
		COALESCE(SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END), 0),
		COALESCE(SUM(CASE WHEN status='SETTLED' AND won=1 THEN 1 ELSE 0 END), 0),
		COALESCE(AVG(CASE WHEN status='SETTLED' THEN entry_probability END), 0),
		COALESCE(AVG(CASE WHEN status='SETTLED' THEN (entry_probability-won)*(entry_probability-won) END), 0),
		COALESCE(SUM(CASE WHEN status='SETTLED' THEN entry_probability ELSE 0 END), 0)
		FROM paper_trades`).Scan(&realizedPnL, &openStake, &total, &settled, &open, &wins, &avgEntryProbability, &brierScore, &expectedWins)
	if err != nil {
		return stats, err
	}
	stats.RealizedPnL = realizedPnL
	stats.OpenStake = openStake
	stats.TotalTrades = total
	stats.SettledTrades = settled
	stats.OpenTrades = open
	stats.Wins = wins
	stats.Losses = settled - wins
	stats.CashBalance = initialBalance + realizedPnL - openStake
	stats.Equity = initialBalance + realizedPnL
	stats.CalibrationN = settled
	stats.AverageEntryProbability = avgEntryProbability
	stats.BrierScore = brierScore
	stats.ExpectedWins = expectedWins
	if settled > 0 {
		stats.WinRate = float64(wins) * 100 / float64(settled)
		stats.ActualWinProbability = float64(wins) / float64(settled)
		stats.CalibrationGap = stats.ActualWinProbability - avgEntryProbability
	}
	return stats, nil
}
