package polymarket

import (
	"bytes"
	"encoding/json"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
)

const defaultMarketWSURL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

type MarketTrade struct {
	Seq       int64     `json:"seq"`
	TokenID   string    `json:"tokenId"`
	Price     float64   `json:"price"`
	Size      float64   `json:"size"`
	Side      string    `json:"side"`
	Timestamp time.Time `json:"timestamp"`
}

type MarketTradeStream struct {
	url       string
	mu        sync.RWMutex
	assets    []string
	version   uint64
	trades    []MarketTrade
	lastRead  time.Time
	conn      *websocket.Conn
	connected bool
	gapCount  int64
	nextSeq   atomic.Int64
	stop      chan struct{}
	stopOnce  sync.Once
	wg        sync.WaitGroup
}

func NewMarketTradeStream() *MarketTradeStream {
	return &MarketTradeStream{url: defaultMarketWSURL, stop: make(chan struct{})}
}

func newMarketTradeStreamWithURL(url string) *MarketTradeStream {
	s := NewMarketTradeStream()
	s.url = url
	return s
}

func (s *MarketTradeStream) Start() {
	if s == nil {
		return
	}
	s.wg.Add(1)
	go s.run()
}

func (s *MarketTradeStream) Stop() {
	if s == nil {
		return
	}
	s.stopOnce.Do(func() { close(s.stop) })
	s.mu.Lock()
	if s.conn != nil {
		_ = s.conn.Close()
	}
	s.mu.Unlock()
	s.wg.Wait()
}

func (s *MarketTradeStream) SetAssets(ids []string) {
	if s == nil {
		return
	}
	uniq := make(map[string]struct{})
	clean := make([]string, 0, len(ids))
	for _, id := range ids {
		id = strings.TrimSpace(id)
		if id == "" {
			continue
		}
		if _, ok := uniq[id]; ok {
			continue
		}
		uniq[id] = struct{}{}
		clean = append(clean, id)
	}
	sort.Strings(clean)

	s.mu.Lock()
	if equalStrings(s.assets, clean) {
		s.mu.Unlock()
		return
	}
	s.assets = clean
	s.version++
	s.trades = nil
	conn := s.conn
	s.mu.Unlock()
	if conn != nil {
		_ = conn.Close()
	}
}

func (s *MarketTradeStream) LastSeq() int64 {
	if s == nil {
		return 0
	}
	return s.nextSeq.Load()
}

func (s *MarketTradeStream) GapCount() int64 {
	if s == nil {
		return 0
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.gapCount
}

func (s *MarketTradeStream) Healthy(maxAge time.Duration) bool {
	if s == nil {
		return false
	}
	if maxAge <= 0 {
		maxAge = 20 * time.Second
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.connected && !s.lastRead.IsZero() && time.Since(s.lastRead) <= maxAge
}

func (s *MarketTradeStream) TradesAfter(seq int64) ([]MarketTrade, int64) {
	if s == nil {
		return nil, 0
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]MarketTrade, 0)
	for _, tr := range s.trades {
		if tr.Seq > seq {
			out = append(out, tr)
		}
	}
	return out, s.nextSeq.Load()
}

func (s *MarketTradeStream) run() {
	defer s.wg.Done()
	for {
		select {
		case <-s.stop:
			return
		default:
		}

		s.mu.RLock()
		assets := append([]string(nil), s.assets...)
		version := s.version
		s.mu.RUnlock()
		if len(assets) == 0 {
			if !sleepStop(s.stop, 250*time.Millisecond) {
				return
			}
			continue
		}

		conn, _, err := websocket.DefaultDialer.Dial(s.url, nil)
		if err != nil {
			if !sleepStop(s.stop, time.Second) {
				return
			}
			continue
		}
		sub := map[string]any{"assets_ids": assets, "type": "market", "custom_feature_enabled": true}
		if err := conn.WriteJSON(sub); err != nil {
			_ = conn.Close()
			if !sleepStop(s.stop, time.Second) {
				return
			}
			continue
		}

		s.mu.Lock()
		s.conn = conn
		s.connected = true
		s.lastRead = time.Now().UTC()
		s.mu.Unlock()

		pingStop := make(chan struct{})
		go func(c *websocket.Conn) {
			t := time.NewTicker(10 * time.Second)
			defer t.Stop()
			for {
				select {
				case <-pingStop:
					return
				case <-t.C:
					_ = c.WriteMessage(websocket.TextMessage, []byte("PING"))
				}
			}
		}(conn)

		err = s.readLoop(conn)
		close(pingStop)
		_ = conn.Close()

		s.mu.Lock()
		if s.conn == conn {
			s.conn = nil
			s.connected = false
		}
		intentionalAssetChange := s.version != version
		if !intentionalAssetChange {
			s.gapCount++
		}
		s.mu.Unlock()
		if !sleepStop(s.stop, 500*time.Millisecond) {
			return
		}
		_ = err
	}
}

func (s *MarketTradeStream) readLoop(conn *websocket.Conn) error {
	for {
		_ = conn.SetReadDeadline(time.Now().Add(25 * time.Second))
		_, msg, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		s.mu.Lock()
		s.lastRead = time.Now().UTC()
		s.mu.Unlock()
		if bytes.Equal(bytes.TrimSpace(msg), []byte("PONG")) {
			continue
		}
		for _, tr := range decodeMarketTrades(msg) {
			tr.Seq = s.nextSeq.Add(1)
			s.mu.Lock()
			s.trades = append(s.trades, tr)
			cutoff := time.Now().UTC().Add(-2 * time.Minute)
			idx := 0
			for idx < len(s.trades) && s.trades[idx].Timestamp.Before(cutoff) {
				idx++
			}
			if idx > 0 {
				s.trades = append([]MarketTrade(nil), s.trades[idx:]...)
			}
			s.mu.Unlock()
		}
	}
}

func decodeMarketTrades(msg []byte) []MarketTrade {
	msg = bytes.TrimSpace(msg)
	if len(msg) == 0 {
		return nil
	}
	if msg[0] == '[' {
		var parts []json.RawMessage
		if json.Unmarshal(msg, &parts) != nil {
			return nil
		}
		var out []MarketTrade
		for _, p := range parts {
			out = append(out, decodeMarketTrades(p)...)
		}
		return out
	}

	var env struct {
		EventType string          `json:"event_type"`
		Type      string          `json:"type"`
		AssetID   string          `json:"asset_id"`
		TokenID   string          `json:"token_id"`
		Price     json.RawMessage `json:"price"`
		Size      json.RawMessage `json:"size"`
		Side      string          `json:"side"`
		Timestamp json.RawMessage `json:"timestamp"`
		Payload   json.RawMessage `json:"payload"`
	}
	if json.Unmarshal(msg, &env) != nil {
		return nil
	}
	kind := env.EventType
	if kind == "" {
		kind = env.Type
	}
	if kind != "last_trade_price" {
		return nil
	}

	if len(env.Payload) > 0 && string(env.Payload) != "null" {
		var p struct {
			TokenIDCamel string          `json:"tokenId"`
			TokenIDSnake string          `json:"token_id"`
			Price        json.RawMessage `json:"price"`
			Size         json.RawMessage `json:"size"`
			Side         string          `json:"side"`
			Timestamp    json.RawMessage `json:"timestamp"`
		}
		if json.Unmarshal(env.Payload, &p) == nil {
			id := p.TokenIDCamel
			if id == "" {
				id = p.TokenIDSnake
			}
			if tr, ok := buildMarketTrade(id, p.Price, p.Size, p.Side, p.Timestamp); ok {
				return []MarketTrade{tr}
			}
		}
	}
	id := env.AssetID
	if id == "" {
		id = env.TokenID
	}
	if tr, ok := buildMarketTrade(id, env.Price, env.Size, env.Side, env.Timestamp); ok {
		return []MarketTrade{tr}
	}
	return nil
}

func buildMarketTrade(tokenID string, rawPrice, rawSize json.RawMessage, side string, rawTS json.RawMessage) (MarketTrade, bool) {
	price, okP := parseJSONNumber(rawPrice)
	size, okS := parseJSONNumber(rawSize)
	side = strings.ToUpper(strings.TrimSpace(side))
	if strings.TrimSpace(tokenID) == "" || !okP || !okS || price <= 0 || size <= 0 || (side != "BUY" && side != "SELL") {
		return MarketTrade{}, false
	}
	return MarketTrade{TokenID: tokenID, Price: price, Size: size, Side: side, Timestamp: parseMarketTimestamp(rawTS)}, true
}

func parseJSONNumber(raw json.RawMessage) (float64, bool) {
	if len(raw) == 0 || string(raw) == "null" {
		return 0, false
	}
	var f float64
	if json.Unmarshal(raw, &f) == nil {
		return f, true
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		v, err := strconv.ParseFloat(s, 64)
		return v, err == nil
	}
	return 0, false
}

func parseMarketTimestamp(raw json.RawMessage) time.Time {
	if len(raw) > 0 && string(raw) != "null" {
		var s string
		if json.Unmarshal(raw, &s) == nil {
			if t, err := time.Parse(time.RFC3339Nano, s); err == nil {
				return t.UTC()
			}
			if ms, err := strconv.ParseInt(s, 10, 64); err == nil {
				return time.UnixMilli(ms).UTC()
			}
		}
		var n int64
		if json.Unmarshal(raw, &n) == nil {
			return time.UnixMilli(n).UTC()
		}
	}
	return time.Now().UTC()
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func sleepStop(stop <-chan struct{}, d time.Duration) bool {
	select {
	case <-stop:
		return false
	case <-time.After(d):
		return true
	}
}
