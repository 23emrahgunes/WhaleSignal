package api

import (
	"encoding/json"
	"net/http"
	"strconv"
	"sync"

	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
)

type Server struct {
	db                  *storage.Database
	paperInitialBalance float64
	mu                  sync.RWMutex
	currentResult       *engine.EvaluationResult
	currentMarket       *polymarket.Market
}

func NewServer(db *storage.Database, paperInitialBalance ...float64) *Server {
	initial := 1000.0
	if len(paperInitialBalance) > 0 && paperInitialBalance[0] > 0 {
		initial = paperInitialBalance[0]
	}
	return &Server{db: db, paperInitialBalance: initial}
}

func (s *Server) UpdateState(res *engine.EvaluationResult, market *polymarket.Market) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.currentResult = res
	s.currentMarket = market
}

func (s *Server) Start(port string) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/api/live", s.cors(s.handleLive))
	mux.HandleFunc("/api/history", s.cors(s.handleHistory))
	mux.HandleFunc("/api/market", s.cors(s.handleMarket))
	mux.HandleFunc("/api/orderflow", s.cors(s.handleOrderflow))
	mux.HandleFunc("/api/paper/stats", s.cors(s.handlePaperStats))
	mux.HandleFunc("/api/paper/trades", s.cors(s.handlePaperTrades))
	fileServer := http.FileServer(http.Dir("web/static"))
	mux.Handle("/", s.corsHandler(fileServer))
	return http.ListenAndServe(":"+port, mux)
}

func (s *Server) cors(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == "OPTIONS" {
			w.WriteHeader(http.StatusOK)
			return
		}
		h(w, r)
	}
}

func (s *Server) corsHandler(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == "OPTIONS" {
			w.WriteHeader(http.StatusOK)
			return
		}
		h.ServeHTTP(w, r)
	})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("OK"))
}

func (s *Server) handleLive(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	res := s.currentResult
	s.mu.RUnlock()
	w.Header().Set("Content-Type", "application/json")
	if res == nil {
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "waiting_for_data"})
		return
	}
	_ = json.NewEncoder(w).Encode(res)
}

func (s *Server) handleHistory(w http.ResponseWriter, r *http.Request) {
	limit := parseLimit(r, 100, 10000)
	history, err := s.db.GetHistory(limit)
	writeJSON(w, history, err)
}

func (s *Server) handleMarket(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	m := s.currentMarket
	s.mu.RUnlock()
	w.Header().Set("Content-Type", "application/json")
	if m == nil {
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "no_active_market"})
		return
	}
	_ = json.NewEncoder(w).Encode(m)
}

func (s *Server) handleOrderflow(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	res := s.currentResult
	s.mu.RUnlock()
	w.Header().Set("Content-Type", "application/json")
	if res == nil {
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "waiting_for_fresh_depth"})
		return
	}
	_ = json.NewEncoder(w).Encode(map[string]interface{}{
		"timestamp":          res.Timestamp,
		"bid_vol":            res.BidVol,
		"ask_vol":            res.AskVol,
		"imbalance":          res.Imbalance,
		"weighted_imbalance": res.WeightedImbalance,
		"depth_source":       res.DepthSource,
		"depth_fresh":        res.DepthFresh,
		"depth_age_ms":       res.DepthAgeMs,
	})
}

func (s *Server) handlePaperStats(w http.ResponseWriter, r *http.Request) {
	stats, err := s.db.GetPaperStats(s.paperInitialBalance)
	writeJSON(w, stats, err)
}

func (s *Server) handlePaperTrades(w http.ResponseWriter, r *http.Request) {
	limit := parseLimit(r, 50, 1000)
	trades, err := s.db.GetPaperTrades(limit)
	writeJSON(w, trades, err)
}

func parseLimit(r *http.Request, fallback, max int) int {
	limit := fallback
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if val, err := strconv.Atoi(raw); err == nil && val > 0 {
			limit = val
		}
	}
	if limit > max {
		limit = max
	}
	return limit
}

func writeJSON(w http.ResponseWriter, payload interface{}, err error) {
	w.Header().Set("Content-Type", "application/json")
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
		return
	}
	_ = json.NewEncoder(w).Encode(payload)
}
