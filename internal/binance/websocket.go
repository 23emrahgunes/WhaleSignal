package binance

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"strconv"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"go.uber.org/zap"
	"pm-edge/internal/util"
)

const binanceWSStaleAfter = 3 * time.Second

type WSManager struct {
	client     *Client
	stopChan   chan struct{}
	stopOnce   sync.Once
	wg         sync.WaitGroup
	wsConn     *websocket.Conn
	mu         sync.Mutex
	IsMockMode bool
}

func NewWSManager(client *Client, isMockMode bool) *WSManager {
	return &WSManager{client: client, stopChan: make(chan struct{}), IsMockMode: isMockMode}
}

func (w *WSManager) Start() {
	if w.IsMockMode {
		return
	}
	w.wg.Add(1)
	go w.run()
}

func (w *WSManager) Stop() {
	w.stopOnce.Do(func() { close(w.stopChan) })
	w.mu.Lock()
	if w.wsConn != nil {
		_ = w.wsConn.Close()
	}
	w.mu.Unlock()
	w.wg.Wait()
}

func (w *WSManager) run() {
	defer w.wg.Done()
	url := "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@depth20@100ms"
	backoff := time.Second

	for {
		select {
		case <-w.stopChan:
			return
		default:
		}

		util.Logger.Info("Connecting to Binance WS streams...", zap.String("url", url))
		conn, _, err := websocket.DefaultDialer.Dial(url, nil)
		if err != nil {
			w.client.SetWSState(false, true)
			util.Logger.Warn("Binance WS connection failed", zap.Error(err), zap.Duration("backoff", backoff))
			if !w.sleepWithContext(backoff) {
				return
			}
			backoff = nextBinanceBackoff(backoff)
			continue
		}

		backoff = time.Second
		w.mu.Lock()
		w.wsConn = conn
		w.mu.Unlock()
		w.client.SetWSState(true, false)
		util.Logger.Info("Connected to Binance WebSocket stream")

		err = w.readLoop(conn)
		w.client.SetWSState(false, true)
		w.mu.Lock()
		if w.wsConn == conn {
			w.wsConn = nil
		}
		w.mu.Unlock()
		_ = conn.Close()

		select {
		case <-w.stopChan:
			return
		default:
			util.Logger.Warn("Binance WebSocket connection lost", zap.Error(err))
		}
		if !w.sleepWithContext(time.Second) {
			return
		}
	}
}

func nextBinanceBackoff(d time.Duration) time.Duration {
	d = time.Duration(float64(d) * 1.5)
	if d > 60*time.Second {
		return 60 * time.Second
	}
	return d
}

func (w *WSManager) sleepWithContext(d time.Duration) bool {
	select {
	case <-w.stopChan:
		return false
	case <-time.After(d):
		return true
	}
}

type CombinedStreamPayload struct {
	Stream string          `json:"stream"`
	Data   json.RawMessage `json:"data"`
}

type TradeEvent struct {
	EventTime int64  `json:"E"`
	Price     string `json:"p"`
	Quantity  string `json:"q"`
}

type DepthEvent struct {
	Bids [][]string `json:"b"`
	Asks [][]string `json:"a"`
}

func (w *WSManager) readLoop(conn *websocket.Conn) error {
	for {
		// A TCP connection can remain open while data silently stops. Force a
		// reconnect when no message arrives so REST can take over immediately.
		_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
		_, msg, err := conn.ReadMessage()
		if err != nil {
			return err
		}

		var payload CombinedStreamPayload
		if err := json.Unmarshal(msg, &payload); err != nil {
			continue
		}

		switch payload.Stream {
		case "btcusdt@trade":
			var ev TradeEvent
			if err := json.Unmarshal(payload.Data, &ev); err != nil {
				continue
			}
			price, errP := strconv.ParseFloat(ev.Price, 64)
			size, errQ := strconv.ParseFloat(ev.Quantity, 64)
			if errP == nil && errQ == nil && price > 0 {
				w.client.UpdateFromTrade(price, size, time.UnixMilli(ev.EventTime).UTC(), true)
			}
		case "btcusdt@depth20@100ms":
			var ev DepthEvent
			if err := json.Unmarshal(payload.Data, &ev); err == nil {
				w.client.UpdateDepth(ev.Bids, ev.Asks, time.Now().UTC())
			}
		}
	}
}

// StartFallbackRESTPoller also acts as a freshness watchdog. It polls REST when
// WS is disconnected OR when the last live price is stale despite an open WS.
func (w *WSManager) StartFallbackRESTPoller() {
	if w.IsMockMode {
		return
	}
	w.wg.Add(1)
	go func() {
		defer w.wg.Done()
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-w.stopChan:
				return
			case now := <-ticker.C:
				if !w.client.ShouldRESTFallback(now.UTC(), binanceWSStaleAfter) {
					continue
				}
				price, err := w.client.FetchTickerPriceREST()
				if err != nil {
					util.Logger.Warn("Binance fallback REST poller failed", zap.Error(err))
					continue
				}
				w.client.UpdateFromTrade(price, 0, now.UTC(), false)
			}
		}
	}()
}

func (w *WSManager) StartMockDataInjector() {
	if !w.IsMockMode {
		return
	}
	w.wg.Add(1)
	go func() {
		defer w.wg.Done()
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-w.stopChan:
				return
			case now := <-ticker.C:
				p := 98000 + rand.Float64()*1000
				w.client.UpdateFromTrade(p, 0.5, now.UTC(), false)
				w.client.SetDataSource("MOCK")
				bids := [][]string{{fmt.Sprintf("%f", p-10), "5.5"}, {fmt.Sprintf("%f", p-20), "10.0"}}
				asks := [][]string{{fmt.Sprintf("%f", p+10), "4.8"}, {fmt.Sprintf("%f", p+20), "12.0"}}
				w.client.UpdateDepth(bids, asks, now.UTC())
			}
		}
	}()
}
