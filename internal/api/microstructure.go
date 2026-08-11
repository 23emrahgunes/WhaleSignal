package api

import (
	"encoding/json"
	"net/http"
)

func (s *Server) handleMicrostructure(w http.ResponseWriter, r *http.Request) {
	tf := normalizeTF(r)
	s.mu.RLock()
	res := s.currentResults[tf]
	s.mu.RUnlock()
	w.Header().Set("Content-Type", "application/json")
	if res == nil {
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "waiting_for_data"})
		return
	}
	_ = json.NewEncoder(w).Encode(map[string]interface{}{
		"timeframe":               tf,
		"timestamp":               res.Timestamp,
		"marketSlug":              res.Slug,
		"priceToBeat":             res.PriceToBeat,
		"binancePrice":            res.BinancePrice,
		"deepMicrostructure":      res.DeepMicrostructure,
		"deepBookScore":           res.DeepBookScore,
		"tradeFlowScore":          res.TradeFlowScore,
		"wallDynamicsScore":       res.WallDynamicsScore,
		"ptbBarrierScore":         res.PTBBarrierScore,
		"microstructureScore":     res.MicrostructureScore,
		"shadowModelBScore":       res.ShadowModelBScore,
		"shadowDecision":          res.ShadowDecision,
		"shadowConfidence":        res.ShadowConfidence,
		"productionModelDecision": res.Decision,
		"productionModelScore":    res.FinalScore,
	})
}

func (s *Server) handleMicrostructureHistory(w http.ResponseWriter, r *http.Request) {
	limit := parseLimit(r, 100, 10000)
	rows, err := s.db.GetMicrostructureSnapshots(limit, normalizeTF(r))
	writeJSON(w, rows, err)
}
