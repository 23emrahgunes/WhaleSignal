package api

import (
	"encoding/json"
	"net/http"
)

// dual40 kontrol kancalari (main.go runtime'a baglar). nil ise kontrol pasif.
type dual40Control struct {
	setLive func(bool) error
	kill    func()
	status  func() string
}

// SetDual40Control: canli-kontrol kancalarini baglar (mode/kill/status uclari icin).
func (s *Server) SetDual40Control(setLive func(bool) error, kill func(), status func() string) {
	s.d40 = &dual40Control{setLive: setLive, kill: kill, status: status}
}

// GET /api/dual40/status -> {mode}
func (s *Server) handleDual40Status(w http.ResponseWriter, r *http.Request) {
	mode := "shadow"
	if s.d40 != nil && s.d40.status != nil {
		mode = s.d40.status()
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"mode": mode, "controllable": s.d40 != nil})
}

// POST /api/dual40/mode {mode:"dry"|"live", pass} -> session + taze sifre gerektirir.
func (s *Server) handleDual40Mode(w http.ResponseWriter, r *http.Request) {
	var body struct{ Mode, Pass string }
	_ = json.NewDecoder(r.Body).Decode(&body)
	if !s.checkFreshPassword(body.Pass) {
		writeJSONErr(w, http.StatusUnauthorized, "sifre onayi gerekli")
		return
	}
	if s.d40 == nil || s.d40.setLive == nil {
		writeJSONErr(w, http.StatusBadRequest, "kontrol pasif (shadow)")
		return
	}
	if err := s.d40.setLive(body.Mode == "live"); err != nil {
		writeJSONErr(w, http.StatusBadRequest, err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "mode": s.d40.status()})
}

// POST /api/dual40/kill {pass} -> session + taze sifre; tum acik emirler iptal + DRY.
func (s *Server) handleDual40Kill(w http.ResponseWriter, r *http.Request) {
	var body struct{ Pass string }
	_ = json.NewDecoder(r.Body).Decode(&body)
	if !s.checkFreshPassword(body.Pass) {
		writeJSONErr(w, http.StatusUnauthorized, "sifre onayi gerekli")
		return
	}
	if s.d40 == nil || s.d40.kill == nil {
		writeJSONErr(w, http.StatusBadRequest, "kontrol pasif (shadow)")
		return
	}
	s.d40.kill()
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "mode": "dry"})
}

func writeJSONErr(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]any{"ok": false, "error": msg})
}
