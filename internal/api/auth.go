package api

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const sessionCookie = "pmedge_session"

// publicPath: auth GEREKTIRMEYEN yollar (login akisi + saglik).
func publicPath(p string) bool {
	switch p {
	case "/health", "/api/login", "/login.html", "/favicon.ico":
		return true
	}
	return false
}

// requireAuth: koruma aktifse gecerli session cookie ister. /api/* -> 401 JSON;
// diger (HTML/statik) -> /login.html'e yonlendir. Koruma kapaliysa aynen gecirir.
func (s *Server) requireAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !s.authEnabled() || publicPath(r.URL.Path) || r.Method == http.MethodOptions {
			next.ServeHTTP(w, r)
			return
		}
		if s.validSession(r) {
			next.ServeHTTP(w, r)
			return
		}
		if strings.HasPrefix(r.URL.Path, "/api/") {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte(`{"error":"giris gerekli"}`))
			return
		}
		http.Redirect(w, r, "/login.html", http.StatusFound)
	})
}

// handleLogin: {user,pass} dogrula -> imzali session cookie kur.
func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		User string `json:"user"`
		Pass string `json:"pass"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	userOK := subtle.ConstantTimeCompare([]byte(strings.TrimSpace(body.User)), []byte(s.authUser)) == 1
	passOK := subtle.ConstantTimeCompare([]byte(body.Pass), []byte(s.authPass)) == 1
	if !s.authEnabled() || !userOK || !passOK {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"ok":false,"error":"kullanici veya sifre hatali"}`))
		return
	}
	ttl := time.Duration(s.authTTLMin) * time.Minute
	if ttl <= 0 {
		ttl = 12 * time.Hour
	}
	http.SetCookie(w, &http.Cookie{
		Name: sessionCookie, Value: s.issueSession(time.Now().Add(ttl)),
		Path: "/", HttpOnly: true, SameSite: http.SameSiteLaxMode, MaxAge: int(ttl.Seconds()),
	})
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"ok":true}`))
}

// handleLogout: cookie'yi temizler.
func (s *Server) handleLogout(w http.ResponseWriter, r *http.Request) {
	http.SetCookie(w, &http.Cookie{Name: sessionCookie, Value: "", Path: "/", MaxAge: -1, HttpOnly: true})
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"ok":true}`))
}

// issueSession: "<expUnix>.<hmac>" imzali token.
func (s *Server) issueSession(exp time.Time) string {
	payload := strconv.FormatInt(exp.Unix(), 10)
	return payload + "." + s.sign(payload)
}

func (s *Server) sign(payload string) string {
	mac := hmac.New(sha256.New, []byte(s.authSecret))
	mac.Write([]byte(payload))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// validSession: cookie imzasi ve suresi gecerli mi.
func (s *Server) validSession(r *http.Request) bool {
	ck, err := r.Cookie(sessionCookie)
	if err != nil || ck.Value == "" {
		return false
	}
	parts := strings.SplitN(ck.Value, ".", 2)
	if len(parts) != 2 {
		return false
	}
	if subtle.ConstantTimeCompare([]byte(parts[1]), []byte(s.sign(parts[0]))) != 1 {
		return false
	}
	exp, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || time.Now().Unix() > exp {
		return false
	}
	return true
}

// authedRequest: canli-kontrol uclari icin (session + taze sifre onayi kontrolu).
func (s *Server) checkFreshPassword(pass string) bool {
	return s.authEnabled() && subtle.ConstantTimeCompare([]byte(pass), []byte(s.authPass)) == 1
}
