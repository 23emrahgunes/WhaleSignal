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
	db            *storage.Database
	mu            sync.RWMutex
	currentResult *engine.EvaluationResult
	currentMarket *polymarket.Market
}

func NewServer(db *storage.Database) *Server {
	return &Server{
		db: db,
	}
}

func (s *Server) UpdateState(res *engine.EvaluationResult, market *polymarket.Market) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.currentResult = res
	s.currentMarket = market
}

func (s *Server) Start(port string) error {
	mux := http.NewServeMux()

	// Endpoints
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/api/live", s.cors(s.handleLive))
	mux.HandleFunc("/api/history", s.cors(s.handleHistory))
	mux.HandleFunc("/api/market", s.cors(s.handleMarket))
	mux.HandleFunc("/api/orderflow", s.cors(s.handleOrderflow))

	// Static web files
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
	limitStr := r.URL.Query().Get("limit")
	limit := 100
	if limitStr != "" {
		if val, err := strconv.Atoi(limitStr); err == nil {
			limit = val
		}
	}

	history, err := s.db.GetHistory(limit)
	w.Header().Set("Content-Type", "application/json")
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
		return
	}

	_ = json.NewEncoder(w).Encode(history)
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
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "waiting_for_data"})
		return
	}

	_ = json.NewEncoder(w).Encode(map[string]interface{}{
		"timestamp":          res.Timestamp,
		"bid_vol":            res.BidVol,
		"ask_vol":            res.AskVol,
		"imbalance":          res.Imbalance,
		"weighted_imbalance": res.WeightedImbalance,
	})
}
