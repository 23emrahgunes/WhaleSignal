package storage

import (
	"database/sql"
	"encoding/json"
	"fmt"

	"pm-edge/internal/dual40"
)

type Dual40Stats struct {
	Timeframe       string  `json:"timeframe"`
	TotalTrials     int     `json:"totalTrials"`
	EligibleTrials  int     `json:"eligibleTrials"`
	SkippedTrials   int     `json:"skippedTrials"`
	OpenTrials      int     `json:"openTrials"`
	Completed       int     `json:"completed"`
	Hedged          int     `json:"hedged"`
	ExpiredNoFill   int     `json:"expiredNoFill"`
	PartialPair     int     `json:"partialPair"`
	DataGapInvalid  int     `json:"dataGapInvalid"`
	NetPaperPnL     float64 `json:"netPaperPnl"`
	AverageChop     float64 `json:"averageChop"`
	AverageHedge    float64 `json:"averageHedgePrice"`
	DualFillRate    float64 `json:"dualFillRate"`
	HedgeRate       float64 `json:"hedgeRate"`
	LastState       string  `json:"lastState"`
	LastReason      string  `json:"lastReason"`
	LastEntrySecond int     `json:"lastEntrySecond"`
}

func (d *Database) EnsureDual40Schema() error {
	if d == nil || d.db == nil {
		return fmt.Errorf("database unavailable")
	}
	_, err := d.db.Exec(`
        CREATE TABLE IF NOT EXISTS dual40_trials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timeframe TEXT NOT NULL,
            market_slug TEXT NOT NULL,
            entry_second INTEGER NOT NULL,
            state TEXT NOT NULL,
            eligible INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            chop_score REAL NOT NULL DEFAULT 0,
            paper_pnl REAL NOT NULL DEFAULT 0,
            hedge_price REAL NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL,
            UNIQUE(market_slug, entry_second)
        );
        CREATE INDEX IF NOT EXISTS idx_dual40_tf_id ON dual40_trials(timeframe, id DESC);
        CREATE INDEX IF NOT EXISTS idx_dual40_state ON dual40_trials(timeframe, state);
    `)
	return err
}

func (d *Database) InsertDual40Trial(t *dual40.Trial) error {
	if t == nil {
		return fmt.Errorf("nil dual40 trial")
	}
	raw, err := json.Marshal(t)
	if err != nil {
		return err
	}
	_, err = d.db.Exec(`INSERT INTO dual40_trials
        (timeframe,market_slug,entry_second,state,eligible,created_at,updated_at,chop_score,paper_pnl,hedge_price,reason,payload)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(market_slug,entry_second) DO NOTHING`,
		NormalizeTimeframe(t.Timeframe), t.MarketSlug, t.EntrySecond, t.State, dual40BoolInt(t.Eligible), t.CreatedAt, t.UpdatedAt,
		t.Metrics.ChopScore, t.PaperPnL, t.HedgeAvgPrice, t.Reason, string(raw))
	if err != nil {
		return err
	}
	if err := d.db.QueryRow(`SELECT id FROM dual40_trials WHERE market_slug=? AND entry_second=?`, t.MarketSlug, t.EntrySecond).Scan(&t.ID); err != nil {
		return err
	}
	raw, err = json.Marshal(t)
	if err != nil {
		return err
	}
	_, err = d.db.Exec(`UPDATE dual40_trials SET payload=? WHERE id=?`, string(raw), t.ID)
	return err
}

func (d *Database) UpdateDual40Trial(t *dual40.Trial) error {
	if t == nil || t.ID <= 0 {
		return fmt.Errorf("invalid dual40 trial")
	}
	raw, err := json.Marshal(t)
	if err != nil {
		return err
	}
	_, err = d.db.Exec(`UPDATE dual40_trials SET state=?,eligible=?,updated_at=?,chop_score=?,paper_pnl=?,hedge_price=?,reason=?,payload=? WHERE id=?`,
		t.State, dual40BoolInt(t.Eligible), t.UpdatedAt, t.Metrics.ChopScore, t.PaperPnL, t.HedgeAvgPrice, t.Reason, string(raw), t.ID)
	return err
}

func (d *Database) GetDual40TrialsByTimeframe(limit int, tf string) ([]dual40.Trial, error) {
	if limit < 1 {
		limit = 50
	}
	if limit > 2000 {
		limit = 2000
	}
	rows, err := d.db.Query(`SELECT payload FROM dual40_trials WHERE timeframe=? ORDER BY id DESC LIMIT ?`, NormalizeTimeframe(tf), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]dual40.Trial, 0, limit)
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var t dual40.Trial
		if err := json.Unmarshal([]byte(raw), &t); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

func (d *Database) GetOpenDual40TrialsByTimeframe(tf string) ([]dual40.Trial, error) {
	rows, err := d.db.Query(`SELECT payload FROM dual40_trials WHERE timeframe=? AND state IN (?,?) ORDER BY id`, NormalizeTimeframe(tf), dual40.StateResting, dual40.StateOneLeg)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []dual40.Trial
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var t dual40.Trial
		if err := json.Unmarshal([]byte(raw), &t); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

func (d *Database) GetDual40StatsByTimeframe(tf string) (Dual40Stats, error) {
	tf = NormalizeTimeframe(tf)
	out := Dual40Stats{Timeframe: tf}
	err := d.db.QueryRow(`SELECT
        COUNT(*),
        COALESCE(SUM(CASE WHEN eligible=1 THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN state=? THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN state IN (?,?) THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN state=? THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN state=? THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN state=? THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN state=? THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN state=? THEN 1 ELSE 0 END),0),
        COALESCE(SUM(paper_pnl),0),
        COALESCE(AVG(CASE WHEN eligible=1 THEN chop_score END),0),
        COALESCE(AVG(CASE WHEN state=? THEN hedge_price END),0)
        FROM dual40_trials WHERE timeframe=?`,
		dual40.StateSkipped,
		dual40.StateResting, dual40.StateOneLeg,
		dual40.StateCompleted,
		dual40.StateHedged,
		dual40.StateExpiredNoFill,
		dual40.StatePartialPair,
		dual40.StateDataGapInvalid,
		dual40.StateHedged,
		tf).Scan(
		&out.TotalTrials, &out.EligibleTrials, &out.SkippedTrials, &out.OpenTrials, &out.Completed, &out.Hedged,
		&out.ExpiredNoFill, &out.PartialPair, &out.DataGapInvalid, &out.NetPaperPnL, &out.AverageChop, &out.AverageHedge)
	if err != nil {
		return out, err
	}
	resolvedRisk := out.Completed + out.Hedged
	if resolvedRisk > 0 {
		out.DualFillRate = float64(out.Completed) / float64(resolvedRisk)
		out.HedgeRate = float64(out.Hedged) / float64(resolvedRisk)
	}
	var state, reason sql.NullString
	var entry sql.NullInt64
	err = d.db.QueryRow(`SELECT state,reason,entry_second FROM dual40_trials WHERE timeframe=? ORDER BY id DESC LIMIT 1`, tf).Scan(&state, &reason, &entry)
	if err != nil && err != sql.ErrNoRows {
		return out, err
	}
	if state.Valid {
		out.LastState = state.String
	}
	if reason.Valid {
		out.LastReason = reason.String
	}
	if entry.Valid {
		out.LastEntrySecond = int(entry.Int64)
	}
	return out, nil
}

func dual40BoolInt(v bool) int {
	if v {
		return 1
	}
	return 0
}
