package storage

import (
	"database/sql"
	"encoding/json"
	"fmt"

	"pm-edge/internal/arb"
)

type ArbStats struct {
	Timeframe      string  `json:"timeframe"`
	TotalSnapshots int     `json:"totalSnapshots"`
	Candidates     int     `json:"candidates"`
	CandidateRate  float64 `json:"candidateRate"`
	AverageNetEdge float64 `json:"averageNetEdge"`
	BestNetEdge    float64 `json:"bestNetEdge"`
	UpFirst        int     `json:"upFirst"`
	DownFirst      int     `json:"downFirst"`
	LastStatus     string  `json:"lastStatus"`
	LastReason     string  `json:"lastReason"`
	LastNetEdge    float64 `json:"lastNetEdge"`
}

func (d *Database) EnsureArbSchema() error {
	if d == nil || d.db == nil {
		return fmt.Errorf("database unavailable")
	}
	_, err := d.db.Exec(`
        CREATE TABLE IF NOT EXISTS arb_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            market_slug TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            net_edge REAL NOT NULL,
            target_edge REAL NOT NULL,
            first_leg TEXT NOT NULL,
            order_size REAL NOT NULL,
            pair_edge_pass INTEGER NOT NULL,
            ptb_ready INTEGER NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_arb_tf_time ON arb_snapshots(timeframe, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_arb_market ON arb_snapshots(market_slug, timestamp DESC);

        CREATE TABLE IF NOT EXISTS arb_paper_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timeframe TEXT NOT NULL,
            market_slug TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            first_leg TEXT NOT NULL DEFAULT '',
            preferred_first_leg TEXT NOT NULL DEFAULT '',
            preferred_first_matched INTEGER NOT NULL DEFAULT 0,
            locked_pnl REAL NOT NULL DEFAULT 0,
            paper_pnl REAL NOT NULL DEFAULT 0,
            deployed_cost REAL NOT NULL DEFAULT 0,
            completion_ms INTEGER NOT NULL DEFAULT 0,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_arb_paper_tf_id ON arb_paper_cycles(timeframe, id DESC);
        CREATE INDEX IF NOT EXISTS idx_arb_paper_open ON arb_paper_cycles(timeframe, status);
    `)
	return err
}

func (d *Database) InsertArbSnapshot(s *arb.Snapshot) error {
	if s == nil {
		return fmt.Errorf("nil arb snapshot")
	}
	raw, err := json.Marshal(s)
	if err != nil {
		return err
	}
	_, err = d.db.Exec(`INSERT INTO arb_snapshots
        (timestamp,timeframe,market_slug,status,reason,net_edge,target_edge,first_leg,order_size,pair_edge_pass,ptb_ready,payload)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`,
		s.Timestamp, NormalizeTimeframe(s.Timeframe), s.MarketSlug, s.Status, s.Reason, s.NetEdge, s.TargetEdge, s.FirstLeg, s.OrderSize, arbBoolInt(s.PairEdgePass), arbBoolInt(s.PTBReady), string(raw))
	return err
}

func (d *Database) GetLatestArbSnapshot(tf string) (*arb.Snapshot, error) {
	var raw string
	err := d.db.QueryRow(`SELECT payload FROM arb_snapshots WHERE timeframe=? ORDER BY id DESC LIMIT 1`, NormalizeTimeframe(tf)).Scan(&raw)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out arb.Snapshot
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		return nil, err
	}
	return &out, nil
}

func (d *Database) GetArbSnapshotsByTimeframe(limit int, tf string) ([]arb.Snapshot, error) {
	if limit < 1 {
		limit = 50
	}
	if limit > 1000 {
		limit = 1000
	}
	rows, err := d.db.Query(`SELECT payload FROM arb_snapshots WHERE timeframe=? ORDER BY id DESC LIMIT ?`, NormalizeTimeframe(tf), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]arb.Snapshot, 0, limit)
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var s arb.Snapshot
		if err := json.Unmarshal([]byte(raw), &s); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

func (d *Database) GetArbStatsByTimeframe(tf string) (ArbStats, error) {
	tf = NormalizeTimeframe(tf)
	out := ArbStats{Timeframe: tf}
	err := d.db.QueryRow(`SELECT COUNT(*), COALESCE(SUM(CASE WHEN status IN ('CANDIDATE','PAPER_CANDIDATE') THEN 1 ELSE 0 END),0),
        COALESCE(AVG(CASE WHEN status IN ('CANDIDATE','PAPER_CANDIDATE') THEN net_edge END),0), COALESCE(MAX(net_edge),0),
        COALESCE(SUM(CASE WHEN first_leg='UP' THEN 1 ELSE 0 END),0), COALESCE(SUM(CASE WHEN first_leg='DOWN' THEN 1 ELSE 0 END),0)
        FROM arb_snapshots WHERE timeframe=?`, tf).Scan(&out.TotalSnapshots, &out.Candidates, &out.AverageNetEdge, &out.BestNetEdge, &out.UpFirst, &out.DownFirst)
	if err != nil {
		return out, err
	}
	if out.TotalSnapshots > 0 {
		out.CandidateRate = float64(out.Candidates) / float64(out.TotalSnapshots)
	}
	var status, reason sql.NullString
	var edge sql.NullFloat64
	err = d.db.QueryRow(`SELECT status,reason,net_edge FROM arb_snapshots WHERE timeframe=? ORDER BY id DESC LIMIT 1`, tf).Scan(&status, &reason, &edge)
	if err != nil && err != sql.ErrNoRows {
		return out, err
	}
	if status.Valid {
		out.LastStatus = status.String
	}
	if reason.Valid {
		out.LastReason = reason.String
	}
	if edge.Valid {
		out.LastNetEdge = edge.Float64
	}
	return out, nil
}

func arbBoolInt(v bool) int {
	if v {
		return 1
	}
	return 0
}

type ArbPaperStats struct {
	Timeframe               string  `json:"timeframe"`
	InitialBalance          float64 `json:"initialBalance"`
	CashBalance             float64 `json:"cashBalance"`
	NetPaperPnL             float64 `json:"netPaperPnl"`
	LockedPnL               float64 `json:"lockedPnl"`
	StrandedPnL             float64 `json:"strandedPnl"`
	DeployedCost            float64 `json:"deployedCost"`
	ReturnOnDeployedPct     float64 `json:"returnOnDeployedPct"`
	TotalCycles             int     `json:"totalCycles"`
	OpenCycles              int     `json:"openCycles"`
	CompletedCycles         int     `json:"completedCycles"`
	ExpiredNoFill           int     `json:"expiredNoFill"`
	StrandedTimeout         int     `json:"strandedTimeout"`
	FirstLegFilledCycles    int     `json:"firstLegFilledCycles"`
	PairCompletionRate      float64 `json:"pairCompletionRate"`
	PreferredFirstMatches   int     `json:"preferredFirstMatches"`
	PreferredFirstMatchRate float64 `json:"preferredFirstMatchRate"`
	AverageCompletionMs     float64 `json:"averageCompletionMs"`
	AverageLockedProfit     float64 `json:"averageLockedProfit"`
	InvalidDataGap          int     `json:"invalidDataGap"`
}

func (d *Database) InsertArbPaperCycle(c *arb.PaperCycle) error {
	if c == nil {
		return fmt.Errorf("nil arb paper cycle")
	}
	raw, err := json.Marshal(c)
	if err != nil {
		return err
	}
	res, err := d.db.Exec(`INSERT INTO arb_paper_cycles
        (timeframe,market_slug,status,created_at,updated_at,first_leg,preferred_first_leg,preferred_first_matched,locked_pnl,paper_pnl,deployed_cost,completion_ms,payload)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`, NormalizeTimeframe(c.Timeframe), c.MarketSlug, c.Status, c.CreatedAt, c.UpdatedAt, c.ActualFirstLeg, c.PreferredFirstLeg, arbBoolInt(c.PreferredFirstMatched), c.LockedPnL, c.PaperPnL, c.DeployedCost, c.CompletionMs, string(raw))
	if err != nil {
		return err
	}
	id, err := res.LastInsertId()
	if err != nil {
		return err
	}
	c.ID = id
	raw, _ = json.Marshal(c)
	_, err = d.db.Exec(`UPDATE arb_paper_cycles SET payload=? WHERE id=?`, string(raw), id)
	return err
}

func (d *Database) UpdateArbPaperCycle(c *arb.PaperCycle) error {
	if c == nil || c.ID <= 0 {
		return fmt.Errorf("invalid arb paper cycle")
	}
	raw, err := json.Marshal(c)
	if err != nil {
		return err
	}
	_, err = d.db.Exec(`UPDATE arb_paper_cycles SET status=?,updated_at=?,first_leg=?,preferred_first_leg=?,preferred_first_matched=?,locked_pnl=?,paper_pnl=?,deployed_cost=?,completion_ms=?,payload=? WHERE id=?`,
		c.Status, c.UpdatedAt, c.ActualFirstLeg, c.PreferredFirstLeg, arbBoolInt(c.PreferredFirstMatched), c.LockedPnL, c.PaperPnL, c.DeployedCost, c.CompletionMs, string(raw), c.ID)
	return err
}

func (d *Database) GetOpenArbPaperCycle(tf string) (*arb.PaperCycle, error) {
	var raw string
	err := d.db.QueryRow(`SELECT payload FROM arb_paper_cycles WHERE timeframe=? AND status IN ('RESTING_FIRST','FIRST_PARTIAL','COMPLETING','COMPLETION_PARTIAL','RESTING_PAIR','ONE_LEG_FILLED') ORDER BY id DESC LIMIT 1`, NormalizeTimeframe(tf)).Scan(&raw)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var c arb.PaperCycle
	if err := json.Unmarshal([]byte(raw), &c); err != nil {
		return nil, err
	}
	return &c, nil
}

func (d *Database) GetArbPaperCyclesByTimeframe(limit int, tf string) ([]arb.PaperCycle, error) {
	if limit < 1 {
		limit = 50
	}
	if limit > 1000 {
		limit = 1000
	}
	rows, err := d.db.Query(`SELECT payload FROM arb_paper_cycles WHERE timeframe=? ORDER BY id DESC LIMIT ?`, NormalizeTimeframe(tf), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]arb.PaperCycle, 0, limit)
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var c arb.PaperCycle
		if err := json.Unmarshal([]byte(raw), &c); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

func (d *Database) GetArbPaperStatsByTimeframe(initial float64, tf string) (ArbPaperStats, error) {
	tf = NormalizeTimeframe(tf)
	if initial <= 0 {
		initial = 1000
	}
	out := ArbPaperStats{Timeframe: tf, InitialBalance: initial}
	err := d.db.QueryRow(`SELECT
        COUNT(*),
        COALESCE(SUM(CASE WHEN status IN ('RESTING_FIRST','FIRST_PARTIAL','COMPLETING','COMPLETION_PARTIAL','RESTING_PAIR','ONE_LEG_FILLED') THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status=? THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status=? THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status=? THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status=? THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN first_leg<>'' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN preferred_first_matched=1 THEN 1 ELSE 0 END),0),
        COALESCE(SUM(locked_pnl),0), COALESCE(SUM(paper_pnl),0), COALESCE(SUM(deployed_cost),0),
        COALESCE(AVG(CASE WHEN status=? THEN completion_ms END),0),
        COALESCE(AVG(CASE WHEN status=? THEN locked_pnl END),0),
        COALESCE(SUM(CASE WHEN status=? THEN paper_pnl ELSE 0 END),0)
        FROM arb_paper_cycles WHERE timeframe=?`,
		arb.PaperStatusCompleted, arb.PaperStatusExpiredNoFill, arb.PaperStatusStrandedTimeout, arb.PaperStatusDataGapInvalid,
		arb.PaperStatusCompleted, arb.PaperStatusCompleted, arb.PaperStatusStrandedTimeout, tf).Scan(
		&out.TotalCycles, &out.OpenCycles, &out.CompletedCycles, &out.ExpiredNoFill, &out.StrandedTimeout, &out.InvalidDataGap,
		&out.FirstLegFilledCycles, &out.PreferredFirstMatches, &out.LockedPnL, &out.NetPaperPnL, &out.DeployedCost,
		&out.AverageCompletionMs, &out.AverageLockedProfit, &out.StrandedPnL)
	if err != nil {
		return out, err
	}
	out.CashBalance = initial + out.NetPaperPnL
	resolvedAfterFirst := out.CompletedCycles + out.StrandedTimeout
	if resolvedAfterFirst > 0 {
		out.PairCompletionRate = float64(out.CompletedCycles) / float64(resolvedAfterFirst)
	}
	if out.FirstLegFilledCycles > 0 {
		out.PreferredFirstMatchRate = float64(out.PreferredFirstMatches) / float64(out.FirstLegFilledCycles)
	}
	if out.DeployedCost > 0 {
		out.ReturnOnDeployedPct = out.NetPaperPnL / out.DeployedCost * 100
	}
	return out, nil
}
