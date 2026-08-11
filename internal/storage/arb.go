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
	err := d.db.QueryRow(`SELECT COUNT(*), COALESCE(SUM(CASE WHEN status='CANDIDATE' THEN 1 ELSE 0 END),0),
        COALESCE(AVG(CASE WHEN status='CANDIDATE' THEN net_edge END),0), COALESCE(MAX(net_edge),0),
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
