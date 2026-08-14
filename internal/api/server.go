package api

import (
	"encoding/json"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"

	"pm-edge/internal/engine"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/storage"
)

type Server struct {
	db                  *storage.Database
	paperInitialBalance float64
	mu                  sync.RWMutex
	currentResults      map[string]*engine.EvaluationResult
	currentMarkets      map[string]*polymarket.Market
	gates               map[string]gateState

	// auth (Faz 3): pass+secret ikisi de set ise dashboard+API session ister.
	authUser   string
	authPass   string
	authSecret string
	authTTLMin int
	d40        *dual40Control // canli-kontrol kancalari (nil = pasif/shadow)
	d40ModelB  func() any     // Model B shadow son sonuc kancasi
}

// SetAuth: dashboard giris korumasini yapilandirir. pass VE secret bos degilse
// koruma aktif; aksi halde acik kalir (uyari loglanir main'de).
func (s *Server) SetAuth(user, pass, secret string, ttlMin int) {
	s.authUser, s.authPass, s.authSecret, s.authTTLMin = user, pass, secret, ttlMin
}

func (s *Server) authEnabled() bool { return s.authPass != "" && s.authSecret != "" }

func NewServer(db *storage.Database, paperInitialBalance ...float64) *Server {
	initial := 1000.0
	if len(paperInitialBalance) > 0 && paperInitialBalance[0] > 0 {
		initial = paperInitialBalance[0]
	}
	return &Server{db: db, paperInitialBalance: initial, currentResults: make(map[string]*engine.EvaluationResult), currentMarkets: make(map[string]*polymarket.Market), gates: make(map[string]gateState)}
}

func (s *Server) UpdateState(res *engine.EvaluationResult, market *polymarket.Market) {
	s.UpdateStateFor("5m", res, market)
}

func (s *Server) Start(port string) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/api/live", s.cors(s.handleLive))
	mux.HandleFunc("/api/history", s.cors(s.handleHistory))
	mux.HandleFunc("/api/market", s.cors(s.handleMarket))
	mux.HandleFunc("/api/orderflow", s.cors(s.handleOrderflow))
	mux.HandleFunc("/api/microstructure", s.cors(s.handleMicrostructure))
	mux.HandleFunc("/api/microstructure/history", s.cors(s.handleMicrostructureHistory))
	mux.HandleFunc("/api/paper/stats", s.cors(s.handlePaperStats))
	mux.HandleFunc("/api/paper/trades", s.cors(s.handlePaperTrades))
	mux.HandleFunc("/api/paper/inverse/trades", s.cors(s.handlePaperInverseTrades))
	mux.HandleFunc("/api/paper/inverse/stats", s.cors(s.handlePaperInverseStats))
	mux.HandleFunc("/api/paper/hedges", s.cors(s.handlePaperHedges))
	mux.HandleFunc("/api/paper/hedge/stats", s.cors(s.handlePaperHedgeStats))
	mux.HandleFunc("/api/gates", s.cors(s.handleGates))
	mux.HandleFunc("/api/arb", s.cors(s.handleArbLive))
	mux.HandleFunc("/api/arb/history", s.cors(s.handleArbHistory))
	mux.HandleFunc("/api/arb/stats", s.cors(s.handleArbStats))
	mux.HandleFunc("/api/arb/paper/cycles", s.cors(s.handleArbPaperCycles))
	mux.HandleFunc("/api/arb/paper/stats", s.cors(s.handleArbPaperStats))
	mux.HandleFunc("/api/arb/paper/analysis", s.cors(s.handleArbPaperAnalysis))
	mux.HandleFunc("/api/comparison", s.cors(s.handleComparison))
	mux.HandleFunc("/api/login", s.cors(s.handleLogin))
	mux.HandleFunc("/api/logout", s.cors(s.handleLogout))
	mux.HandleFunc("/api/dual40/status", s.cors(s.handleDual40Status))
	mux.HandleFunc("/api/dual40/modelb", s.cors(s.handleDual40ModelB))
	mux.HandleFunc("/api/dual40/mode", s.cors(s.handleDual40Mode))
	mux.HandleFunc("/api/dual40/kill", s.cors(s.handleDual40Kill))
	fileServer := http.FileServer(http.Dir("web/static"))
	mux.Handle("/", s.corsHandler(s.staticWithInverseAB(fileServer)))
	return http.ListenAndServe(":"+port, s.requireAuth(mux))
}

// staticWithInverseAB leaves the existing dashboard file untouched and injects
// a tiny versioned A/B overlay only on the root/index response. Other static
// assets, including /dual40.html, keep the standard file-server path.
func (s *Server) staticWithInverseAB(fileServer http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" && r.URL.Path != "/index.html" {
			fileServer.ServeHTTP(w, r)
			return
		}
		data, err := os.ReadFile("web/static/index.html")
		if err != nil {
			http.Error(w, "dashboard unavailable", http.StatusInternalServerError)
			return
		}
		html := strings.Replace(string(data), "</body>", `<script src="/inverse-ab.js?v=1"></script></body>`, 1)
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write([]byte(html))
	})
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
	tf := normalizeTF(r)
	s.mu.RLock()
	res := s.currentResults[tf]
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
	history, err := s.db.GetHistoryByTimeframe(limit, normalizeTF(r))
	writeJSON(w, history, err)
}

func (s *Server) handleMarket(w http.ResponseWriter, r *http.Request) {
	tf := normalizeTF(r)
	s.mu.RLock()
	m := s.currentMarkets[tf]
	s.mu.RUnlock()
	w.Header().Set("Content-Type", "application/json")
	if m == nil {
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "no_active_market"})
		return
	}
	_ = json.NewEncoder(w).Encode(m)
}

func (s *Server) handleOrderflow(w http.ResponseWriter, r *http.Request) {
	tf := normalizeTF(r)
	s.mu.RLock()
	res := s.currentResults[tf]
	s.mu.RUnlock()
	w.Header().Set("Content-Type", "application/json")
	if res == nil {
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "waiting_for_fresh_depth"})
		return
	}
	_ = json.NewEncoder(w).Encode(map[string]interface{}{
		"timestamp":                res.Timestamp,
		"bid_vol":                  res.BidVol,
		"ask_vol":                  res.AskVol,
		"spoof_filtered_bid_vol":   res.SpoofFilteredBidVol,
		"spoof_filtered_ask_vol":   res.SpoofFilteredAskVol,
		"bid_notional_usd":         res.BidNotionalUSD,
		"ask_notional_usd":         res.AskNotionalUSD,
		"total_depth_notional_usd": res.TotalDepthNotionalUSD,
		"best_bid":                 res.BestBid,
		"best_ask":                 res.BestAsk,
		"worst_bid_20":             res.WorstBid20,
		"worst_ask_20":             res.WorstAsk20,
		"mid_price":                res.DepthMidPrice,
		"spread_usd":               res.SpreadUSD,
		"spread_bps":               res.SpreadBps,
		"bid_range_usd":            res.BidRangeUSD,
		"ask_range_usd":            res.AskRangeUSD,
		"bid_range_bps":            res.BidRangeBps,
		"ask_range_bps":            res.AskRangeBps,
		"imbalance":                res.Imbalance,
		"weighted_imbalance":       res.WeightedImbalance,
		"order_flow_score":         res.OrderFlowScore,
		"depth_source":             res.DepthSource,
		"depth_fresh":              res.DepthFresh,
		"depth_age_ms":             res.DepthAgeMs,
	})
}

func (s *Server) handlePaperStats(w http.ResponseWriter, r *http.Request) {
	stats, err := s.db.GetTimeframeStats(s.paperInitialBalance, normalizeTF(r))
	writeJSON(w, stats, err)
}

func (s *Server) handlePaperTrades(w http.ResponseWriter, r *http.Request) {
	limit := parseLimit(r, 50, 1000)
	trades, err := s.db.GetPaperTradesByTimeframe(limit, normalizeTF(r))
	writeJSON(w, trades, err)
}

func (s *Server) handlePaperInverseTrades(w http.ResponseWriter, r *http.Request) {
	limit := parseLimit(r, 50, 1000)
	rows, err := s.db.GetPaperInverseTradesByTimeframe(limit, normalizeTF(r))
	writeJSON(w, rows, err)
}

func (s *Server) handlePaperInverseStats(w http.ResponseWriter, r *http.Request) {
	stats, err := s.db.GetPaperInverseStatsByTimeframe(normalizeTF(r))
	writeJSON(w, stats, err)
}

func (s *Server) handlePaperHedges(w http.ResponseWriter, r *http.Request) {
	limit := parseLimit(r, 50, 1000)
	rows, err := s.db.GetPaperHedgesByTimeframe(limit, normalizeTF(r))
	writeJSON(w, rows, err)
}

func (s *Server) handlePaperHedgeStats(w http.ResponseWriter, r *http.Request) {
	stats, err := s.db.GetPaperHedgeStatsByTimeframe(normalizeTF(r))
	writeJSON(w, stats, err)
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
