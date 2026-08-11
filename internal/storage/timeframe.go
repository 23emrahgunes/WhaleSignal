package storage

import (
	"database/sql"
	"fmt"
	"math"
	"strings"

	"pm-edge/internal/engine"
)

type TimeframeStats struct {
	PaperStats
	Timeframe        string  `json:"timeframe"`
	SettledStake     float64 `json:"settledStake"`
	ReturnOnStakePct float64 `json:"returnOnStakePct"`
	AverageReturnPct float64 `json:"averageReturnPct"`
	ReturnStdDevPct  float64 `json:"returnStdDevPct"`
	ReturnSEPct      float64 `json:"returnSePct"`
}

func NormalizeTimeframe(tf string) string {
	switch strings.ToLower(strings.TrimSpace(tf)) {
	case "15m", "15min", "15minute", "15minutes":
		return "15m"
	default:
		return "5m"
	}
}

func TimeframeFromMarketSlug(slug string) string {
	slug = strings.ToLower(strings.TrimSpace(slug))
	if strings.HasPrefix(slug, "btc-updown-15m-") {
		return "15m"
	}
	if strings.HasPrefix(slug, "btc-updown-5m-") {
		return "5m"
	}
	return ""
}

func IsSupportedBTCMarketSlug(slug string) bool { return TimeframeFromMarketSlug(slug) != "" }

func timeframeLike(tf string) string { return "btc-updown-" + NormalizeTimeframe(tf) + "-%" }

func (d *Database) GetHistoryByTimeframe(limit int, tf string) ([]engine.EvaluationResult, error) {
	if limit <= 0 {
		return []engine.EvaluationResult{}, nil
	}
	if limit > 10000 {
		limit = 10000
	}
	// Reuse the canonical scanner and filter in-memory. The dashboard requests a
	// small history window; over-fetching keeps migration risk low.
	overfetch := limit * 20
	if overfetch < 200 {
		overfetch = 200
	}
	if overfetch > 10000 {
		overfetch = 10000
	}
	all, err := d.GetHistory(overfetch)
	if err != nil {
		return nil, err
	}
	want := NormalizeTimeframe(tf)
	out := make([]engine.EvaluationResult, 0, limit)
	for _, row := range all {
		if TimeframeFromMarketSlug(row.Slug) != want {
			continue
		}
		out = append(out, row)
		if len(out) >= limit {
			break
		}
	}
	return out, nil
}

func (d *Database) GetOpenPaperTradesByTimeframe(tf string) ([]PaperTrade, error) {
	return d.queryPaperTrades(`WHERE status='OPEN' AND market_slug LIKE ? ORDER BY id ASC`, []interface{}{timeframeLike(tf)})
}

func (d *Database) GetPaperTradesByTimeframe(limit int, tf string) ([]PaperTrade, error) {
	if limit <= 0 {
		return []PaperTrade{}, nil
	}
	if limit > 1000 {
		limit = 1000
	}
	return d.queryPaperTrades(`WHERE market_slug LIKE ? ORDER BY id DESC LIMIT ?`, []interface{}{timeframeLike(tf), limit})
}

func (d *Database) PaperTradeExists(slug string) (bool, error) {
	var n int
	if err := d.db.QueryRow(`SELECT COUNT(*) FROM paper_trades WHERE market_slug=?`, slug).Scan(&n); err != nil {
		return false, err
	}
	return n > 0, nil
}

func (d *Database) GetPaperStatsByTimeframe(initialBalance float64, tf string) (PaperStats, error) {
	s, err := d.GetTimeframeStats(initialBalance, tf)
	return s.PaperStats, err
}

func (d *Database) GetTimeframeStats(initialBalance float64, tf string) (TimeframeStats, error) {
	tf = NormalizeTimeframe(tf)
	out := TimeframeStats{PaperStats: PaperStats{InitialBalance: initialBalance}, Timeframe: tf}
	var realizedPnL, openStake, avgEntryProbability, brierScore, expectedWins float64
	var settledStake, avgReturn, avgReturnSq float64
	var total, settled, open, wins int
	err := d.db.QueryRow(`SELECT
		COALESCE(SUM(CASE WHEN status='SETTLED' THEN pnl ELSE 0 END), 0),
		COALESCE(SUM(CASE WHEN status='OPEN' THEN stake ELSE 0 END), 0),
		COUNT(*),
		COALESCE(SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END), 0),
		COALESCE(SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END), 0),
		COALESCE(SUM(CASE WHEN status='SETTLED' AND won=1 THEN 1 ELSE 0 END), 0),
		COALESCE(AVG(CASE WHEN status='SETTLED' THEN entry_probability END), 0),
		COALESCE(AVG(CASE WHEN status='SETTLED' THEN (entry_probability-won)*(entry_probability-won) END), 0),
		COALESCE(SUM(CASE WHEN status='SETTLED' THEN entry_probability ELSE 0 END), 0),
		COALESCE(SUM(CASE WHEN status='SETTLED' THEN stake ELSE 0 END), 0),
		COALESCE(AVG(CASE WHEN status='SETTLED' AND stake>0 THEN pnl/stake END), 0),
		COALESCE(AVG(CASE WHEN status='SETTLED' AND stake>0 THEN (pnl/stake)*(pnl/stake) END), 0)
		FROM paper_trades WHERE market_slug LIKE ?`, timeframeLike(tf)).Scan(
		&realizedPnL, &openStake, &total, &settled, &open, &wins,
		&avgEntryProbability, &brierScore, &expectedWins,
		&settledStake, &avgReturn, &avgReturnSq)
	if err != nil && err != sql.ErrNoRows {
		return out, err
	}
	out.RealizedPnL = realizedPnL
	out.OpenStake = openStake
	out.TotalTrades = total
	out.SettledTrades = settled
	out.OpenTrades = open
	out.Wins = wins
	out.Losses = settled - wins
	out.CashBalance = initialBalance + realizedPnL - openStake
	out.Equity = initialBalance + realizedPnL
	out.CalibrationN = settled
	out.AverageEntryProbability = avgEntryProbability
	out.BrierScore = brierScore
	out.ExpectedWins = expectedWins
	out.SettledStake = settledStake
	if settled > 0 {
		out.WinRate = float64(wins) * 100 / float64(settled)
		out.ActualWinProbability = float64(wins) / float64(settled)
		out.CalibrationGap = out.ActualWinProbability - avgEntryProbability
		out.AverageReturnPct = avgReturn * 100
		variance := math.Max(0, avgReturnSq-avgReturn*avgReturn)
		out.ReturnStdDevPct = math.Sqrt(variance) * 100
		out.ReturnSEPct = out.ReturnStdDevPct / math.Sqrt(float64(settled))
	}
	if settledStake > 0 {
		out.ReturnOnStakePct = realizedPnL / settledStake * 100
	}
	return out, nil
}

func (d *Database) GetPaperHedgesByTimeframe(limit int, tf string) ([]PaperHedge, error) {
	if limit <= 0 {
		return []PaperHedge{}, nil
	}
	if limit > 1000 {
		limit = 1000
	}
	return d.queryPaperHedges(`WHERE market_slug LIKE ? ORDER BY id DESC LIMIT ?`, []interface{}{timeframeLike(tf), limit})
}

func (d *Database) GetPaperHedgeStatsByTimeframe(tf string) (PaperHedgeStats, error) {
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
	FROM paper_hedges h JOIN paper_trades p ON p.id=h.paper_trade_id
	WHERE p.market_slug LIKE ?`, timeframeLike(tf)).Scan(
		&s.TotalHedges, &s.SettledHedges, &s.OpenHedges, &s.OriginalPnLOnHedged,
		&s.HedgeContribution, &s.CombinedPnL, &s.SavedLoss, &s.Regret,
		&s.AverageEdge, &s.AveragePersistence)
	if err == sql.ErrNoRows {
		return s, nil
	}
	return s, err
}

func ValidateSupportedSlug(slug string) error {
	if !IsSupportedBTCMarketSlug(slug) {
		return fmt.Errorf("unsupported BTC up/down market slug %q", slug)
	}
	return nil
}
