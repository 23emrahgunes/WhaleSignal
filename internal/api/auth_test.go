package api

import (
	"net/http"
	"testing"
	"time"
)

func testAuthServer() *Server {
	s := &Server{}
	s.SetAuth("admin", "s3cret", "hmac-secret-key", 60)
	return s
}

func TestSessionRoundTrip(t *testing.T) {
	s := testAuthServer()
	tok := s.issueSession(time.Now().Add(time.Hour))
	r := &http.Request{Header: http.Header{}}
	r.AddCookie(&http.Cookie{Name: sessionCookie, Value: tok})
	if !s.validSession(r) {
		t.Fatal("gecerli session reddedildi")
	}
}

func TestSessionTamperRejected(t *testing.T) {
	s := testAuthServer()
	tok := s.issueSession(time.Now().Add(time.Hour)) + "x"
	r := &http.Request{Header: http.Header{}}
	r.AddCookie(&http.Cookie{Name: sessionCookie, Value: tok})
	if s.validSession(r) {
		t.Fatal("kurcalanmis session kabul edildi")
	}
}

func TestSessionExpiredRejected(t *testing.T) {
	s := testAuthServer()
	tok := s.issueSession(time.Now().Add(-time.Minute)) // gecmis
	r := &http.Request{Header: http.Header{}}
	r.AddCookie(&http.Cookie{Name: sessionCookie, Value: tok})
	if s.validSession(r) {
		t.Fatal("suresi dolmus session kabul edildi")
	}
}

func TestAuthDisabledWhenUnconfigured(t *testing.T) {
	s := &Server{} // pass/secret bos
	if s.authEnabled() {
		t.Fatal("yapilandirilmamis auth aktif gorunuyor")
	}
}
