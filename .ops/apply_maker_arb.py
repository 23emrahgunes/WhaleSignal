from pathlib import Path


def write(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def replace_once(path, old, new):
    p = Path(path)
    s = p.read_text()
    assert old in s, f'marker not found in {path}: {old[:120]!r}'
    p.write_text(s.replace(old, new, 1))


write('internal/arb/engine.go', r'''package arb

import (
    "math"
    "strings"

    "pm-edge/internal/engine"
    "pm-edge/internal/polymarket"
)

const (
    StatusCandidate = "CANDIDATE"
    StatusBlocked   = "BLOCKED"
)

type Config struct {
    Timeframe           string
    Enabled             bool
    TargetEdge          float64
    OperationalBuffer   float64
    UncertaintyPenalty  float64
    MaxStrandedUnits    int
}

type Snapshot struct {
    Timestamp            string  `json:"timestamp"`
    Timeframe            string  `json:"timeframe"`
    MarketSlug           string  `json:"marketSlug"`
    Status               string  `json:"status"`
    Reason               string  `json:"reason"`
    ShadowOnly           bool    `json:"shadowOnly"`
    OrderMode            string  `json:"orderMode"`
    MakerFeeRate         float64 `json:"makerFeeRate"`

    UpTokenID            string  `json:"upTokenId"`
    DownTokenID          string  `json:"downTokenId"`
    UpBestBid            float64 `json:"upBestBid"`
    UpBestAsk            float64 `json:"upBestAsk"`
    DownBestBid          float64 `json:"downBestBid"`
    DownBestAsk          float64 `json:"downBestAsk"`
    UpTickSize           float64 `json:"upTickSize"`
    DownTickSize         float64 `json:"downTickSize"`
    UpMinOrderSize       float64 `json:"upMinOrderSize"`
    DownMinOrderSize     float64 `json:"downMinOrderSize"`
    OrderSize            float64 `json:"orderSize"`
    MaxStrandedShares    float64 `json:"maxStrandedShares"`

    UpMakerPrice         float64 `json:"upMakerPrice"`
    DownMakerPrice       float64 `json:"downMakerPrice"`
    PairCost             float64 `json:"pairCost"`
    GrossEdge            float64 `json:"grossEdge"`
    NetEdge              float64 `json:"netEdge"`
    TargetEdge           float64 `json:"targetEdge"`
    OperationalBuffer    float64 `json:"operationalBuffer"`
    PairEdgePass         bool    `json:"pairEdgePass"`
    ExpectedLockedProfit float64 `json:"expectedLockedProfit"`

    PTBReady             bool    `json:"ptbReady"`
    PTBDecision          string  `json:"ptbDecision"`
    PTBPUp               float64 `json:"ptbPUp"`
    PTBPDown             float64 `json:"ptbPDown"`
    PTBConfidence        float64 `json:"ptbConfidence"`
    UpExitRisk           float64 `json:"upExitRisk"`
    DownExitRisk         float64 `json:"downExitRisk"`
    UpStrandedEV         float64 `json:"upStrandedEv"`
    DownStrandedEV       float64 `json:"downStrandedEv"`
    FirstLeg             string  `json:"firstLeg"`
    QuoteSkew            string  `json:"quoteSkew"`

    DownCompletionMax    float64 `json:"downCompletionMax"`
    UpCompletionMax      float64 `json:"upCompletionMax"`
}

type Engine struct{ cfg Config }

func NewEngine(cfg Config) *Engine {
    if strings.TrimSpace(cfg.Timeframe) == "" {
        cfg.Timeframe = "5m"
    }
    if cfg.TargetEdge <= 0 {
        cfg.TargetEdge = 0.02
    }
    if cfg.OperationalBuffer <= 0 {
        cfg.OperationalBuffer = 0.002
    }
    if cfg.UncertaintyPenalty <= 0 {
        cfg.UncertaintyPenalty = 0.02
    }
    if cfg.MaxStrandedUnits < 1 {
        cfg.MaxStrandedUnits = 1
    }
    return &Engine{cfg: cfg}
}

func (e *Engine) Enabled() bool { return e != nil && e.cfg.Enabled }

func (e *Engine) Evaluate(res *engine.EvaluationResult, market *polymarket.Market, upBook, downBook polymarket.BookSnapshot) *Snapshot {
    if e == nil || !e.cfg.Enabled || res == nil || market == nil {
        return nil
    }
    snap := &Snapshot{
        Timestamp:          res.Timestamp,
        Timeframe:          e.cfg.Timeframe,
        MarketSlug:         market.Slug,
        Status:             StatusBlocked,
        Reason:             "BOOK_NOT_READY",
        ShadowOnly:         true,
        OrderMode:          "GTC_GTD_POST_ONLY",
        MakerFeeRate:       0,
        UpTokenID:          upBook.TokenID,
        DownTokenID:        downBook.TokenID,
        UpBestBid:          upBook.BestBid,
        UpBestAsk:          upBook.BestAsk,
        DownBestBid:        downBook.BestBid,
        DownBestAsk:        downBook.BestAsk,
        UpTickSize:         upBook.TickSize,
        DownTickSize:       downBook.TickSize,
        UpMinOrderSize:     upBook.MinOrderSize,
        DownMinOrderSize:   downBook.MinOrderSize,
        TargetEdge:         e.cfg.TargetEdge,
        OperationalBuffer:  e.cfg.OperationalBuffer,
        PTBReady:           res.PTBTerminal.Ready,
        PTBDecision:        res.PTBTerminal.Decision,
        PTBConfidence:      res.PTBTerminal.Confidence,
    }

    if !validBook(upBook) || !validBook(downBook) {
        return snap
    }
    snap.OrderSize = math.Max(upBook.MinOrderSize, downBook.MinOrderSize)
    snap.MaxStrandedShares = snap.OrderSize * float64(e.cfg.MaxStrandedUnits)

    pUp, pDown := res.PUp, res.PDown
    if res.PTBTerminal.Ready {
        pUp, pDown = res.PTBTerminal.PAbove, res.PTBTerminal.PBelow
    }
    snap.PTBPUp, snap.PTBPDown = pUp, pDown

    upPassive, okUpPassive := MakerBuyPrice(upBook, false)
    downPassive, okDownPassive := MakerBuyPrice(downBook, false)
    upAggressive, okUpAggressive := MakerBuyPrice(upBook, true)
    downAggressive, okDownAggressive := MakerBuyPrice(downBook, true)
    if !okUpPassive || !okDownPassive {
        return snap
    }
    if !okUpAggressive {
        upAggressive = upPassive
    }
    if !okDownAggressive {
        downAggressive = downPassive
    }

    upAggEV, _, _ := strandedEV(pUp, upAggressive, upBook.BestBid, res.PTBTerminal.Confidence, e.cfg.UncertaintyPenalty)
    downAggEV, _, _ := strandedEV(pDown, downAggressive, downBook.BestBid, res.PTBTerminal.Confidence, e.cfg.UncertaintyPenalty)
    safeLeg := "UP"
    if downAggEV > upAggEV {
        safeLeg = "DOWN"
    }

    upQuote, downQuote := upPassive, downPassive
    if safeLeg == "UP" {
        upQuote = upAggressive
        snap.QuoteSkew = "UP_AGGRESSIVE_DOWN_PASSIVE"
    } else {
        downQuote = downAggressive
        snap.QuoteSkew = "DOWN_AGGRESSIVE_UP_PASSIVE"
    }

    // If the queue-jump tick consumes too much edge, fall back to both passive
    // maker quotes instead of inventing edge below the target.
    net := 1 - upQuote - downQuote - e.cfg.OperationalBuffer
    if net+1e-12 < e.cfg.TargetEdge {
        upQuote, downQuote = upPassive, downPassive
        snap.QuoteSkew = "BOTH_PASSIVE"
        net = 1 - upQuote - downQuote - e.cfg.OperationalBuffer
    }

    snap.UpMakerPrice, snap.DownMakerPrice = upQuote, downQuote
    snap.PairCost = upQuote + downQuote
    snap.GrossEdge = 1 - snap.PairCost
    snap.NetEdge = net
    snap.PairEdgePass = net+1e-12 >= e.cfg.TargetEdge
    if snap.PairEdgePass {
        snap.ExpectedLockedProfit = snap.OrderSize * snap.NetEdge
    }

    snap.UpStrandedEV, snap.UpExitRisk, _ = strandedEV(pUp, upQuote, upBook.BestBid, res.PTBTerminal.Confidence, e.cfg.UncertaintyPenalty)
    snap.DownStrandedEV, snap.DownExitRisk, _ = strandedEV(pDown, downQuote, downBook.BestBid, res.PTBTerminal.Confidence, e.cfg.UncertaintyPenalty)
    snap.FirstLeg = "UP"
    if snap.DownStrandedEV > snap.UpStrandedEV {
        snap.FirstLeg = "DOWN"
    }

    snap.DownCompletionMax = completionMax(upQuote, e.cfg.TargetEdge, e.cfg.OperationalBuffer, downBook)
    snap.UpCompletionMax = completionMax(downQuote, e.cfg.TargetEdge, e.cfg.OperationalBuffer, upBook)

    if !snap.PairEdgePass {
        snap.Reason = "PAIR_EDGE_BELOW_TARGET"
        return snap
    }
    if !res.PTBTerminal.Ready {
        snap.Reason = "PTB_TERMINAL_NOT_READY"
        return snap
    }
    snap.Status = StatusCandidate
    snap.Reason = "READY"
    return snap
}

func validBook(b polymarket.BookSnapshot) bool {
    return b.TokenID != "" && b.BestBid > 0 && b.BestAsk > b.BestBid && b.BestAsk < 1 && b.TickSize > 0 && b.MinOrderSize > 0
}

// MakerBuyPrice returns a post-only BUY price. improve=true attempts one tick
// above best bid, but never crosses/touches the best ask. If the spread is only
// one tick, it joins the current best bid.
func MakerBuyPrice(book polymarket.BookSnapshot, improve bool) (float64, bool) {
    if !validBook(book) {
        return 0, false
    }
    price := floorToTick(book.BestBid, book.TickSize)
    if improve {
        candidate := floorToTick(book.BestBid+book.TickSize, book.TickSize)
        if candidate < book.BestAsk-1e-12 {
            price = candidate
        }
    }
    if price <= 0 || price >= book.BestAsk-1e-12 {
        return 0, false
    }
    return price, true
}

func completionMax(filledPrice, targetEdge, operationalBuffer float64, opposite polymarket.BookSnapshot) float64 {
    if !validBook(opposite) {
        return 0
    }
    arbCeiling := floorToTick(1-filledPrice-targetEdge-operationalBuffer, opposite.TickSize)
    postOnlyCeiling := floorToTick(opposite.BestAsk-opposite.TickSize, opposite.TickSize)
    if postOnlyCeiling < opposite.BestBid {
        postOnlyCeiling = floorToTick(opposite.BestBid, opposite.TickSize)
    }
    if arbCeiling > postOnlyCeiling {
        arbCeiling = postOnlyCeiling
    }
    if arbCeiling < 0 {
        return 0
    }
    return arbCeiling
}

func strandedEV(prob, makerPrice, bestBid, confidence, uncertaintyPenalty float64) (ev, exitRisk, modelRisk float64) {
    prob = clamp(prob, 0, 1)
    exitRisk = math.Max(0, makerPrice-bestBid)
    conf := clamp(confidence/100, 0, 1)
    modelRisk = uncertaintyPenalty * (1 - conf)
    ev = prob - makerPrice - exitRisk - modelRisk
    return ev, exitRisk, modelRisk
}

func floorToTick(v, tick float64) float64 {
    if tick <= 0 {
        return v
    }
    units := math.Floor((v + 1e-10) / tick)
    return math.Round(units*tick*1e8) / 1e8
}

func clamp(v, lo, hi float64) float64 {
    if v < lo { return lo }
    if v > hi { return hi }
    return v
}
''')

write('internal/arb/engine_test.go', r'''package arb

import (
    "math"
    "testing"

    "pm-edge/internal/engine"
    "pm-edge/internal/polymarket"
)

func book(token string, bid, ask float64) polymarket.BookSnapshot {
    return polymarket.BookSnapshot{TokenID: token, BestBid: bid, BestAsk: ask, TickSize: 0.01, MinOrderSize: 5}
}

func baseResult() *engine.EvaluationResult {
    return &engine.EvaluationResult{Timestamp: "2026-08-12T00:00:00Z", PUp: .7, PDown: .3, PTBTerminal: engine.PTBTerminalEstimate{Ready: true, Decision: "UP", PAbove: .80, PBelow: .20, Confidence: 60}}
}

func TestMakerBuyPriceNeverCrossesAsk(t *testing.T) {
    p, ok := MakerBuyPrice(book("u", .42, .44), true)
    if !ok || math.Abs(p-.43) > 1e-9 { t.Fatalf("got %.4f ok=%v", p, ok) }
    p, ok = MakerBuyPrice(book("u", .42, .43), true)
    if !ok || math.Abs(p-.42) > 1e-9 { t.Fatalf("one-tick spread must join bid: %.4f", p) }
}

func TestDynamicMinOrderSizeAndSafeLegSkew(t *testing.T) {
    e := NewEngine(Config{Enabled:true, Timeframe:"5m", TargetEdge:.02, OperationalBuffer:.002, UncertaintyPenalty:.02, MaxStrandedUnits:1})
    up := book("up", .40, .44)
    down := book("down", .54, .58)
    down.MinOrderSize = 7
    s := e.Evaluate(baseResult(), &polymarket.Market{Slug:"btc-updown-5m-1"}, up, down)
    if s == nil { t.Fatal("nil snapshot") }
    if s.OrderSize != 7 || s.MaxStrandedShares != 7 { t.Fatalf("dynamic min size not used: %+v", s) }
    if s.FirstLeg != "UP" { t.Fatalf("expected safe UP leg, got %s", s.FirstLeg) }
    if s.UpMakerPrice != .41 || s.DownMakerPrice != .54 { t.Fatalf("expected safe-leg queue jump, got %.2f/%.2f", s.UpMakerPrice, s.DownMakerPrice) }
    if !s.PairEdgePass || s.Status != StatusCandidate { t.Fatalf("expected candidate: %+v", s) }
}

func TestPairEdgeBelowTargetBlocked(t *testing.T) {
    e := NewEngine(Config{Enabled:true, TargetEdge:.03, OperationalBuffer:.002})
    s := e.Evaluate(baseResult(), &polymarket.Market{Slug:"btc-updown-5m-1"}, book("up", .49, .51), book("down", .49, .51))
    if s.PairEdgePass || s.Status != StatusBlocked || s.Reason != "PAIR_EDGE_BELOW_TARGET" { t.Fatalf("unexpected %+v", s) }
}

func TestPTBNotReadyFailsClosed(t *testing.T) {
    r := baseResult(); r.PTBTerminal.Ready = false
    e := NewEngine(Config{Enabled:true, TargetEdge:.02, OperationalBuffer:.002})
    s := e.Evaluate(r, &polymarket.Market{Slug:"btc-updown-5m-1"}, book("up", .40, .44), book("down", .54, .58))
    if s.Status != StatusBlocked || s.Reason != "PTB_TERMINAL_NOT_READY" { t.Fatalf("unexpected %+v", s) }
}

func TestCompletionMaxPreservesTargetAndPostOnly(t *testing.T) {
    opposite := book("down", .54, .58)
    got := completionMax(.41, .02, .002, opposite)
    if got != .55 { t.Fatalf("got %.4f want .55 (post-only ceiling)", got) }
    if .41+got+.02+.002 > 1+1e-9 { t.Fatal("completion price breaks target edge") }
}
''')

write('internal/storage/arb.go', r'''package storage

import (
    "database/sql"
    "encoding/json"
    "fmt"

    "pm-edge/internal/arb"
)

type ArbStats struct {
    Timeframe       string  `json:"timeframe"`
    TotalSnapshots  int     `json:"totalSnapshots"`
    Candidates      int     `json:"candidates"`
    CandidateRate   float64 `json:"candidateRate"`
    AverageNetEdge  float64 `json:"averageNetEdge"`
    BestNetEdge     float64 `json:"bestNetEdge"`
    UpFirst         int     `json:"upFirst"`
    DownFirst       int     `json:"downFirst"`
    LastStatus      string  `json:"lastStatus"`
    LastReason      string  `json:"lastReason"`
    LastNetEdge     float64 `json:"lastNetEdge"`
}

func (d *Database) EnsureArbSchema() error {
    if d == nil || d.db == nil { return fmt.Errorf("database unavailable") }
    _, err := d.db.Exec(`
        CREATE TABLE IF NOT EXISTS arb_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            market_slug TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            net_edge REAL NOT NULL,
            target_edge REAL NOT NULL,
            first_leg TEXT NOT NULL,
            order_size REAL NOT NULL,
            pair_edge_pass INTEGER NOT NULL,
            ptb_ready INTEGER NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_arb_tf_time ON arb_snapshots(timeframe, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_arb_market ON arb_snapshots(market_slug, timestamp DESC);
    `)
    return err
}

func (d *Database) InsertArbSnapshot(s *arb.Snapshot) error {
    if s == nil { return fmt.Errorf("nil arb snapshot") }
    raw, err := json.Marshal(s)
    if err != nil { return err }
    _, err = d.db.Exec(`INSERT INTO arb_snapshots
        (timestamp,timeframe,market_slug,status,reason,net_edge,target_edge,first_leg,order_size,pair_edge_pass,ptb_ready,payload)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`,
        s.Timestamp, NormalizeTimeframe(s.Timeframe), s.MarketSlug, s.Status, s.Reason, s.NetEdge, s.TargetEdge, s.FirstLeg, s.OrderSize, boolInt(s.PairEdgePass), boolInt(s.PTBReady), string(raw))
    return err
}

func (d *Database) GetLatestArbSnapshot(tf string) (*arb.Snapshot, error) {
    var raw string
    err := d.db.QueryRow(`SELECT payload FROM arb_snapshots WHERE timeframe=? ORDER BY id DESC LIMIT 1`, NormalizeTimeframe(tf)).Scan(&raw)
    if err == sql.ErrNoRows { return nil, nil }
    if err != nil { return nil, err }
    var out arb.Snapshot
    if err := json.Unmarshal([]byte(raw), &out); err != nil { return nil, err }
    return &out, nil
}

func (d *Database) GetArbSnapshotsByTimeframe(limit int, tf string) ([]arb.Snapshot, error) {
    if limit < 1 { limit = 50 }
    if limit > 1000 { limit = 1000 }
    rows, err := d.db.Query(`SELECT payload FROM arb_snapshots WHERE timeframe=? ORDER BY id DESC LIMIT ?`, NormalizeTimeframe(tf), limit)
    if err != nil { return nil, err }
    defer rows.Close()
    out := make([]arb.Snapshot, 0, limit)
    for rows.Next() {
        var raw string
        if err := rows.Scan(&raw); err != nil { return nil, err }
        var s arb.Snapshot
        if err := json.Unmarshal([]byte(raw), &s); err != nil { return nil, err }
        out = append(out, s)
    }
    return out, rows.Err()
}

func (d *Database) GetArbStatsByTimeframe(tf string) (ArbStats, error) {
    tf = NormalizeTimeframe(tf)
    out := ArbStats{Timeframe: tf}
    err := d.db.QueryRow(`SELECT COUNT(*), COALESCE(SUM(CASE WHEN status='CANDIDATE' THEN 1 ELSE 0 END),0),
        COALESCE(AVG(CASE WHEN status='CANDIDATE' THEN net_edge END),0), COALESCE(MAX(net_edge),0),
        COALESCE(SUM(CASE WHEN first_leg='UP' THEN 1 ELSE 0 END),0), COALESCE(SUM(CASE WHEN first_leg='DOWN' THEN 1 ELSE 0 END),0)
        FROM arb_snapshots WHERE timeframe=?`, tf).Scan(&out.TotalSnapshots, &out.Candidates, &out.AverageNetEdge, &out.BestNetEdge, &out.UpFirst, &out.DownFirst)
    if err != nil { return out, err }
    if out.TotalSnapshots > 0 { out.CandidateRate = float64(out.Candidates)/float64(out.TotalSnapshots) }
    var status, reason sql.NullString
    var edge sql.NullFloat64
    err = d.db.QueryRow(`SELECT status,reason,net_edge FROM arb_snapshots WHERE timeframe=? ORDER BY id DESC LIMIT 1`, tf).Scan(&status,&reason,&edge)
    if err != nil && err != sql.ErrNoRows { return out, err }
    if status.Valid { out.LastStatus = status.String }
    if reason.Valid { out.LastReason = reason.String }
    if edge.Valid { out.LastNetEdge = edge.Float64 }
    return out, nil
}

func boolInt(v bool) int { if v { return 1 }; return 0 }
''')

write('internal/storage/arb_test.go', r'''package storage

import (
    "path/filepath"
    "testing"

    "pm-edge/internal/arb"
)

func TestArbStorageRoundTripAndStats(t *testing.T) {
    db, err := NewDatabase(filepath.Join(t.TempDir(), "arb.sqlite")); if err != nil { t.Fatal(err) }; defer db.Close()
    if err := db.EnsureArbSchema(); err != nil { t.Fatal(err) }
    s := &arb.Snapshot{Timestamp:"2026-08-12T00:00:00Z", Timeframe:"5m", MarketSlug:"btc-updown-5m-1", Status:arb.StatusCandidate, Reason:"READY", NetEdge:.031, TargetEdge:.02, FirstLeg:"UP", OrderSize:5, PairEdgePass:true, PTBReady:true}
    if err := db.InsertArbSnapshot(s); err != nil { t.Fatal(err) }
    got, err := db.GetLatestArbSnapshot("5m"); if err != nil { t.Fatal(err) }
    if got == nil || got.NetEdge != .031 || got.OrderSize != 5 { t.Fatalf("bad roundtrip %+v", got) }
    stats, err := db.GetArbStatsByTimeframe("5m"); if err != nil { t.Fatal(err) }
    if stats.TotalSnapshots != 1 || stats.Candidates != 1 || stats.UpFirst != 1 { t.Fatalf("bad stats %+v", stats) }
}
''')

write('internal/api/arb.go', r'''package api

import "net/http"

func (s *Server) handleArbLive(w http.ResponseWriter, r *http.Request) {
    row, err := s.db.GetLatestArbSnapshot(normalizeTF(r))
    if err != nil { writeJSON(w, nil, err); return }
    if row == nil { writeJSON(w, map[string]string{"status":"waiting_for_arb_data", "timeframe":normalizeTF(r)}, nil); return }
    writeJSON(w, row, nil)
}

func (s *Server) handleArbHistory(w http.ResponseWriter, r *http.Request) {
    rows, err := s.db.GetArbSnapshotsByTimeframe(parseLimit(r, 50, 1000), normalizeTF(r))
    writeJSON(w, rows, err)
}

func (s *Server) handleArbStats(w http.ResponseWriter, r *http.Request) {
    stats, err := s.db.GetArbStatsByTimeframe(normalizeTF(r))
    writeJSON(w, stats, err)
}
''')

write('internal/api/arb_test.go', r'''package api

import (
    "net/http/httptest"
    "path/filepath"
    "strings"
    "testing"

    "pm-edge/internal/arb"
    "pm-edge/internal/storage"
)

func TestArbLiveEndpoint(t *testing.T) {
    db, err := storage.NewDatabase(filepath.Join(t.TempDir(), "api.sqlite")); if err != nil { t.Fatal(err) }; defer db.Close()
    if err := db.EnsureArbSchema(); err != nil { t.Fatal(err) }
    if err := db.InsertArbSnapshot(&arb.Snapshot{Timestamp:"2026-08-12T00:00:00Z",Timeframe:"5m",MarketSlug:"btc-updown-5m-1",Status:arb.StatusCandidate,Reason:"READY",NetEdge:.03,TargetEdge:.02,FirstLeg:"UP",OrderSize:5}); err != nil { t.Fatal(err) }
    s := NewServer(db)
    req := httptest.NewRequest("GET", "/api/arb?tf=5m", nil); rec := httptest.NewRecorder()
    s.handleArbLive(rec, req)
    if rec.Code != 200 || !strings.Contains(rec.Body.String(), `"netEdge":0.03`) { t.Fatalf("%d %s", rec.Code, rec.Body.String()) }
}
''')

write('cmd/pm-edge/arb_shadow.go', r'''package main

import (
    "sync/atomic"

    "go.uber.org/zap"
    "pm-edge/internal/arb"
    "pm-edge/internal/config"
    "pm-edge/internal/engine"
    "pm-edge/internal/polymarket"
    "pm-edge/internal/storage"
    "pm-edge/internal/util"
)

type arbShadowRuntime struct {
    engine   *arb.Engine
    db       *storage.Database
    pmClient *polymarket.Client
    busy     atomic.Bool
}

func newArbShadowRuntime(tf string, cfg *config.Config, db *storage.Database, pmClient *polymarket.Client, enabled bool) *arbShadowRuntime {
    return &arbShadowRuntime{engine:arb.NewEngine(arb.Config{
        Timeframe:tf, Enabled:enabled && cfg.ArbShadowEnabled,
        TargetEdge:cfg.ArbTargetEdge, OperationalBuffer:cfg.ArbOperationalBuffer,
        UncertaintyPenalty:cfg.ArbUncertaintyPenalty, MaxStrandedUnits:cfg.ArbMaxStrandedUnits,
    }), db:db, pmClient:pmClient}
}

func (r *arbShadowRuntime) Submit(res *engine.EvaluationResult, market *polymarket.Market) {
    if r == nil || r.engine == nil || !r.engine.Enabled() || res == nil || market == nil { return }
    if !r.busy.CompareAndSwap(false, true) { return }
    rc := *res
    mc := *market
    mc.Tokens = append([]polymarket.Token(nil), market.Tokens...)
    go func() {
        defer r.busy.Store(false)
        upID, ok := polymarket.TokenIDForOutcome(&mc, "UP"); if !ok { util.Logger.Warn("Maker arb missing UP token", zap.String("market",mc.Slug)); return }
        downID, ok := polymarket.TokenIDForOutcome(&mc, "DOWN"); if !ok { util.Logger.Warn("Maker arb missing DOWN token", zap.String("market",mc.Slug)); return }
        upBook, err := r.pmClient.FetchBookSnapshot(upID); if err != nil { util.Logger.Warn("Maker arb UP book unavailable", zap.Error(err), zap.String("market",mc.Slug)); return }
        downBook, err := r.pmClient.FetchBookSnapshot(downID); if err != nil { util.Logger.Warn("Maker arb DOWN book unavailable", zap.Error(err), zap.String("market",mc.Slug)); return }
        snap := r.engine.Evaluate(&rc, &mc, upBook, downBook)
        if snap == nil { return }
        if err := r.db.InsertArbSnapshot(snap); err != nil { util.Logger.Error("Maker arb snapshot store failed", zap.Error(err)); return }
        if snap.Status == arb.StatusCandidate {
            util.Logger.Info("MAKER ARB SHADOW CANDIDATE", zap.String("market",snap.MarketSlug), zap.Float64("up",snap.UpMakerPrice), zap.Float64("down",snap.DownMakerPrice), zap.Float64("netEdge",snap.NetEdge), zap.Float64("shares",snap.OrderSize), zap.String("safeFirstLeg",snap.FirstLeg), zap.Float64("upStrandedEV",snap.UpStrandedEV), zap.Float64("downStrandedEV",snap.DownStrandedEV))
        }
    }()
}
''')

write('internal/polymarket/book_snapshot_test.go', r'''package polymarket

import (
    "net/http"
    "net/http/httptest"
    "testing"
)

func TestFetchBookSnapshotParsesDynamicMinAndTick(t *testing.T) {
    srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        w.Header().Set("Content-Type","application/json")
        _, _ = w.Write([]byte(`{"bids":[{"price":"0.40","size":"9"},{"price":"0.41","size":"7"}],"asks":[{"price":"0.44","size":"8"},{"price":"0.43","size":"6"}],"min_order_size":"5","tick_size":"0.01"}`))
    })); defer srv.Close()
    c := NewClientWithBaseURL("http://unused", srv.Client())
    b, err := c.fetchBookSnapshot(srv.URL, "tok"); if err != nil { t.Fatal(err) }
    if b.BestBid != .41 || b.BestAsk != .43 || b.MinOrderSize != 5 || b.TickSize != .01 { t.Fatalf("bad book %+v", b) }
    if len(b.Bids)!=2 || len(b.Asks)!=2 { t.Fatalf("levels missing %+v", b) }
}
''')

# Append full CLOB book snapshot support without disturbing the existing market-BUY quote path.
replace_once('internal/polymarket/clob.go', '\nfunc TokenIDForOutcome', r'''

type BookSnapshot struct {
    TokenID      string      `json:"tokenId"`
    BestBid      float64     `json:"bestBid"`
    BestAsk      float64     `json:"bestAsk"`
    MinOrderSize float64     `json:"minOrderSize"`
    TickSize     float64     `json:"tickSize"`
    Bids         []CLOBLevel `json:"bids,omitempty"`
    Asks         []CLOBLevel `json:"asks,omitempty"`
}

type clobFullBookResponse struct {
    Bids []struct { Price string `json:"price"`; Size string `json:"size"` } `json:"bids"`
    Asks []struct { Price string `json:"price"`; Size string `json:"size"` } `json:"asks"`
    MinOrderSize string `json:"min_order_size"`
    TickSize string `json:"tick_size"`
}

func (c *Client) FetchBookSnapshot(tokenID string) (BookSnapshot, error) {
    return c.fetchBookSnapshot(defaultCLOBBaseURL, tokenID)
}

func (c *Client) fetchBookSnapshot(baseURL, tokenID string) (BookSnapshot, error) {
    out := BookSnapshot{TokenID: tokenID}
    if strings.TrimSpace(tokenID) == "" { return out, fmt.Errorf("missing token id") }
    endpoint := strings.TrimRight(baseURL, "/") + "/book?" + url.Values{"token_id": []string{tokenID}}.Encode()
    resp, err := c.httpClient.Get(endpoint); if err != nil { return out, err }
    defer resp.Body.Close()
    if resp.StatusCode != http.StatusOK { return out, fmt.Errorf("clob book status %d", resp.StatusCode) }
    var raw clobFullBookResponse
    if err := json.NewDecoder(resp.Body).Decode(&raw); err != nil { return out, err }
    out.MinOrderSize, _ = strconv.ParseFloat(raw.MinOrderSize, 64)
    out.TickSize, _ = strconv.ParseFloat(raw.TickSize, 64)
    for _, row := range raw.Bids {
        p, ep := strconv.ParseFloat(row.Price,64); s, es := strconv.ParseFloat(row.Size,64)
        if ep==nil && es==nil && p>0 && p<1 && s>0 { out.Bids=append(out.Bids,CLOBLevel{Price:p,Size:s}) }
    }
    for _, row := range raw.Asks {
        p, ep := strconv.ParseFloat(row.Price,64); s, es := strconv.ParseFloat(row.Size,64)
        if ep==nil && es==nil && p>0 && p<1 && s>0 { out.Asks=append(out.Asks,CLOBLevel{Price:p,Size:s}) }
    }
    sort.Slice(out.Bids, func(i,j int) bool { return out.Bids[i].Price > out.Bids[j].Price })
    sort.Slice(out.Asks, func(i,j int) bool { return out.Asks[i].Price < out.Asks[j].Price })
    if len(out.Bids)==0 || len(out.Asks)==0 { return out, fmt.Errorf("incomplete CLOB book for token %s",tokenID) }
    if out.MinOrderSize<=0 { return out, fmt.Errorf("invalid min_order_size for token %s",tokenID) }
    if out.TickSize<=0 { return out, fmt.Errorf("invalid tick_size for token %s",tokenID) }
    out.BestBid, out.BestAsk = out.Bids[0].Price, out.Asks[0].Price
    if out.BestAsk <= out.BestBid { return out, fmt.Errorf("crossed CLOB book for token %s",tokenID) }
    return out,nil
}

func TokenIDForOutcome''')

# Config surface.
replace_once('internal/config/config.go', '\tPaperMinEconomicEdge   float64\n', '\tPaperMinEconomicEdge   float64\n\n\tArbShadowEnabled       bool\n\tArbTargetEdge          float64\n\tArbOperationalBuffer   float64\n\tArbUncertaintyPenalty  float64\n\tArbMaxStrandedUnits    int\n')
replace_once('internal/config/config.go', '\t\tPaperMinEconomicEdge:      envFloat("PAPER_MIN_ECONOMIC_EDGE", 0.05),\n', '\t\tPaperMinEconomicEdge:      envFloat("PAPER_MIN_ECONOMIC_EDGE", 0.05),\n\t\tArbShadowEnabled:          envBool("ARB_SHADOW_ENABLED", true),\n\t\tArbTargetEdge:             envFloat("ARB_TARGET_EDGE", 0.02),\n\t\tArbOperationalBuffer:      envFloat("ARB_OPERATIONAL_BUFFER", 0.002),\n\t\tArbUncertaintyPenalty:     envFloat("ARB_UNCERTAINTY_PENALTY", 0.02),\n\t\tArbMaxStrandedUnits:       envInt("ARB_MAX_STRANDED_UNITS", 1),\n')

# DB migration + 5m runtime wiring.
replace_once('cmd/pm-edge/main.go', '\tif err := db.EnsureMicrostructureSchema(); err != nil {\n\t\tutil.Logger.Fatal("Deep microstructure schema setup failed", zap.Error(err))\n\t}\n', '\tif err := db.EnsureMicrostructureSchema(); err != nil {\n\t\tutil.Logger.Fatal("Deep microstructure schema setup failed", zap.Error(err))\n\t}\n\tif err := db.EnsureArbSchema(); err != nil {\n\t\tutil.Logger.Fatal("Maker arb schema setup failed", zap.Error(err))\n\t}\n')
replace_once('cmd/pm-edge/main.go', '\tpaperEngine := paper.NewEngine(db, paper.Config{', '\tarbShadow := newArbShadowRuntime("5m", cfg, db, pmClient, !isMockMode)\n\tpaperEngine := paper.NewEngine(db, paper.Config{')
replace_once('cmd/pm-edge/main.go', '\t\t\t\tif isMockMode || strings.Contains(res.DataSource, "MOCK") {\n', '\t\t\t\tif isMockMode || strings.Contains(res.DataSource, "MOCK") {\n')
replace_once('cmd/pm-edge/main.go', '\t\t\t\tif err := db.InsertSignalWithMicro(res); err != nil {', '\t\t\t\tarbShadow.Submit(res, m)\n\t\t\t\tif err := db.InsertSignalWithMicro(res); err != nil {')

# 15m wiring.
replace_once('cmd/pm-edge/runtime15.go', '\tpaperEngine := paper.NewEngine(db, paper.Config{', '\tarbShadow := newArbShadowRuntime("15m", cfg, db, pmClient, !isMockMode)\n\tpaperEngine := paper.NewEngine(db, paper.Config{')
replace_once('cmd/pm-edge/runtime15.go', '\t\t\t\tif err := db.InsertSignalWithMicro(res); err != nil {', '\t\t\t\tarbShadow.Submit(res, m)\n\t\t\t\tif err := db.InsertSignalWithMicro(res); err != nil {')

# API routes.
replace_once('internal/api/server.go', '\tmux.HandleFunc("/api/gates", s.cors(s.handleGates))\n', '\tmux.HandleFunc("/api/gates", s.cors(s.handleGates))\n\tmux.HandleFunc("/api/arb", s.cors(s.handleArbLive))\n\tmux.HandleFunc("/api/arb/history", s.cors(s.handleArbHistory))\n\tmux.HandleFunc("/api/arb/stats", s.cors(s.handleArbStats))\n')

# Environment defaults.
p = Path('.env.example'); s = p.read_text();
if 'ARB_SHADOW_ENABLED' not in s:
    s += r'''

# Maker arbitrage shadow engine. NO live orders are submitted.
# Maker fee is modeled as zero; operational buffer remains conservative.
ARB_SHADOW_ENABLED=true
ARB_TARGET_EDGE=0.02
ARB_OPERATIONAL_BUFFER=0.002
ARB_UNCERTAINTY_PENALTY=0.02
# One unit = max(UP min_order_size, DOWN min_order_size), normally 5 shares.
ARB_MAX_STRANDED_UNITS=1
'''
p.write_text(s)

write('docs/maker-arb-shadow.md', r'''# Maker Arb Shadow Engine

This engine is research/shadow-only. It never signs or submits a Polymarket order.

For each active BTC 5m/15m market it reads both outcome CLOB books and uses the market-provided `min_order_size` and `tick_size`.

- Order mode modeled: GTC/GTD + post-only maker.
- Maker fee assumption: 0.
- Base order size: `max(UP.min_order_size, DOWN.min_order_size)`.
- Max stranded inventory: one base unit by default.
- Net pair edge: `1 - upMaker - downMaker - operationalBuffer`.
- Candidate gate: net pair edge >= target edge AND PTB terminal estimate is ready.
- Safe first leg: max PTB stranded EV after exit-risk and model-uncertainty penalty.
- Safer leg may improve one tick above best bid; risky leg remains passive. The engine falls back to both-passive quotes if the queue jump consumes target edge.
- Completion ceiling preserves both target edge and post-only status.

SQLite table `arb_snapshots` stores every evaluated shadow snapshot. APIs:

- `/api/arb?tf=5m|15m`
- `/api/arb/history?tf=...&limit=...`
- `/api/arb/stats?tf=...`
''')

# Dashboard card and JS.
p = Path('web/static/index.html'); s = p.read_text()
card_marker = '  <div class="card">\n    <h2>Kağıt İşlem Portföyü — Asıl Strateji A</h2>'
card = r'''  <div class="card">
    <h2>Maker Arbitraj — Ters Bacak Risk Motoru (Gölge)</h2>
    <div class="banner" style="margin:-4px 0 14px;border-radius:7px;border:1px solid #80500a"><b>CANLI EMİR YOK</b> — Gerçek Polymarket UP/DOWN emir defteriyle GTC/GTD + post-only maker arbitraj matematiği izlenir. Minimum pay adedi marketin min_order_size değerinden okunur.</div>
    <div class="grid4">
      <div class="mini"><span>Durum</span><strong id="arbStatus">BEKLENİYOR</strong></div>
      <div class="mini"><span>Maker Fiyatları (Yukarı + Aşağı)</span><strong id="arbPair">—</strong></div>
      <div class="mini"><span>Net Arbitraj Avantajı / Hedef</span><strong id="arbEdge">—</strong></div>
      <div class="mini"><span>Emir Birimi / Azami Ters Bacak</span><strong id="arbSize">—</strong></div>
      <div class="mini"><span>Daha Güvenli İlk Bacak</span><strong id="arbFirstLeg">—</strong></div>
      <div class="mini"><span>Tek Bacak Beklenen Değeri (Yukarı / Aşağı)</span><strong id="arbStranded">—</strong></div>
      <div class="mini"><span>Karşı Bacak Azami Maker Fiyatı</span><strong id="arbCompletion">—</strong></div>
      <div class="mini"><span>PTB Terminal Olasılığı</span><strong id="arbPTB">—</strong></div>
    </div>
    <div class="scroll" style="margin-top:14px"><table><thead><tr><th>Saat</th><th>Durum</th><th>UP</th><th>DOWN</th><th>Net Avantaj</th><th>Pay</th><th>Güvenli Bacak</th><th>UP Tek Bacak EV</th><th>DOWN Tek Bacak EV</th><th>Neden</th></tr></thead><tbody id="arbBody"><tr><td colspan="10">Arbitraj gölge verisi bekleniyor...</td></tr></tbody></table></div>
  </div>

'''
assert card_marker in s
s = s.replace(card_marker, card + card_marker, 1)
js_marker = 'async function updatePaper(){'
js = r'''async function updateArbLive(){
  const a=await getJSON('/api/arb?tf='+activeTf);if(a.status){document.getElementById('arbStatus').innerHTML=chip('VERİ BEKLENİYOR','neutral');return}
  document.getElementById('arbStatus').innerHTML=a.status==='CANDIDATE'?chip('ADAY · '+reasonTr(a.reason),'fresh'):chip('BEKLE · '+reasonTr(a.reason),'warn');
  document.getElementById('arbPair').textContent=`${Number(a.upMakerPrice||0).toFixed(3)} + ${Number(a.downMakerPrice||0).toFixed(3)} = ${Number(a.pairCost||0).toFixed(3)}`;
  document.getElementById('arbEdge').textContent=`${pct(a.netEdge||0,2)} / hedef ${pct(a.targetEdge||0,2)} · tampon ${pct(a.operationalBuffer||0,2)}`;
  document.getElementById('arbSize').textContent=`${Number(a.orderSize||0).toFixed(2)} pay / ${Number(a.maxStrandedShares||0).toFixed(2)} pay`;
  document.getElementById('arbFirstLeg').innerHTML=a.firstLeg?decisionChip(a.firstLeg):'—';
  document.getElementById('arbStranded').textContent=`${Number(a.upStrandedEv||0).toFixed(3)} / ${Number(a.downStrandedEv||0).toFixed(3)}`;
  document.getElementById('arbCompletion').textContent=`UP dolarsa DOWN ≤ ${Number(a.downCompletionMax||0).toFixed(3)} · DOWN dolarsa UP ≤ ${Number(a.upCompletionMax||0).toFixed(3)}`;
  document.getElementById('arbPTB').textContent=a.ptbReady?`${pct(a.ptbPUp,1)} / ${pct(a.ptbPDown,1)} · ${directionText(a.ptbDecision)}`:'Hazır değil';
}
async function updateArbHistory(){
  const rows=await getJSON('/api/arb/history?limit=20&tf='+activeTf);const body=document.getElementById('arbBody');if(!rows||!rows.length){body.innerHTML='<tr><td colspan="10">Arbitraj gölge verisi bekleniyor...</td></tr>';return}
  body.innerHTML=rows.map(a=>`<tr><td>${timeOnly(a.timestamp)}</td><td>${a.status==='CANDIDATE'?chip('ADAY','fresh'):chip('BEKLE','neutral')}</td><td>${Number(a.upMakerPrice||0).toFixed(3)}</td><td>${Number(a.downMakerPrice||0).toFixed(3)}</td><td>${pct(a.netEdge||0,2)}</td><td>${Number(a.orderSize||0).toFixed(2)}</td><td>${a.firstLeg?directionText(a.firstLeg):'—'}</td><td>${Number(a.upStrandedEv||0).toFixed(3)}</td><td>${Number(a.downStrandedEv||0).toFixed(3)}</td><td>${reasonTr(a.reason)}</td></tr>`).join('');
}
'''
assert js_marker in s
s = s.replace(js_marker, js + js_marker, 1)
s = s.replace("'BLOCKED':'ENGELLENDİ','READY':'HAZIR'", "'PAIR_EDGE_BELOW_TARGET':'Net maker arbitraj avantajı hedefin altında','BOOK_NOT_READY':'UP/DOWN emir defteri hazır değil','BLOCKED':'ENGELLENDİ','READY':'HAZIR'", 1)
s = s.replace('await Promise.all([updateLive(),updateGates()])', 'await Promise.all([updateLive(),updateGates(),updateArbLive()])', 1)
s = s.replace('await Promise.all([updateHistory(),updatePaper(),updateHedge(),updateComparison()])', 'await Promise.all([updateHistory(),updatePaper(),updateHedge(),updateComparison(),updateArbHistory()])', 1)
p.write_text(s)
