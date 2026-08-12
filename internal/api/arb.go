package api

import (
	"net/http"

	"pm-edge/internal/dual40"
)

func (s *Server) handleArbLive(w http.ResponseWriter, r *http.Request) {
	row, err := s.db.GetLatestArbSnapshot(normalizeTF(r))
	if err != nil {
		writeJSON(w, nil, err)
		return
	}
	if row == nil {
		writeJSON(w, map[string]string{"status": "waiting_for_arb_data", "timeframe": normalizeTF(r)}, nil)
		return
	}
	writeJSON(w, row, nil)
}

func (s *Server) handleArbHistory(w http.ResponseWriter, r *http.Request) {
	rows, err := s.db.GetArbSnapshotsByTimeframe(parseLimit(r, 50, 1000), normalizeTF(r))
	writeJSON(w, rows, err)
}

func (s *Server) handleArbStats(w http.ResponseWriter, r *http.Request) {
	stats, err := s.db.GetArbStatsByTimeframe(normalizeTF(r))
	writeJSON(w, stats, err)
}

func (s *Server) handleArbPaperCycles(w http.ResponseWriter, r *http.Request) {
	if r.URL.Query().Get("strategy") == "dual40" {
		rows, err := s.db.GetDual40TrialsByTimeframe(parseLimit(r, 50, 2000), normalizeTF(r))
		writeJSON(w, rows, err)
		return
	}
	rows, err := s.db.GetArbPaperCyclesByTimeframe(parseLimit(r, 50, 1000), normalizeTF(r))
	writeJSON(w, rows, err)
}

func (s *Server) handleArbPaperStats(w http.ResponseWriter, r *http.Request) {
	if r.URL.Query().Get("strategy") == "dual40" {
		stats, err := s.db.GetDual40StatsByTimeframe(normalizeTF(r))
		writeJSON(w, stats, err)
		return
	}
	stats, err := s.db.GetArbPaperStatsByTimeframe(s.paperInitialBalance, normalizeTF(r))
	writeJSON(w, stats, err)
}

// handleArbPaperAnalysis: dual40 ISTATISTIKSEL KANIT — net EV ± SE, t-stat,
// P(second|first) first-fill feature bucket'larinda. GET
// /api/arb/paper/analysis?strategy=dual40&tf=5m
func (s *Server) handleArbPaperAnalysis(w http.ResponseWriter, r *http.Request) {
	if r.URL.Query().Get("strategy") != "dual40" {
		writeJSON(w, map[string]string{"error": "analysis yalnizca strategy=dual40 icin"}, nil)
		return
	}
	trials, err := s.db.GetDual40TrialsByTimeframe(parseLimit(r, 5000, 100000), normalizeTF(r))
	if err != nil {
		writeJSON(w, nil, err)
		return
	}
	writeJSON(w, dual40.AnalyzeTrials(trials), nil)
}
