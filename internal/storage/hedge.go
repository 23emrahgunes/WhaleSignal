package storage

import (
	"database/sql"
	"fmt"
)

type PaperHedge struct {
	ID                  int64   `json:"id"`
	PaperTradeID        int64   `json:"paperTradeId"`
	MarketSlug          string  `json:"marketSlug"`
	OriginalSide        string  `json:"originalSide"`
	Side                string  `json:"side"`
	HedgeTime           string  `json:"hedgeTime"`
	EntryPrice          float64 `json:"entryPrice"`
	Shares              float64 `json:"shares"`
	Notional            float64 `json:"notional"`
	Fee                 float64 `json:"fee"`
	TotalCost           float64 `json:"totalCost"`
	ReverseProbability  float64 `json:"reverseProbability"`
	Edge                float64 `json:"edge"`
	Persistence         float64 `json:"persistence"`
	SmoothedScore       float64 `json:"smoothedScore"`
	PTBZ                float64 `json:"ptbZ"`
	LockedPnL           float64 `json:"lockedPnl"`
	ExpectedHoldPnL     float64 `json:"expectedHoldPnl"`
	ExpectedImprovement float64 `json:"expectedImprovement"`
	Status              string  `json:"status"`
	SettlementTime      string  `json:"settlementTime"`
	Outcome             string  `json:"outcome"`
	Won                 bool    `json:"won"`
	Payout              float64 `json:"payout"`
	PnL                 float64 `json:"pnl"`
	CombinedPnL         float64 `json:"combinedPnl"`
}

type PaperHedgeStats struct {
	TotalHedges         int     `json:"totalHedges"`
	SettledHedges       int     `json:"settledHedges"`
	OpenHedges          int     `json:"openHedges"`
	OriginalPnLOnHedged float64 `json:"originalPnlOnHedged"`
	HedgeContribution   float64 `json:"hedgeContribution"`
	CombinedPnL         float64 `json:"combinedPnl"`
	SavedLoss           float64 `json:"savedLoss"`
	Regret              float64 `json:"regret"`
	AverageEdge         float64 `json:"averageEdge"`
	AveragePersistence  float64 `json:"averagePersistence"`
}

func (d *Database) EnsurePaperHedgeSchema() error {
	_, err := d.db.Exec(`
	CREATE TABLE IF NOT EXISTS paper_hedges (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		paper_trade_id INTEGER NOT NULL UNIQUE,
		market_slug TEXT NOT NULL UNIQUE,
		original_side TEXT NOT NULL,
		side TEXT NOT NULL,
		hedge_time TEXT NOT NULL,
		entry_price REAL NOT NULL,
		shares REAL NOT NULL,
		notional REAL NOT NULL,
		fee REAL NOT NULL,
		total_cost REAL NOT NULL,
		reverse_probability REAL NOT NULL,
		edge REAL NOT NULL,
		persistence REAL NOT NULL,
		smoothed_score REAL NOT NULL,
		ptb_z REAL NOT NULL,
		locked_pnl REAL NOT NULL,
		expected_hold_pnl REAL NOT NULL,
		expected_improvement REAL NOT NULL,
		status TEXT NOT NULL DEFAULT 'OPEN',
		settlement_time TEXT NOT NULL DEFAULT '',
		outcome TEXT NOT NULL DEFAULT '',
		won INTEGER NOT NULL DEFAULT 0,
		payout REAL NOT NULL DEFAULT 0,
		pnl REAL NOT NULL DEFAULT 0,
		combined_pnl REAL NOT NULL DEFAULT 0,
		FOREIGN KEY(paper_trade_id) REFERENCES paper_trades(id)
	);
	CREATE INDEX IF NOT EXISTS idx_paper_hedges_status ON paper_hedges(status);
	CREATE INDEX IF NOT EXISTS idx_paper_hedges_time ON paper_hedges(hedge_time);
	`)
	return err
}

func (d *Database) GetOpenPaperTradeByMarket(slug string) (*PaperTrade, error) {
	trades, err := d.queryPaperTrades(`WHERE market_slug=? AND status='OPEN' LIMIT 1`, []interface{}{slug})
	if err != nil || len(trades) == 0 {
		return nil, err
	}
	return &trades[0], nil
}

func (d *Database) CreatePaperHedge(h *PaperHedge) (bool, error) {
	if h == nil || h.PaperTradeID <= 0 || !IsSupportedBTCMarketSlug(h.MarketSlug) {
		return false, fmt.Errorf("invalid paper hedge")
	}
	if h.Side != "UP" && h.Side != "DOWN" || h.OriginalSide == h.Side {
		return false, fmt.Errorf("invalid hedge side")
	}
	if h.EntryPrice <= 0 || h.EntryPrice >= 1 || h.Shares <= 0 || h.TotalCost <= 0 || h.Edge <= 0 {
		return false, fmt.Errorf("invalid hedge economics")
	}
	res, err := d.db.Exec(`INSERT OR IGNORE INTO paper_hedges (
		paper_trade_id, market_slug, original_side, side, hedge_time, entry_price,
		shares, notional, fee, total_cost, reverse_probability, edge, persistence,
		smoothed_score, ptb_z, locked_pnl, expected_hold_pnl, expected_improvement, status
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')`,
		h.PaperTradeID, h.MarketSlug, h.OriginalSide, h.Side, h.HedgeTime, h.EntryPrice,
		h.Shares, h.Notional, h.Fee, h.TotalCost, h.ReverseProbability, h.Edge,
		h.Persistence, h.SmoothedScore, h.PTBZ, h.LockedPnL, h.ExpectedHoldPnL, h.ExpectedImprovement)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n == 1, err
}

func (d *Database) GetPaperHedgeByTradeID(tradeID int64) (*PaperHedge, error) {
	rows, err := d.queryPaperHedges(`WHERE paper_trade_id=? LIMIT 1`, []interface{}{tradeID})
	if err != nil || len(rows) == 0 {
		return nil, err
	}
	return &rows[0], nil
}

func (d *Database) SettlePaperHedge(tradeID int64, settlementTime, outcome string, payout, pnl, combinedPnL float64) error {
	won := 0
	h, err := d.GetPaperHedgeByTradeID(tradeID)
	if err != nil {
		return err
	}
	if h == nil {
		return nil
	}
	if outcome == h.Side {
		won = 1
	}
	res, err := d.db.Exec(`UPDATE paper_hedges SET status='SETTLED', settlement_time=?, outcome=?, won=?, payout=?, pnl=?, combined_pnl=? WHERE paper_trade_id=? AND status='OPEN'`, settlementTime, outcome, won, payout, pnl, combinedPnL, tradeID)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return fmt.Errorf("paper hedge for trade %d was not open", tradeID)
	}
	return nil
}

func (d *Database) GetPaperHedges(limit int) ([]PaperHedge, error) {
	if limit <= 0 {
		return []PaperHedge{}, nil
	}
	if limit > 1000 {
		limit = 1000
	}
	return d.queryPaperHedges(`ORDER BY id DESC LIMIT ?`, []interface{}{limit})
}

func (d *Database) queryPaperHedges(suffix string, args []interface{}) ([]PaperHedge, error) {
	query := `SELECT id, paper_trade_id, market_slug, original_side, side, hedge_time, entry_price,
		shares, notional, fee, total_cost, reverse_probability, edge, persistence,
		smoothed_score, ptb_z, locked_pnl, expected_hold_pnl, expected_improvement,
		status, settlement_time, outcome, won, payout, pnl, combined_pnl
		FROM paper_hedges ` + suffix
	rows, err := d.db.Query(query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]PaperHedge, 0)
	for rows.Next() {
		var h PaperHedge
		var won int
		if err := rows.Scan(&h.ID, &h.PaperTradeID, &h.MarketSlug, &h.OriginalSide, &h.Side,
			&h.HedgeTime, &h.EntryPrice, &h.Shares, &h.Notional, &h.Fee, &h.TotalCost,
			&h.ReverseProbability, &h.Edge, &h.Persistence, &h.SmoothedScore, &h.PTBZ,
			&h.LockedPnL, &h.ExpectedHoldPnL, &h.ExpectedImprovement, &h.Status,
			&h.SettlementTime, &h.Outcome, &won, &h.Payout, &h.PnL, &h.CombinedPnL); err != nil {
			return nil, err
		}
		h.Won = won == 1
		out = append(out, h)
	}
	return out, rows.Err()
}

func (d *Database) GetPaperHedgeStats() (PaperHedgeStats, error) {
	var s PaperHedgeStats
	err := d.db.QueryRow(`SELECT
		COUNT(*),
		COALESCE(SUM(CASE WHEN h.status='SETTLED' THEN 1 ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN h.status='OPEN' THEN 1 ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN h.status='SETTLED' THEN p.pnl ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN h.status='SETTLED' THEN h.pnl ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN h.status='SETTLED' THEN h.combined_pnl ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN h.status='SETTLED' AND h.pnl>0 THEN h.pnl ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN h.status='SETTLED' AND h.pnl<0 THEN -h.pnl ELSE 0 END),0),
		COALESCE(AVG(h.edge),0),
		COALESCE(AVG(h.persistence),0)
	FROM paper_hedges h JOIN paper_trades p ON p.id=h.paper_trade_id`).Scan(
		&s.TotalHedges, &s.SettledHedges, &s.OpenHedges, &s.OriginalPnLOnHedged,
		&s.HedgeContribution, &s.CombinedPnL, &s.SavedLoss, &s.Regret,
		&s.AverageEdge, &s.AveragePersistence)
	if err == sql.ErrNoRows {
		return s, nil
	}
	return s, err
}
