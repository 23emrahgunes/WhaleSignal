package storage

import (
	"database/sql"
	"fmt"
)

type PaperInverseTrade struct {
	ID             int64   `json:"id"`
	PaperTradeID   int64   `json:"paperTradeId"`
	MarketSlug     string  `json:"marketSlug"`
	OriginalSide   string  `json:"originalSide"`
	Side           string  `json:"side"`
	EntryTime      string  `json:"entryTime"`
	EntryPrice     float64 `json:"entryPrice"`
	Shares         float64 `json:"shares"`
	Notional       float64 `json:"notional"`
	Fee            float64 `json:"fee"`
	TotalCost      float64 `json:"totalCost"`
	Status         string  `json:"status"`
	SettlementTime string  `json:"settlementTime"`
	Outcome        string  `json:"outcome"`
	Won            bool    `json:"won"`
	Payout         float64 `json:"payout"`
	PnL            float64 `json:"pnl"`
}

type PaperInverseStats struct {
	TotalTrades  int     `json:"totalTrades"`
	SettledTrades int    `json:"settledTrades"`
	OpenTrades   int     `json:"openTrades"`
	Wins         int     `json:"wins"`
	Losses       int     `json:"losses"`
	WinRate      float64 `json:"winRate"`
	RealizedPnL  float64 `json:"realizedPnl"`
	OpenStake    float64 `json:"openStake"`
}

func (d *Database) EnsurePaperInverseSchema() error {
	_, err := d.db.Exec(`
	CREATE TABLE IF NOT EXISTS paper_inverse_trades (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		paper_trade_id INTEGER NOT NULL UNIQUE,
		market_slug TEXT NOT NULL UNIQUE,
		original_side TEXT NOT NULL,
		side TEXT NOT NULL,
		entry_time TEXT NOT NULL,
		entry_price REAL NOT NULL,
		shares REAL NOT NULL,
		notional REAL NOT NULL,
		fee REAL NOT NULL,
		total_cost REAL NOT NULL,
		status TEXT NOT NULL DEFAULT 'OPEN',
		settlement_time TEXT NOT NULL DEFAULT '',
		outcome TEXT NOT NULL DEFAULT '',
		won INTEGER NOT NULL DEFAULT 0,
		payout REAL NOT NULL DEFAULT 0,
		pnl REAL NOT NULL DEFAULT 0,
		FOREIGN KEY(paper_trade_id) REFERENCES paper_trades(id)
	);
	CREATE INDEX IF NOT EXISTS idx_paper_inverse_status ON paper_inverse_trades(status);
	CREATE INDEX IF NOT EXISTS idx_paper_inverse_time ON paper_inverse_trades(entry_time);
	`)
	return err
}

func (d *Database) CreatePaperInverseTrade(t *PaperInverseTrade) (bool, error) {
	if t == nil || t.PaperTradeID <= 0 || !IsSupportedBTCMarketSlug(t.MarketSlug) {
		return false, fmt.Errorf("invalid inverse paper trade")
	}
	if (t.Side != "UP" && t.Side != "DOWN") || (t.OriginalSide != "UP" && t.OriginalSide != "DOWN") || t.Side == t.OriginalSide {
		return false, fmt.Errorf("invalid inverse paper side")
	}
	if t.EntryPrice <= 0 || t.EntryPrice >= 1 || t.Shares <= 0 || t.TotalCost <= 0 {
		return false, fmt.Errorf("invalid inverse paper economics")
	}
	res, err := d.db.Exec(`INSERT OR IGNORE INTO paper_inverse_trades (
		paper_trade_id, market_slug, original_side, side, entry_time, entry_price,
		shares, notional, fee, total_cost, status
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')`,
		t.PaperTradeID, t.MarketSlug, t.OriginalSide, t.Side, t.EntryTime, t.EntryPrice,
		t.Shares, t.Notional, t.Fee, t.TotalCost)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n == 1, err
}

func (d *Database) GetPaperInverseByTradeID(tradeID int64) (*PaperInverseTrade, error) {
	rows, err := d.queryPaperInverseTrades(`WHERE paper_trade_id=? LIMIT 1`, []interface{}{tradeID})
	if err != nil || len(rows) == 0 {
		return nil, err
	}
	return &rows[0], nil
}

func (d *Database) SettlePaperInverseTrade(tradeID int64, settlementTime, outcome string, payout, pnl float64) error {
	if tradeID <= 0 || (outcome != "UP" && outcome != "DOWN") {
		return fmt.Errorf("invalid inverse paper settlement")
	}
	inv, err := d.GetPaperInverseByTradeID(tradeID)
	if err != nil {
		return err
	}
	if inv == nil {
		return nil
	}
	won := 0
	if outcome == inv.Side {
		won = 1
	}
	res, err := d.db.Exec(`UPDATE paper_inverse_trades SET status='SETTLED', settlement_time=?, outcome=?, won=?, payout=?, pnl=? WHERE paper_trade_id=? AND status='OPEN'`, settlementTime, outcome, won, payout, pnl, tradeID)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return fmt.Errorf("inverse paper trade for trade %d was not open", tradeID)
	}
	return nil
}

func (d *Database) GetPaperInverseTradesByTimeframe(limit int, tf string) ([]PaperInverseTrade, error) {
	if limit <= 0 {
		return []PaperInverseTrade{}, nil
	}
	if limit > 1000 {
		limit = 1000
	}
	return d.queryPaperInverseTrades(`WHERE market_slug LIKE ? ORDER BY id DESC LIMIT ?`, []interface{}{timeframeLike(tf), limit})
}

func (d *Database) queryPaperInverseTrades(suffix string, args []interface{}) ([]PaperInverseTrade, error) {
	query := `SELECT id, paper_trade_id, market_slug, original_side, side, entry_time, entry_price,
		shares, notional, fee, total_cost, status, settlement_time, outcome, won, payout, pnl
		FROM paper_inverse_trades ` + suffix
	rows, err := d.db.Query(query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]PaperInverseTrade, 0)
	for rows.Next() {
		var t PaperInverseTrade
		var won int
		if err := rows.Scan(&t.ID, &t.PaperTradeID, &t.MarketSlug, &t.OriginalSide, &t.Side,
			&t.EntryTime, &t.EntryPrice, &t.Shares, &t.Notional, &t.Fee, &t.TotalCost,
			&t.Status, &t.SettlementTime, &t.Outcome, &won, &t.Payout, &t.PnL); err != nil {
			return nil, err
		}
		t.Won = won == 1
		out = append(out, t)
	}
	return out, rows.Err()
}

func (d *Database) GetPaperInverseStatsByTimeframe(tf string) (PaperInverseStats, error) {
	var s PaperInverseStats
	var settled, open, wins int
	err := d.db.QueryRow(`SELECT
		COUNT(*),
		COALESCE(SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN status='SETTLED' AND won=1 THEN 1 ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN status='SETTLED' THEN pnl ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN status='OPEN' THEN total_cost ELSE 0 END),0)
		FROM paper_inverse_trades WHERE market_slug LIKE ?`, timeframeLike(tf)).Scan(
		&s.TotalTrades, &settled, &open, &wins, &s.RealizedPnL, &s.OpenStake)
	if err == sql.ErrNoRows {
		return s, nil
	}
	if err != nil {
		return s, err
	}
	s.SettledTrades = settled
	s.OpenTrades = open
	s.Wins = wins
	s.Losses = settled - wins
	if settled > 0 {
		s.WinRate = float64(wins) * 100 / float64(settled)
	}
	return s, nil
}
