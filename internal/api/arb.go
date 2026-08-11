package api

import "net/http"

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
