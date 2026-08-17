package api

import (
	"encoding/json"
	"net/http"
)

// SetDirectionControl: yon tahmini (Model A) canli-kontrol kancalari.
// nil ise kontrol pasif (shadow). dual40Control tipi yeniden kullanilir.
func (s *Server) SetDirectionControl(setLive func(bool) error, kill func(), status func() string, execErr func() string) {
	s.dir = &dual40Control{setLive: setLive, kill: kill, status: status, execErr: execErr}
}

// GET /api/direction/status -> {mode, controllable, lastError}
func (s *Server) handleDirectionStatus(w http.ResponseWriter, r *http.Request) {
	mode := "shadow"
	lastErr := ""
	if s.dir != nil {
		if s.dir.status != nil {
			mode = s.dir.status()
		}
		if s.dir.execErr != nil {
			lastErr = s.dir.execErr()
		}
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"mode": mode, "controllable": s.dir != nil, "lastError": lastErr})
}

// POST /api/direction/mode {mode:"dry"|"live", pass} -> session + taze sifre gerektirir.
func (s *Server) handleDirectionMode(w http.ResponseWriter, r *http.Request) {
	var body struct{ Mode, Pass string }
	_ = json.NewDecoder(r.Body).Decode(&body)
	if !s.checkFreshPassword(body.Pass) {
		writeJSONErr(w, http.StatusUnauthorized, "sifre onayi gerekli")
		return
	}
	if s.dir == nil || s.dir.setLive == nil {
		writeJSONErr(w, http.StatusBadRequest, "kontrol pasif (shadow)")
		return
	}
	if err := s.dir.setLive(body.Mode == "live"); err != nil {
		writeJSONErr(w, http.StatusBadRequest, err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "mode": s.dir.status()})
}

// POST /api/direction/kill {pass} -> session + taze sifre; canli kapat + yeni emir yok.
func (s *Server) handleDirectionKill(w http.ResponseWriter, r *http.Request) {
	var body struct{ Pass string }
	_ = json.NewDecoder(r.Body).Decode(&body)
	if !s.checkFreshPassword(body.Pass) {
		writeJSONErr(w, http.StatusUnauthorized, "sifre onayi gerekli")
		return
	}
	if s.dir == nil || s.dir.kill == nil {
		writeJSONErr(w, http.StatusBadRequest, "kontrol pasif (shadow)")
		return
	}
	s.dir.kill()
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "mode": "dry"})
}
