// Package clob: Go beyninin CANLI emirleri, resmi py-clob-client v2'yi saran
// yerel Python KOPRU servisine (executor_bridge.py) HTTP ile yaptirdigi katman.
// Go artik imzalamaz — V2 + POLY_1271 imzalamayi kanitlanmis kutuphane yapar.
// shadow modda hic olusturulmaz (nil). live bayragi runtime'da cevrilir.
package clob

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync/atomic"
	"time"

	"go.uber.org/zap"
)

type Side int

const (
	Buy  Side = 0
	Sell Side = 1
)

func (s Side) String() string {
	if s == Sell {
		return "SELL"
	}
	return "BUY"
}

// Config: kopru baglanti parametreleri.
type Config struct {
	ExecutorURL   string // http://127.0.0.1:8099
	ExecutorToken string // X-Executor-Token paylasilan sir
	DryRun        bool
}

// Client: kopruye HTTP yapan yurutucu.
type Client struct {
	base  string
	token string
	live  atomic.Bool
	http  *http.Client
	log   *zap.Logger
}

func New(cfg Config, log *zap.Logger) (*Client, error) {
	base := strings.TrimRight(strings.TrimSpace(cfg.ExecutorURL), "/")
	if base == "" {
		base = "http://127.0.0.1:8099"
	}
	if _, err := url.Parse(base); err != nil {
		return nil, fmt.Errorf("gecersiz EXECUTOR_URL: %w", err)
	}
	c := &Client{base: base, token: cfg.ExecutorToken, http: &http.Client{Timeout: 8 * time.Second}, log: log}
	c.live.Store(!cfg.DryRun)
	mode := "DRY"
	if !cfg.DryRun {
		mode = "LIVE"
	}
	// Kopru saglik kontrolu (blocklamaz; sadece uyarir).
	if err := c.health(); err != nil {
		log.Warn("CLOB KOPRU saglik kontrolu basarisiz (kopru calisiyor mu?)", zap.String("url", base), zap.Error(err))
	}
	log.Warn("CLOB EXECUTOR (kopru) KURULDU", zap.String("baslangic", mode), zap.String("url", base))
	return c, nil
}

func (c *Client) SetLive(v bool) {
	if c == nil {
		return
	}
	c.live.Store(v)
	m := "DRY"
	if v {
		m = "LIVE"
	}
	c.log.Warn("CLOB MOD DEGISTI", zap.String("mode", m))
}

func (c *Client) IsLive() bool { return c != nil && c.live.Load() }
func (c *Client) DryRun() bool { return c == nil || !c.live.Load() }

// PlaceLimit: dinlenen (GTC) limit emir. DRY'de kopru yalniz IMZALAR (POST yok).
func (c *Client) PlaceLimit(tokenID string, side Side, size, price float64) (string, error) {
	return c.place(tokenID, side, size, price, "GTC")
}

// PlaceMarketable: marketable (FAK) emir — hedge icin.
func (c *Client) PlaceMarketable(tokenID string, side Side, size, price float64) (string, error) {
	return c.place(tokenID, side, size, price, "FAK")
}

func (c *Client) place(tokenID string, side Side, size, price float64, orderType string) (string, error) {
	dry := !c.live.Load()
	var out struct {
		OK      bool   `json:"ok"`
		OrderID string `json:"orderId"`
		Dry     bool   `json:"dry"`
		Error   string `json:"error"`
	}
	err := c.post("/place", map[string]any{
		"token_id": tokenID, "side": side.String(), "size": size, "price": price,
		"type": orderType, "dry": dry,
	}, &out)
	if err != nil {
		return "", err
	}
	if !out.OK {
		return "", fmt.Errorf("kopru emir reddetti: %s", out.Error)
	}
	if dry {
		c.log.Info("CLOB DRY: kopru imzaladi (POST YOK)", zap.String("type", orderType), zap.String("side", side.String()), zap.String("tokenId", short(tokenID)), zap.Float64("size", size), zap.Float64("price", price))
		return fmt.Sprintf("dry-%s-%d", strings.ToLower(side.String()), time.Now().UnixNano()), nil
	}
	c.log.Info("CLOB CANLI EMIR", zap.String("orderId", out.OrderID), zap.String("type", orderType), zap.String("side", side.String()), zap.Float64("price", price), zap.Float64("size", size))
	return out.OrderID, nil
}

func (c *Client) Cancel(orderID string) error {
	if !c.live.Load() || strings.HasPrefix(orderID, "dry-") || orderID == "" {
		return nil // DRY / sahte / bos -> no-op
	}
	var out struct {
		OK    bool   `json:"ok"`
		Error string `json:"error"`
	}
	if err := c.post("/cancel", map[string]any{"order_id": orderID}, &out); err != nil {
		return err
	}
	c.log.Info("CLOB CANLI IPTAL", zap.String("orderId", orderID))
	return nil
}

func (c *Client) GetFilledShares(orderID string) (float64, error) {
	if !c.live.Load() || strings.HasPrefix(orderID, "dry-") || orderID == "" {
		return 0, nil
	}
	var out struct {
		OK          bool    `json:"ok"`
		SizeMatched float64 `json:"sizeMatched"`
	}
	if err := c.get("/order?id="+url.QueryEscape(orderID), &out); err != nil {
		return 0, err
	}
	return out.SizeMatched, nil
}

func (c *Client) health() error {
	var out struct {
		OK bool `json:"ok"`
	}
	return c.get("/health", &out)
}

func (c *Client) post(path string, body any, out any) error {
	raw, _ := json.Marshal(body)
	req, err := http.NewRequest("POST", c.base+path, bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.token != "" {
		req.Header.Set("X-Executor-Token", c.token)
	}
	return c.do(req, out)
}

func (c *Client) get(path string, out any) error {
	req, err := http.NewRequest("GET", c.base+path, nil)
	if err != nil {
		return err
	}
	if c.token != "" {
		req.Header.Set("X-Executor-Token", c.token)
	}
	return c.do(req, out)
}

func (c *Client) do(req *http.Request, out any) error {
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 300 {
		return fmt.Errorf("kopru %s -> %d: %s", req.URL.Path, resp.StatusCode, strings.TrimSpace(string(data)))
	}
	if out != nil {
		return json.Unmarshal(data, out)
	}
	return nil
}

func short(s string) string {
	if len(s) <= 12 {
		return s
	}
	return s[:6] + ".." + s[len(s)-4:]
}
