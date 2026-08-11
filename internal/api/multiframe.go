package api

import (
	"encoding/json"
	"math"
	"net/http"
	"strings"

	"pm-edge/internal/engine"
	"pm-edge/internal/paper"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
)

type gateState struct {
	Entry paper.EntryGateSnapshot `json:"entry"`
	Hedge paper.HedgeGateSnapshot `json:"hedge"`
}

type comparisonPayload struct {
	FiveMinute     storage.TimeframeStats `json:"fiveMinute"`
	FifteenMinute storage.TimeframeStats `json:"fifteenMinute"`
	MinSettled     int                    `json:"minSettledForInference"`
	Status         string                 `json:"status"`
	Leader         string                 `json:"leader"`
	ReturnDiffPct  float64                `json:"returnDiffPct"`
	DiffSEPct      float64                `json:"diffSePct"`
	ZScore         float64                `json:"zScore"`
	Interpretation string                 `json:"interpretation"`
}

func normalizeTF(r *http.Request) string {
	tf := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("tf")))
	if tf == "15m" {
		return "15m"
	}
	return "5m"
}

func (s *Server) UpdateStateFor(tf string, res *engine.EvaluationResult, market *polymarket.Market) {
	tf = storage.NormalizeTimeframe(tf)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.currentResults == nil {
		s.currentResults = make(map[string]*engine.EvaluationResult)
	}
	if s.currentMarkets == nil {
		s.currentMarkets = make(map[string]*polymarket.Market)
	}
	s.currentResults[tf] = res
	s.currentMarkets[tf] = market
}

func (s *Server) UpdateGatesFor(tf string, entry paper.EntryGateSnapshot, hedge paper.HedgeGateSnapshot) {
	tf = storage.NormalizeTimeframe(tf)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.gates == nil {
		s.gates = make(map[string]gateState)
	}
	s.gates[tf] = gateState{Entry: entry, Hedge: hedge}
}

func (s *Server) handleGates(w http.ResponseWriter, r *http.Request) {
	tf := normalizeTF(r)
	s.mu.RLock()
	g, ok := s.gates[tf]
	s.mu.RUnlock()
	w.Header().Set("Content-Type", "application/json")
	if !ok {
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "waiting_for_gate_state", "timeframe": tf})
		return
	}
	_ = json.NewEncoder(w).Encode(g)
}

func (s *Server) handleComparison(w http.ResponseWriter, r *http.Request) {
	five, err := s.db.GetTimeframeStats(s.paperInitialBalance, "5m")
	if err != nil {
		writeJSON(w, nil, err)
		return
	}
	fifteen, err := s.db.GetTimeframeStats(s.paperInitialBalance, "15m")
	if err != nil {
		writeJSON(w, nil, err)
		return
	}
	const minSettled = 30
	out := comparisonPayload{FiveMinute: five, FifteenMinute: fifteen, MinSettled: minSettled, Status: "collecting", Leader: "none", Interpretation: "Need at least 30 settled trades in each timeframe before inferential comparison."}
	out.ReturnDiffPct = five.AverageReturnPct - fifteen.AverageReturnPct
	out.DiffSEPct = math.Sqrt(five.ReturnSEPct*five.ReturnSEPct + fifteen.ReturnSEPct*fifteen.ReturnSEPct)
	if five.SettledTrades >= minSettled && fifteen.SettledTrades >= minSettled && out.DiffSEPct > 0 {
		out.ZScore = out.ReturnDiffPct / out.DiffSEPct
		if math.Abs(out.ZScore) >= 1.96 {
			out.Status = "statistically_separated"
			if out.ReturnDiffPct > 0 {
				out.Leader = "5m"
			} else {
				out.Leader = "15m"
			}
			out.Interpretation = "Average paper return per trade differs at approximately the 95% normal-approximation threshold. Keep collecting data before live use."
		} else {
			out.Status = "no_significant_difference"
			out.Interpretation = "Current average-return difference is not statistically separated at |z| >= 1.96."
		}
	}
	writeJSON(w, out, nil)
}
