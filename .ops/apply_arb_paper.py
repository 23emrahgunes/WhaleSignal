from pathlib import Path


def write(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def replace_once(path, old, new):
    p = Path(path)
    s = p.read_text()
    assert old in s, f'marker not found in {path}: {old[:140]!r}'
    p.write_text(s.replace(old, new, 1))


write('internal/arb/paper.go', r'''package arb

import (
    "math"
    "strings"
    "time"

    "pm-edge/internal/polymarket"
)

const (
    PaperStatusRestingPair     = "RESTING_PAIR"
    PaperStatusOneLegFilled    = "ONE_LEG_FILLED"
    PaperStatusCompleted       = "COMPLETED"
    PaperStatusExpiredNoFill   = "EXPIRED_NO_FILL"
    PaperStatusStrandedTimeout = "STRANDED_TIMEOUT"
)

type PaperConfig struct {
    Enabled          bool
    OrderTTL         time.Duration
    MaxStranded      time.Duration
    StopBeforeEnd    time.Duration
}

type PaperCycle struct {
    ID                    int64   `json:"id"`
    Timeframe             string  `json:"timeframe"`
    MarketSlug            string  `json:"marketSlug"`
    CreatedAt             string  `json:"createdAt"`
    UpdatedAt             string  `json:"updatedAt"`
    Status                string  `json:"status"`
    Reason                string  `json:"reason"`
    FillModel             string  `json:"fillModel"`
    OrderMode             string  `json:"orderMode"`
    OrderSize             float64 `json:"orderSize"`
    PreferredFirstLeg     string  `json:"preferredFirstLeg"`
    ActualFirstLeg        string  `json:"actualFirstLeg"`
    PreferredFirstMatched bool    `json:"preferredFirstMatched"`

    UpOrderPrice          float64 `json:"upOrderPrice"`
    DownOrderPrice        float64 `json:"downOrderPrice"`
    UpFillPrice           float64 `json:"upFillPrice"`
    DownFillPrice         float64 `json:"downFillPrice"`
    UpFilledAt            string  `json:"upFilledAt"`
    DownFilledAt          string  `json:"downFilledAt"`
    DownCompletionMax     float64 `json:"downCompletionMax"`
    UpCompletionMax       float64 `json:"upCompletionMax"`
    Reprices              int     `json:"reprices"`

    EntryPTBPUp           float64 `json:"entryPtbPUp"`
    EntryPTBPDown         float64 `json:"entryPtbPDown"`
    EntryPTBDecision      string  `json:"entryPtbDecision"`
    EntryNetEdge          float64 `json:"entryNetEdge"`
    TargetEdge            float64 `json:"targetEdge"`
    OperationalBuffer     float64 `json:"operationalBuffer"`

    FirstFillAt           string  `json:"firstFillAt"`
    StrandedSeconds       float64 `json:"strandedSeconds"`
    CompletionMs          int64   `json:"completionMs"`
    ExitMarkPrice         float64 `json:"exitMarkPrice"`
    LockedPnL             float64 `json:"lockedPnl"`
    PaperPnL              float64 `json:"paperPnl"`
    DeployedCost          float64 `json:"deployedCost"`
    ReservedPairCost      float64 `json:"reservedPairCost"`

    LastUpBestBid         float64 `json:"lastUpBestBid"`
    LastUpBestAsk         float64 `json:"lastUpBestAsk"`
    LastDownBestBid       float64 `json:"lastDownBestBid"`
    LastDownBestAsk       float64 `json:"lastDownBestAsk"`
}

func DefaultPaperConfig() PaperConfig {
    return PaperConfig{Enabled: true, OrderTTL: 12 * time.Second, MaxStranded: 20 * time.Second, StopBeforeEnd: 12 * time.Second}
}

func NewPaperCycle(s *Snapshot, now time.Time) *PaperCycle {
    if s == nil || s.Status != StatusCandidate || s.OrderSize <= 0 || s.UpMakerPrice <= 0 || s.DownMakerPrice <= 0 {
        return nil
    }
    now = now.UTC()
    return &PaperCycle{
        Timeframe: s.Timeframe, MarketSlug: s.MarketSlug,
        CreatedAt: now.Format(time.RFC3339Nano), UpdatedAt: now.Format(time.RFC3339Nano),
        Status: PaperStatusRestingPair, Reason: "PAIR_POSTED_SHADOW",
        FillModel: "CONSERVATIVE_CROSS_THROUGH_FULL_SIZE", OrderMode: "GTC_GTD_POST_ONLY",
        OrderSize: s.OrderSize, PreferredFirstLeg: s.FirstLeg,
        UpOrderPrice: s.UpMakerPrice, DownOrderPrice: s.DownMakerPrice,
        DownCompletionMax: s.DownCompletionMax, UpCompletionMax: s.UpCompletionMax,
        EntryPTBPUp: s.PTBPUp, EntryPTBPDown: s.PTBPDown, EntryPTBDecision: s.PTBDecision,
        EntryNetEdge: s.NetEdge, TargetEdge: s.TargetEdge, OperationalBuffer: s.OperationalBuffer,
        ReservedPairCost: s.OrderSize * (s.UpMakerPrice + s.DownMakerPrice),
        LastUpBestBid: s.UpBestBid, LastUpBestAsk: s.UpBestAsk,
        LastDownBestBid: s.DownBestBid, LastDownBestAsk: s.DownBestAsk,
    }
}

func (c *PaperCycle) IsOpen() bool {
    return c != nil && (c.Status == PaperStatusRestingPair || c.Status == PaperStatusOneLegFilled)
}

func (c *PaperCycle) IsTerminal() bool { return c != nil && !c.IsOpen() }

func AdvancePaperCycle(c *PaperCycle, upBook, downBook polymarket.BookSnapshot, now, marketEnd time.Time, cfg PaperConfig) bool {
    if c == nil || !c.IsOpen() {
        return false
    }
    if cfg.OrderTTL <= 0 { cfg.OrderTTL = 12 * time.Second }
    if cfg.MaxStranded <= 0 { cfg.MaxStranded = 20 * time.Second }
    if cfg.StopBeforeEnd <= 0 { cfg.StopBeforeEnd = 12 * time.Second }
    if !validBook(upBook) || !validBook(downBook) {
        return false
    }
    now = now.UTC()
    changed := updateLastBook(c, upBook, downBook)

    if c.Status == PaperStatusRestingPair {
        upFill := makerCrossFill(upBook, c.UpOrderPrice, c.OrderSize)
        downFill := makerCrossFill(downBook, c.DownOrderPrice, c.OrderSize)
        switch {
        case upFill && downFill:
            fillBoth(c, now)
            c.ActualFirstLeg = "SIMULTANEOUS"
            c.PreferredFirstMatched = false
            c.CompletionMs = 0
            completeCycle(c, now)
            return true
        case upFill:
            fillFirst(c, "UP", now)
            changed = true
        case downFill:
            fillFirst(c, "DOWN", now)
            changed = true
        default:
            created, ok := parseTime(c.CreatedAt)
            if ok && now.Sub(created) >= cfg.OrderTTL {
                c.Status = PaperStatusExpiredNoFill
                c.Reason = "PAIR_ORDER_TTL_EXPIRED"
                c.UpdatedAt = now.Format(time.RFC3339Nano)
                return true
            }
            if !marketEnd.IsZero() && marketEnd.Sub(now) <= cfg.StopBeforeEnd {
                c.Status = PaperStatusExpiredNoFill
                c.Reason = "TOO_CLOSE_TO_MARKET_END_NO_FILL"
                c.UpdatedAt = now.Format(time.RFC3339Nano)
                return true
            }
            if changed { c.UpdatedAt = now.Format(time.RFC3339Nano) }
            return changed
        }
    }

    if c.Status != PaperStatusOneLegFilled {
        return changed
    }

    firstAt, _ := parseTime(c.FirstFillAt)
    if !firstAt.IsZero() {
        c.StrandedSeconds = math.Max(0, now.Sub(firstAt).Seconds())
    }

    if c.ActualFirstLeg == "UP" {
        if makerCrossFill(downBook, c.DownOrderPrice, c.OrderSize) {
            c.DownFillPrice = c.DownOrderPrice
            c.DownFilledAt = now.Format(time.RFC3339Nano)
            c.DeployedCost += c.OrderSize * c.DownFillPrice
            if !firstAt.IsZero() { c.CompletionMs = now.Sub(firstAt).Milliseconds() }
            completeCycle(c, now)
            return true
        }
    } else if c.ActualFirstLeg == "DOWN" {
        if makerCrossFill(upBook, c.UpOrderPrice, c.OrderSize) {
            c.UpFillPrice = c.UpOrderPrice
            c.UpFilledAt = now.Format(time.RFC3339Nano)
            c.DeployedCost += c.OrderSize * c.UpFillPrice
            if !firstAt.IsZero() { c.CompletionMs = now.Sub(firstAt).Milliseconds() }
            completeCycle(c, now)
            return true
        }
    }

    if (!marketEnd.IsZero() && marketEnd.Sub(now) <= cfg.StopBeforeEnd) || (!firstAt.IsZero() && now.Sub(firstAt) >= cfg.MaxStranded) {
        timeoutStranded(c, upBook, downBook, now)
        return true
    }

    if c.ActualFirstLeg == "UP" {
        if p, ok := completionReprice(c.DownOrderPrice, c.DownCompletionMax, downBook); ok && p > c.DownOrderPrice+1e-12 {
            c.DownOrderPrice = p
            c.Reprices++
            changed = true
        }
    } else if c.ActualFirstLeg == "DOWN" {
        if p, ok := completionReprice(c.UpOrderPrice, c.UpCompletionMax, upBook); ok && p > c.UpOrderPrice+1e-12 {
            c.UpOrderPrice = p
            c.Reprices++
            changed = true
        }
    }
    if changed { c.UpdatedAt = now.Format(time.RFC3339Nano) }
    return changed
}

func ClosePaperCycleForMarketChange(c *PaperCycle, now time.Time) bool {
    if c == nil || !c.IsOpen() { return false }
    now = now.UTC()
    if c.Status == PaperStatusRestingPair {
        c.Status = PaperStatusExpiredNoFill
        c.Reason = "MARKET_CHANGED_NO_FILL"
        c.UpdatedAt = now.Format(time.RFC3339Nano)
        return true
    }
    if c.ActualFirstLeg == "UP" {
        c.ExitMarkPrice = c.LastUpBestBid
        c.PaperPnL = c.OrderSize * (c.ExitMarkPrice - c.UpFillPrice)
    } else if c.ActualFirstLeg == "DOWN" {
        c.ExitMarkPrice = c.LastDownBestBid
        c.PaperPnL = c.OrderSize * (c.ExitMarkPrice - c.DownFillPrice)
    }
    c.Status = PaperStatusStrandedTimeout
    c.Reason = "MARKET_CHANGED_MARK_TO_LAST_BID"
    c.UpdatedAt = now.Format(time.RFC3339Nano)
    return true
}

func fillFirst(c *PaperCycle, side string, now time.Time) {
    c.Status = PaperStatusOneLegFilled
    c.Reason = "FIRST_LEG_FILLED"
    c.ActualFirstLeg = side
    c.PreferredFirstMatched = strings.EqualFold(c.PreferredFirstLeg, side)
    c.FirstFillAt = now.Format(time.RFC3339Nano)
    if side == "UP" {
        c.UpFillPrice = c.UpOrderPrice
        c.UpFilledAt = c.FirstFillAt
        c.DeployedCost = c.OrderSize * c.UpFillPrice
    } else {
        c.DownFillPrice = c.DownOrderPrice
        c.DownFilledAt = c.FirstFillAt
        c.DeployedCost = c.OrderSize * c.DownFillPrice
    }
    c.UpdatedAt = c.FirstFillAt
}

func fillBoth(c *PaperCycle, now time.Time) {
    ts := now.Format(time.RFC3339Nano)
    c.UpFillPrice, c.DownFillPrice = c.UpOrderPrice, c.DownOrderPrice
    c.UpFilledAt, c.DownFilledAt = ts, ts
    c.FirstFillAt = ts
    c.DeployedCost = c.OrderSize * (c.UpFillPrice + c.DownFillPrice)
}

func completeCycle(c *PaperCycle, now time.Time) {
    c.Status = PaperStatusCompleted
    c.Reason = "PAIR_COMPLETED_LOCKED"
    c.LockedPnL = c.OrderSize * (1 - c.UpFillPrice - c.DownFillPrice)
    c.PaperPnL = c.LockedPnL
    c.StrandedSeconds = float64(c.CompletionMs) / 1000
    c.UpdatedAt = now.UTC().Format(time.RFC3339Nano)
}

func timeoutStranded(c *PaperCycle, upBook, downBook polymarket.BookSnapshot, now time.Time) {
    if c.ActualFirstLeg == "UP" {
        c.ExitMarkPrice = upBook.BestBid
        c.PaperPnL = c.OrderSize * (c.ExitMarkPrice - c.UpFillPrice)
    } else {
        c.ExitMarkPrice = downBook.BestBid
        c.PaperPnL = c.OrderSize * (c.ExitMarkPrice - c.DownFillPrice)
    }
    c.Status = PaperStatusStrandedTimeout
    c.Reason = "STRANDED_TIMEOUT_MARK_TO_BID"
    c.UpdatedAt = now.UTC().Format(time.RFC3339Nano)
}

func makerCrossFill(book polymarket.BookSnapshot, orderPrice, orderSize float64) bool {
    if orderPrice <= 0 || orderSize <= 0 || !validBook(book) { return false }
    // Conservative: merely touching our hypothetical maker limit is not enough.
    // We require the public ask book to move strictly THROUGH the resting limit
    // and show at least one full order unit of sell liquidity below that limit.
    qty := 0.0
    for _, level := range book.Asks {
        if level.Price >= orderPrice-1e-12 { break }
        qty += level.Size
        if qty+1e-9 >= orderSize { return true }
    }
    return false
}

func completionReprice(current, economicCeiling float64, book polymarket.BookSnapshot) (float64, bool) {
    if current <= 0 || economicCeiling <= 0 || !validBook(book) { return 0, false }
    candidate := floorToTick(book.BestBid+book.TickSize, book.TickSize)
    postOnlyCeiling := floorToTick(book.BestAsk-book.TickSize, book.TickSize)
    if candidate > postOnlyCeiling { candidate = postOnlyCeiling }
    if candidate > economicCeiling { candidate = floorToTick(economicCeiling, book.TickSize) }
    if candidate <= current+1e-12 || candidate >= book.BestAsk-1e-12 { return current, false }
    return candidate, true
}

func updateLastBook(c *PaperCycle, upBook, downBook polymarket.BookSnapshot) bool {
    changed := c.LastUpBestBid != upBook.BestBid || c.LastUpBestAsk != upBook.BestAsk || c.LastDownBestBid != downBook.BestBid || c.LastDownBestAsk != downBook.BestAsk
    c.LastUpBestBid, c.LastUpBestAsk = upBook.BestBid, upBook.BestAsk
    c.LastDownBestBid, c.LastDownBestAsk = downBook.BestBid, downBook.BestAsk
    return changed
}

func parseTime(v string) (time.Time, bool) {
    t, err := time.Parse(time.RFC3339Nano, v)
    if err != nil { return time.Time{}, false }
    return t, true
}
''')

write('internal/arb/paper_test.go', r'''package arb

import (
    "math"
    "testing"
    "time"

    "pm-edge/internal/polymarket"
)

func paperBook(token string, bid, ask float64, askLevels ...polymarket.CLOBLevel) polymarket.BookSnapshot {
    if len(askLevels) == 0 { askLevels = []polymarket.CLOBLevel{{Price: ask, Size: 100}} }
    return polymarket.BookSnapshot{TokenID: token, BestBid: bid, BestAsk: ask, TickSize: .01, MinOrderSize: 5, Bids: []polymarket.CLOBLevel{{Price: bid, Size: 100}}, Asks: askLevels}
}

func paperSnap() *Snapshot {
    return &Snapshot{Timestamp:"2026-08-12T00:00:00Z", Timeframe:"5m", MarketSlug:"btc-updown-5m-1", Status:StatusCandidate, OrderSize:5, FirstLeg:"UP", UpMakerPrice:.41, DownMakerPrice:.54, UpBestBid:.40, UpBestAsk:.44, DownBestBid:.54, DownBestAsk:.58, UpCompletionMax:.43, DownCompletionMax:.56, PTBPUp:.8, PTBPDown:.2, PTBDecision:"UP", NetEdge:.048, TargetEdge:.02, OperationalBuffer:.002}
}

func TestPaperCycleDoesNotFillOnTouch(t *testing.T) {
    now := time.Date(2026,8,12,0,0,0,0,time.UTC)
    c := NewPaperCycle(paperSnap(), now)
    up := paperBook("up", .40, .41, polymarket.CLOBLevel{Price:.41, Size:100})
    down := paperBook("down", .53, .58)
    AdvancePaperCycle(c, up, down, now.Add(time.Second), now.Add(2*time.Minute), DefaultPaperConfig())
    if c.Status != PaperStatusRestingPair { t.Fatalf("touch must not fill: %+v", c) }
}

func TestPaperCycleFirstLegThenCompletion(t *testing.T) {
    now := time.Date(2026,8,12,0,0,0,0,time.UTC)
    c := NewPaperCycle(paperSnap(), now)
    upCross := paperBook("up", .39, .40, polymarket.CLOBLevel{Price:.40, Size:10})
    down := paperBook("down", .54, .58)
    if !AdvancePaperCycle(c, upCross, down, now.Add(time.Second), now.Add(2*time.Minute), DefaultPaperConfig()) { t.Fatal("expected first fill") }
    if c.Status != PaperStatusOneLegFilled || c.ActualFirstLeg != "UP" || !c.PreferredFirstMatched { t.Fatalf("bad first fill %+v", c) }
    // Reprice the remaining DOWN maker order from .54 to .55, still post-only and below .58 ask.
    AdvancePaperCycle(c, upCross, down, now.Add(2*time.Second), now.Add(2*time.Minute), DefaultPaperConfig())
    if math.Abs(c.DownOrderPrice-.55) > 1e-9 || c.Reprices != 1 { t.Fatalf("expected .55 reprice %+v", c) }
    downCross := paperBook("down", .53, .54, polymarket.CLOBLevel{Price:.54, Size:7})
    AdvancePaperCycle(c, upCross, downCross, now.Add(3*time.Second), now.Add(2*time.Minute), DefaultPaperConfig())
    if c.Status != PaperStatusCompleted { t.Fatalf("expected completed %+v", c) }
    want := 5 * (1 - .41 - .55)
    if math.Abs(c.PaperPnL-want) > 1e-9 { t.Fatalf("pnl got %.4f want %.4f", c.PaperPnL, want) }
    if c.CompletionMs != 2000 { t.Fatalf("completion ms=%d", c.CompletionMs) }
}

func TestPaperCycleRequiresFullCrossLiquidity(t *testing.T) {
    now := time.Date(2026,8,12,0,0,0,0,time.UTC)
    c := NewPaperCycle(paperSnap(), now)
    up := paperBook("up", .39, .40, polymarket.CLOBLevel{Price:.40, Size:2})
    down := paperBook("down", .54, .58)
    AdvancePaperCycle(c, up, down, now.Add(time.Second), now.Add(time.Minute), DefaultPaperConfig())
    if c.Status != PaperStatusRestingPair { t.Fatalf("partial cross liquidity must not fake full fill %+v", c) }
}

func TestPaperNoFillTTL(t *testing.T) {
    now := time.Date(2026,8,12,0,0,0,0,time.UTC)
    c := NewPaperCycle(paperSnap(), now)
    cfg := DefaultPaperConfig(); cfg.OrderTTL = 3*time.Second
    AdvancePaperCycle(c, paperBook("up",.40,.44), paperBook("down",.54,.58), now.Add(4*time.Second), now.Add(time.Minute), cfg)
    if c.Status != PaperStatusExpiredNoFill || c.PaperPnL != 0 { t.Fatalf("unexpected %+v", c) }
}

func TestPaperStrandedTimeoutMarksToBid(t *testing.T) {
    now := time.Date(2026,8,12,0,0,0,0,time.UTC)
    c := NewPaperCycle(paperSnap(), now)
    upCross := paperBook("up", .39, .40, polymarket.CLOBLevel{Price:.40, Size:10})
    down := paperBook("down", .54, .58)
    cfg := DefaultPaperConfig(); cfg.MaxStranded = 2*time.Second
    AdvancePaperCycle(c, upCross, down, now.Add(time.Second), now.Add(time.Minute), cfg)
    upLater := paperBook("up", .38, .42)
    AdvancePaperCycle(c, upLater, down, now.Add(4*time.Second), now.Add(time.Minute), cfg)
    if c.Status != PaperStatusStrandedTimeout { t.Fatalf("unexpected %+v", c) }
    want := 5*(.38-.41)
    if math.Abs(c.PaperPnL-want) > 1e-9 { t.Fatalf("pnl %.4f want %.4f", c.PaperPnL, want) }
}

func TestCompletionRepriceNeverBreaksCeilingOrPostOnly(t *testing.T) {
    book := paperBook("d", .55, .58)
    got, ok := completionReprice(.54, .56, book)
    if !ok || got != .56 { t.Fatalf("got %.4f ok=%v", got, ok) }
    book = paperBook("d", .56, .57)
    got, ok = completionReprice(.56, .56, book)
    if ok || got != .56 { t.Fatalf("must not exceed economic ceiling: %.4f %v", got, ok) }
}
''')

# Extend arb snapshot with local pair fetch latency diagnostics.
replace_once('internal/arb/engine.go', '    DownCompletionMax float64 `json:"downCompletionMax"`\n    UpCompletionMax   float64 `json:"upCompletionMax"`\n', '    DownCompletionMax float64 `json:"downCompletionMax"`\n    UpCompletionMax   float64 `json:"upCompletionMax"`\n    BookFetchMs       int64   `json:"bookFetchMs"`\n')

# Storage: paper-cycle schema + CRUD/stats.
replace_once('internal/storage/arb.go', '        CREATE INDEX IF NOT EXISTS idx_arb_market ON arb_snapshots(market_slug, timestamp DESC);\n    `)', '''        CREATE INDEX IF NOT EXISTS idx_arb_market ON arb_snapshots(market_slug, timestamp DESC);\n\n        CREATE TABLE IF NOT EXISTS arb_paper_cycles (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            timeframe TEXT NOT NULL,\n            market_slug TEXT NOT NULL,\n            status TEXT NOT NULL,\n            created_at TEXT NOT NULL,\n            updated_at TEXT NOT NULL,\n            first_leg TEXT NOT NULL DEFAULT '',\n            preferred_first_leg TEXT NOT NULL DEFAULT '',\n            preferred_first_matched INTEGER NOT NULL DEFAULT 0,\n            locked_pnl REAL NOT NULL DEFAULT 0,\n            paper_pnl REAL NOT NULL DEFAULT 0,\n            deployed_cost REAL NOT NULL DEFAULT 0,\n            completion_ms INTEGER NOT NULL DEFAULT 0,\n            payload TEXT NOT NULL\n        );\n        CREATE INDEX IF NOT EXISTS idx_arb_paper_tf_id ON arb_paper_cycles(timeframe, id DESC);\n        CREATE INDEX IF NOT EXISTS idx_arb_paper_open ON arb_paper_cycles(timeframe, status);\n    `)''')

append_storage = r'''

type ArbPaperStats struct {
    Timeframe               string  `json:"timeframe"`
    InitialBalance          float64 `json:"initialBalance"`
    CashBalance             float64 `json:"cashBalance"`
    NetPaperPnL             float64 `json:"netPaperPnl"`
    LockedPnL               float64 `json:"lockedPnl"`
    StrandedPnL             float64 `json:"strandedPnl"`
    DeployedCost            float64 `json:"deployedCost"`
    ReturnOnDeployedPct     float64 `json:"returnOnDeployedPct"`
    TotalCycles             int     `json:"totalCycles"`
    OpenCycles              int     `json:"openCycles"`
    CompletedCycles         int     `json:"completedCycles"`
    ExpiredNoFill           int     `json:"expiredNoFill"`
    StrandedTimeout         int     `json:"strandedTimeout"`
    FirstLegFilledCycles    int     `json:"firstLegFilledCycles"`
    PairCompletionRate      float64 `json:"pairCompletionRate"`
    PreferredFirstMatches   int     `json:"preferredFirstMatches"`
    PreferredFirstMatchRate float64 `json:"preferredFirstMatchRate"`
    AverageCompletionMs     float64 `json:"averageCompletionMs"`
    AverageLockedProfit     float64 `json:"averageLockedProfit"`
}

func (d *Database) InsertArbPaperCycle(c *arb.PaperCycle) error {
    if c == nil { return fmt.Errorf("nil arb paper cycle") }
    raw, err := json.Marshal(c); if err != nil { return err }
    res, err := d.db.Exec(`INSERT INTO arb_paper_cycles
        (timeframe,market_slug,status,created_at,updated_at,first_leg,preferred_first_leg,preferred_first_matched,locked_pnl,paper_pnl,deployed_cost,completion_ms,payload)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`, NormalizeTimeframe(c.Timeframe), c.MarketSlug, c.Status, c.CreatedAt, c.UpdatedAt, c.ActualFirstLeg, c.PreferredFirstLeg, arbBoolInt(c.PreferredFirstMatched), c.LockedPnL, c.PaperPnL, c.DeployedCost, c.CompletionMs, string(raw))
    if err != nil { return err }
    id, err := res.LastInsertId(); if err != nil { return err }
    c.ID = id
    raw, _ = json.Marshal(c)
    _, err = d.db.Exec(`UPDATE arb_paper_cycles SET payload=? WHERE id=?`, string(raw), id)
    return err
}

func (d *Database) UpdateArbPaperCycle(c *arb.PaperCycle) error {
    if c == nil || c.ID <= 0 { return fmt.Errorf("invalid arb paper cycle") }
    raw, err := json.Marshal(c); if err != nil { return err }
    _, err = d.db.Exec(`UPDATE arb_paper_cycles SET status=?,updated_at=?,first_leg=?,preferred_first_leg=?,preferred_first_matched=?,locked_pnl=?,paper_pnl=?,deployed_cost=?,completion_ms=?,payload=? WHERE id=?`,
        c.Status, c.UpdatedAt, c.ActualFirstLeg, c.PreferredFirstLeg, arbBoolInt(c.PreferredFirstMatched), c.LockedPnL, c.PaperPnL, c.DeployedCost, c.CompletionMs, string(raw), c.ID)
    return err
}

func (d *Database) GetOpenArbPaperCycle(tf string) (*arb.PaperCycle, error) {
    var raw string
    err := d.db.QueryRow(`SELECT payload FROM arb_paper_cycles WHERE timeframe=? AND status IN (?,?) ORDER BY id DESC LIMIT 1`, NormalizeTimeframe(tf), arb.PaperStatusRestingPair, arb.PaperStatusOneLegFilled).Scan(&raw)
    if err == sql.ErrNoRows { return nil, nil }
    if err != nil { return nil, err }
    var c arb.PaperCycle
    if err := json.Unmarshal([]byte(raw), &c); err != nil { return nil, err }
    return &c, nil
}

func (d *Database) GetArbPaperCyclesByTimeframe(limit int, tf string) ([]arb.PaperCycle, error) {
    if limit < 1 { limit = 50 }; if limit > 1000 { limit = 1000 }
    rows, err := d.db.Query(`SELECT payload FROM arb_paper_cycles WHERE timeframe=? ORDER BY id DESC LIMIT ?`, NormalizeTimeframe(tf), limit)
    if err != nil { return nil, err }; defer rows.Close()
    out := make([]arb.PaperCycle, 0, limit)
    for rows.Next() {
        var raw string; if err := rows.Scan(&raw); err != nil { return nil, err }
        var c arb.PaperCycle; if err := json.Unmarshal([]byte(raw), &c); err != nil { return nil, err }
        out = append(out, c)
    }
    return out, rows.Err()
}

func (d *Database) GetArbPaperStatsByTimeframe(initial float64, tf string) (ArbPaperStats, error) {
    tf = NormalizeTimeframe(tf)
    if initial <= 0 { initial = 1000 }
    out := ArbPaperStats{Timeframe: tf, InitialBalance: initial}
    err := d.db.QueryRow(`SELECT
        COUNT(*),
        COALESCE(SUM(CASE WHEN status IN (?,?) THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status=? THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status=? THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status=? THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN first_leg<>'' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN preferred_first_matched=1 THEN 1 ELSE 0 END),0),
        COALESCE(SUM(locked_pnl),0), COALESCE(SUM(paper_pnl),0), COALESCE(SUM(deployed_cost),0),
        COALESCE(AVG(CASE WHEN status=? THEN completion_ms END),0),
        COALESCE(AVG(CASE WHEN status=? THEN locked_pnl END),0),
        COALESCE(SUM(CASE WHEN status=? THEN paper_pnl ELSE 0 END),0)
        FROM arb_paper_cycles WHERE timeframe=?`,
        arb.PaperStatusRestingPair, arb.PaperStatusOneLegFilled, arb.PaperStatusCompleted, arb.PaperStatusExpiredNoFill, arb.PaperStatusStrandedTimeout,
        arb.PaperStatusCompleted, arb.PaperStatusCompleted, arb.PaperStatusStrandedTimeout, tf).Scan(
        &out.TotalCycles, &out.OpenCycles, &out.CompletedCycles, &out.ExpiredNoFill, &out.StrandedTimeout,
        &out.FirstLegFilledCycles, &out.PreferredFirstMatches, &out.LockedPnL, &out.NetPaperPnL, &out.DeployedCost,
        &out.AverageCompletionMs, &out.AverageLockedProfit, &out.StrandedPnL)
    if err != nil { return out, err }
    out.CashBalance = initial + out.NetPaperPnL
    resolvedAfterFirst := out.CompletedCycles + out.StrandedTimeout
    if resolvedAfterFirst > 0 { out.PairCompletionRate = float64(out.CompletedCycles)/float64(resolvedAfterFirst) }
    if out.FirstLegFilledCycles > 0 { out.PreferredFirstMatchRate = float64(out.PreferredFirstMatches)/float64(out.FirstLegFilledCycles) }
    if out.DeployedCost > 0 { out.ReturnOnDeployedPct = out.NetPaperPnL/out.DeployedCost*100 }
    return out, nil
}
'''
Path('internal/storage/arb.go').write_text(Path('internal/storage/arb.go').read_text() + append_storage)

# Storage tests for paper cycles.
Path('internal/storage/arb_test.go').write_text(Path('internal/storage/arb_test.go').read_text() + r'''

func TestArbPaperCycleStorageAndStats(t *testing.T) {
    db, err := NewDatabase(filepath.Join(t.TempDir(), "arb-paper.sqlite")); if err != nil { t.Fatal(err) }; defer db.Close()
    if err := db.EnsureArbSchema(); err != nil { t.Fatal(err) }
    c := &arb.PaperCycle{Timeframe:"5m", MarketSlug:"btc-updown-5m-1", CreatedAt:"2026-08-12T00:00:00Z", UpdatedAt:"2026-08-12T00:00:00Z", Status:arb.PaperStatusRestingPair, OrderSize:5, PreferredFirstLeg:"UP"}
    if err := db.InsertArbPaperCycle(c); err != nil { t.Fatal(err) }
    if c.ID <= 0 { t.Fatal("missing id") }
    open, err := db.GetOpenArbPaperCycle("5m"); if err != nil || open == nil || open.ID != c.ID { t.Fatalf("open=%+v err=%v", open, err) }
    c.Status = arb.PaperStatusCompleted; c.ActualFirstLeg="UP"; c.PreferredFirstMatched=true; c.LockedPnL=.20; c.PaperPnL=.20; c.DeployedCost=4.80; c.CompletionMs=1200; c.UpdatedAt="2026-08-12T00:00:02Z"
    if err := db.UpdateArbPaperCycle(c); err != nil { t.Fatal(err) }
    stats, err := db.GetArbPaperStatsByTimeframe(1000,"5m"); if err != nil { t.Fatal(err) }
    if stats.CompletedCycles != 1 || stats.OpenCycles != 0 || stats.NetPaperPnL != .20 || stats.CashBalance != 1000.20 || stats.PairCompletionRate != 1 { t.Fatalf("stats=%+v", stats) }
}
''')

# API endpoints.
Path('internal/api/arb.go').write_text(Path('internal/api/arb.go').read_text() + r'''

func (s *Server) handleArbPaperCycles(w http.ResponseWriter, r *http.Request) {
    rows, err := s.db.GetArbPaperCyclesByTimeframe(parseLimit(r, 50, 1000), normalizeTF(r))
    writeJSON(w, rows, err)
}

func (s *Server) handleArbPaperStats(w http.ResponseWriter, r *http.Request) {
    stats, err := s.db.GetArbPaperStatsByTimeframe(s.paperInitialBalance, normalizeTF(r))
    writeJSON(w, stats, err)
}
''')
replace_once('internal/api/server.go', '    mux.HandleFunc("/api/arb/stats", s.cors(s.handleArbStats))\n', '    mux.HandleFunc("/api/arb/stats", s.cors(s.handleArbStats))\n    mux.HandleFunc("/api/arb/paper/cycles", s.cors(s.handleArbPaperCycles))\n    mux.HandleFunc("/api/arb/paper/stats", s.cors(s.handleArbPaperStats))\n')

# Config.
replace_once('internal/config/config.go', '    ArbMaxStrandedUnits   int\n', '    ArbMaxStrandedUnits   int\n    ArbMaxBookFetchMs     int\n    ArbPaperEnabled       bool\n    ArbPaperOrderTTLSec   int\n    ArbPaperMaxStrandedSec int\n    ArbPaperStopBeforeEndSec int\n')
replace_once('internal/config/config.go', '        ArbMaxStrandedUnits:       envInt("ARB_MAX_STRANDED_UNITS", 1),\n', '        ArbMaxStrandedUnits:       envInt("ARB_MAX_STRANDED_UNITS", 1),\n        ArbMaxBookFetchMs:          envInt("ARB_MAX_BOOK_FETCH_MS", 1000),\n        ArbPaperEnabled:            envBool("ARB_PAPER_ENABLED", true),\n        ArbPaperOrderTTLSec:        envInt("ARB_PAPER_ORDER_TTL_SEC", 12),\n        ArbPaperMaxStrandedSec:     envInt("ARB_PAPER_MAX_STRANDED_SEC", 20),\n        ArbPaperStopBeforeEndSec:   envInt("ARB_PAPER_STOP_BEFORE_END_SEC", 12),\n')

# Replace arb runtime: concurrent pair books + paper state machine.
write('cmd/pm-edge/arb_shadow.go', r'''package main

import (
    "sync"
    "sync/atomic"
    "time"

    "go.uber.org/zap"
    "pm-edge/internal/arb"
    "pm-edge/internal/config"
    "pm-edge/internal/engine"
    "pm-edge/internal/polymarket"
    "pm-edge/internal/storage"
    "pm-edge/internal/util"
)

type arbShadowRuntime struct {
    engine              *arb.Engine
    db                  *storage.Database
    pmClient            *polymarket.Client
    busy                atomic.Bool
    paperEnabled        bool
    paperCfg            arb.PaperConfig
    paperInitialBalance float64
    maxBookFetchMs      int64
    active              *arb.PaperCycle
}

func newArbShadowRuntime(tf string, cfg *config.Config, db *storage.Database, pmClient *polymarket.Client, enabled bool) *arbShadowRuntime {
    r := &arbShadowRuntime{engine: arb.NewEngine(arb.Config{
        Timeframe: tf, Enabled: enabled && cfg.ArbShadowEnabled,
        TargetEdge: cfg.ArbTargetEdge, OperationalBuffer: cfg.ArbOperationalBuffer,
        UncertaintyPenalty: cfg.ArbUncertaintyPenalty, MaxStrandedUnits: cfg.ArbMaxStrandedUnits,
    }), db: db, pmClient: pmClient, paperEnabled: enabled && cfg.ArbPaperEnabled,
        paperCfg: arb.PaperConfig{Enabled: enabled && cfg.ArbPaperEnabled, OrderTTL: time.Duration(cfg.ArbPaperOrderTTLSec)*time.Second, MaxStranded: time.Duration(cfg.ArbPaperMaxStrandedSec)*time.Second, StopBeforeEnd: time.Duration(cfg.ArbPaperStopBeforeEndSec)*time.Second},
        paperInitialBalance: cfg.PaperInitialBalance, maxBookFetchMs: int64(cfg.ArbMaxBookFetchMs)}
    if r.maxBookFetchMs <= 0 { r.maxBookFetchMs = 1000 }
    if r.paperEnabled {
        if open, err := db.GetOpenArbPaperCycle(tf); err != nil {
            util.Logger.Warn("Maker arb paper open-cycle restore failed", zap.String("tf", tf), zap.Error(err))
        } else if open != nil {
            r.active = open
            util.Logger.Info("MAKER ARB PAPER CYCLE RESTORED", zap.String("tf", tf), zap.Int64("id", open.ID), zap.String("market", open.MarketSlug), zap.String("status", open.Status))
        }
    }
    return r
}

func (r *arbShadowRuntime) Submit(res *engine.EvaluationResult, market *polymarket.Market) {
    if r == nil || r.engine == nil || !r.engine.Enabled() || res == nil || market == nil { return }
    if !r.busy.CompareAndSwap(false, true) { return }
    rc := *res
    mc := *market
    mc.Tokens = append([]polymarket.Token(nil), market.Tokens...)
    go func() {
        defer r.busy.Store(false)
        upID, ok := polymarket.TokenIDForOutcome(&mc, "UP"); if !ok { util.Logger.Warn("Maker arb missing UP token", zap.String("market", mc.Slug)); return }
        downID, ok := polymarket.TokenIDForOutcome(&mc, "DOWN"); if !ok { util.Logger.Warn("Maker arb missing DOWN token", zap.String("market", mc.Slug)); return }

        started := time.Now()
        upBook, downBook, err := r.fetchPairBooks(upID, downID)
        fetchMs := time.Since(started).Milliseconds()
        if err != nil { util.Logger.Warn("Maker arb pair book unavailable", zap.Error(err), zap.String("market", mc.Slug)); return }
        snap := r.engine.Evaluate(&rc, &mc, upBook, downBook)
        if snap == nil { return }
        snap.BookFetchMs = fetchMs
        if snap.Status == arb.StatusCandidate && fetchMs > r.maxBookFetchMs {
            snap.Status = arb.StatusBlocked
            snap.Reason = "BOOK_FETCH_TOO_SLOW"
        }
        if err := r.db.InsertArbSnapshot(snap); err != nil { util.Logger.Error("Maker arb snapshot store failed", zap.Error(err)); return }

        now := time.Now().UTC()
        r.processPaper(snap, upBook, downBook, &mc, now)
        if snap.Status == arb.StatusCandidate {
            util.Logger.Info("MAKER ARB SHADOW CANDIDATE", zap.String("market", snap.MarketSlug), zap.Float64("up", snap.UpMakerPrice), zap.Float64("down", snap.DownMakerPrice), zap.Float64("netEdge", snap.NetEdge), zap.Float64("shares", snap.OrderSize), zap.String("safeFirstLeg", snap.FirstLeg), zap.Int64("bookFetchMs", snap.BookFetchMs))
        }
    }()
}

func (r *arbShadowRuntime) fetchPairBooks(upID, downID string) (polymarket.BookSnapshot, polymarket.BookSnapshot, error) {
    type result struct { book polymarket.BookSnapshot; err error }
    var wg sync.WaitGroup
    wg.Add(2)
    upCh, downCh := make(chan result,1), make(chan result,1)
    go func(){ defer wg.Done(); b,e:=r.pmClient.FetchBookSnapshot(upID); upCh<-result{b,e} }()
    go func(){ defer wg.Done(); b,e:=r.pmClient.FetchBookSnapshot(downID); downCh<-result{b,e} }()
    wg.Wait(); close(upCh); close(downCh)
    u,d := <-upCh, <-downCh
    if u.err != nil { return polymarket.BookSnapshot{}, polymarket.BookSnapshot{}, u.err }
    if d.err != nil { return polymarket.BookSnapshot{}, polymarket.BookSnapshot{}, d.err }
    return u.book, d.book, nil
}

func (r *arbShadowRuntime) processPaper(snap *arb.Snapshot, upBook, downBook polymarket.BookSnapshot, market *polymarket.Market, now time.Time) {
    if !r.paperEnabled || snap == nil || market == nil { return }
    if r.active != nil && r.active.MarketSlug != market.Slug {
        if arb.ClosePaperCycleForMarketChange(r.active, now) {
            if err := r.db.UpdateArbPaperCycle(r.active); err != nil { util.Logger.Error("Maker arb paper market-change close failed", zap.Error(err)); return }
        }
        r.logPaperTerminal(r.active)
        r.active = nil
    }
    if r.active != nil {
        if arb.AdvancePaperCycle(r.active, upBook, downBook, now, market.EndTime, r.paperCfg) {
            if err := r.db.UpdateArbPaperCycle(r.active); err != nil { util.Logger.Error("Maker arb paper update failed", zap.Error(err)); return }
        }
        if r.active.IsTerminal() {
            r.logPaperTerminal(r.active)
            r.active = nil
        }
    }
    if r.active != nil || snap.Status != arb.StatusCandidate { return }
    stats, err := r.db.GetArbPaperStatsByTimeframe(r.paperInitialBalance, snap.Timeframe)
    if err != nil { util.Logger.Warn("Maker arb paper balance unavailable", zap.Error(err)); return }
    required := snap.OrderSize * (snap.UpMakerPrice + snap.DownMakerPrice)
    if stats.CashBalance+1e-9 < required { util.Logger.Warn("Maker arb paper insufficient balance", zap.Float64("cash", stats.CashBalance), zap.Float64("required", required)); return }
    c := arb.NewPaperCycle(snap, now)
    if c == nil { return }
    if err := r.db.InsertArbPaperCycle(c); err != nil { util.Logger.Error("Maker arb paper cycle create failed", zap.Error(err)); return }
    r.active = c
    util.Logger.Info("MAKER ARB PAPER PAIR POSTED", zap.Int64("id", c.ID), zap.String("market", c.MarketSlug), zap.Float64("up", c.UpOrderPrice), zap.Float64("down", c.DownOrderPrice), zap.Float64("shares", c.OrderSize), zap.String("preferredFirst", c.PreferredFirstLeg))
}

func (r *arbShadowRuntime) logPaperTerminal(c *arb.PaperCycle) {
    if c == nil { return }
    util.Logger.Info("MAKER ARB PAPER CYCLE CLOSED", zap.Int64("id", c.ID), zap.String("market", c.MarketSlug), zap.String("status", c.Status), zap.String("firstLeg", c.ActualFirstLeg), zap.Bool("preferredMatched", c.PreferredFirstMatched), zap.Int64("completionMs", c.CompletionMs), zap.Float64("lockedPnL", c.LockedPnL), zap.Float64("paperPnL", c.PaperPnL), zap.Int("reprices", c.Reprices), zap.String("reason", c.Reason))
}
''')

# env + docs
Path('.env.example').write_text(Path('.env.example').read_text() + r'''

# Maker arbitrage paper executor (still NO live orders)
ARB_MAX_BOOK_FETCH_MS=1000
ARB_PAPER_ENABLED=true
ARB_PAPER_ORDER_TTL_SEC=12
ARB_PAPER_MAX_STRANDED_SEC=20
ARB_PAPER_STOP_BEFORE_END_SEC=12
''')
Path('docs/maker-arb-shadow.md').write_text(Path('docs/maker-arb-shadow.md').read_text() + r'''

## Paper executor
`ARB_PAPER_ENABLED=true` adds a separate maker-arbitrage paper portfolio. It never signs or submits an order. A resting maker BUY is counted as filled only when a later public CLOB snapshot moves strictly through its limit and exposes at least the full configured order size in ask liquidity below that limit. A mere touch is not a fill. After one leg fills, only the opposite leg may be repriced, never above the economic completion ceiling and never through the current ask. If the second leg is not completed before the stranded timeout/market-end guard, the cycle is closed using the filled leg's current best bid as a conservative mark-to-market exit estimate.

Endpoints: `/api/arb/paper/stats?tf=5m|15m` and `/api/arb/paper/cycles?tf=5m|15m`.
''')

# Dashboard: add paper portfolio inside arb card after arb grid.
replace_once('web/static/index.html', '    </div>\n    <div class="scroll" style="margin-top:14px"><table><thead><tr><th>Saat</th><th>Durum</th><th>UP</th><th>DOWN</th><th>Net Avantaj</th><th>Pay</th><th>Güvenli Bacak</th><th>UP Tek Bacak EV</th><th>DOWN Tek Bacak EV</th><th>Neden</th></tr></thead><tbody id="arbBody"><tr><td colspan="10">Arbitraj gölge verisi bekleniyor...</td></tr></tbody></table></div>\n  </div>', '''    </div>\n    <div class="grid4" style="margin-top:14px">\n      <div class="mini"><span>Arbitraj Paper Bakiyesi</span><strong id="arbPaperBalance">$1,000.00</strong></div>\n      <div class="mini"><span>Çift Bacak Tamamlanma Oranı</span><strong id="arbPaperCompletion">—</strong></div>\n      <div class="mini"><span>Kilitli Kâr / Ters Bacak K/Z</span><strong id="arbPaperSplitPnl">—</strong></div>\n      <div class="mini"><span>Net Arb Paper K/Z</span><strong id="arbPaperNetPnl">$0.00</strong></div>\n      <div class="mini"><span>Cycle / Açık / Tamamlandı</span><strong id="arbPaperCounts">0 / 0 / 0</strong></div>\n      <div class="mini"><span>Tercih Edilen İlk Bacak İsabeti</span><strong id="arbPaperFirstMatch">—</strong></div>\n      <div class="mini"><span>Ortalama Karşı Bacak Tamamlama</span><strong id="arbPaperCompletionMs">—</strong></div>\n      <div class="mini"><span>Fill Simülasyonu</span><strong style="font-size:12px">Konservatif: fiyat limitin altından geçmeli + tam pay kadar çapraz likidite</strong></div>\n    </div>\n    <div class="scroll" style="margin-top:14px"><table><thead><tr><th>Saat</th><th>Durum</th><th>UP</th><th>DOWN</th><th>Net Avantaj</th><th>Pay</th><th>Güvenli Bacak</th><th>UP Tek Bacak EV</th><th>DOWN Tek Bacak EV</th><th>Neden</th></tr></thead><tbody id="arbBody"><tr><td colspan="10">Arbitraj gölge verisi bekleniyor...</td></tr></tbody></table></div>\n    <div class="scroll" style="margin-top:14px"><table><thead><tr><th>Başlangıç</th><th>Durum</th><th>Tercih</th><th>İlk Dolan</th><th>UP Emir/Dolum</th><th>DOWN Emir/Dolum</th><th>Pay</th><th>Reprice</th><th>Tamamlama</th><th>K/Z</th><th>Neden</th></tr></thead><tbody id="arbPaperBody"><tr><td colspan="11">Arbitraj paper cycle verisi bekleniyor...</td></tr></tbody></table></div>\n  </div>''')

# Reason translations.
replace_once('web/static/index.html', "'PAIR_EDGE_BELOW_TARGET':'Net maker arbitraj avantajı hedefin altında','BOOK_NOT_READY':'UP/DOWN emir defteri hazır değil','BLOCKED':'ENGELLENDİ','READY':'HAZIR'", "'PAIR_EDGE_BELOW_TARGET':'Net maker arbitraj avantajı hedefin altında','BOOK_NOT_READY':'UP/DOWN emir defteri hazır değil','BOOK_FETCH_TOO_SLOW':'UP/DOWN emir defteri birlikte yeterince hızlı alınamadı','PAIR_ORDER_TTL_EXPIRED':'Maker çift emri süre içinde dolmadı','TOO_CLOSE_TO_MARKET_END_NO_FILL':'Kapanışa çok yaklaşıldı, emirler dolmadan iptal sayıldı','FIRST_LEG_FILLED':'İlk bacak doldu, karşı bacak bekleniyor','PAIR_COMPLETED_LOCKED':'İki bacak tamamlandı, arbitraj kârı kilitlendi','STRANDED_TIMEOUT_MARK_TO_BID':'Karşı bacak dolmadı; tek bacak mevcut alış fiyatından konservatif kapatıldı','MARKET_CHANGED_NO_FILL':'Piyasa değişti, dolmayan emirler iptal sayıldı','MARKET_CHANGED_MARK_TO_LAST_BID':'Piyasa değişti; tek bacak son alış fiyatından işaretlendi','BLOCKED':'ENGELLENDİ','READY':'HAZIR'")

# Add JS paper updater after updateArbHistory.
marker = "async function updatePaper(){\n"
js = r'''async function updateArbPaper(){
  const [s,rows]=await Promise.all([getJSON('/api/arb/paper/stats?tf='+activeTf),getJSON('/api/arb/paper/cycles?limit=30&tf='+activeTf)]);
  document.getElementById('arbPaperBalance').textContent=usd(s.cashBalance||0);
  document.getElementById('arbPaperCompletion').textContent=`${pct(s.pairCompletionRate||0,1)} · ${s.completedCycles||0}/${(s.completedCycles||0)+(s.strandedTimeout||0)}`;
  document.getElementById('arbPaperSplitPnl').textContent=`${usd(s.lockedPnl||0)} / ${usd(s.strandedPnl||0)}`;
  const net=document.getElementById('arbPaperNetPnl');net.textContent=usd(s.netPaperPnl||0);net.className=signClass(s.netPaperPnl||0);
  document.getElementById('arbPaperCounts').textContent=`${s.totalCycles||0} / ${s.openCycles||0} / ${s.completedCycles||0}`;
  document.getElementById('arbPaperFirstMatch').textContent=`${pct(s.preferredFirstMatchRate||0,1)} · ${s.preferredFirstMatches||0}/${s.firstLegFilledCycles||0}`;
  document.getElementById('arbPaperCompletionMs').textContent=s.averageCompletionMs?`${Number(s.averageCompletionMs).toFixed(0)} ms`:'—';
  const body=document.getElementById('arbPaperBody');if(!rows||!rows.length){body.innerHTML='<tr><td colspan="11">Arbitraj paper cycle verisi bekleniyor...</td></tr>';return}
  const st=x=>x==='COMPLETED'?chip('TAMAMLANDI','fresh'):x==='ONE_LEG_FILLED'?chip('TEK BACAK','warn'):x==='RESTING_PAIR'?chip('EMİRLER BEKLİYOR','open'):x==='STRANDED_TIMEOUT'?chip('TERS BACAK ZARARI','down'):chip('DOLMADI','neutral');
  body.innerHTML=rows.map(c=>`<tr><td>${timeOnly(c.createdAt)}</td><td>${st(c.status)}</td><td>${c.preferredFirstLeg?directionText(c.preferredFirstLeg):'—'}</td><td>${c.actualFirstLeg?directionText(c.actualFirstLeg):'—'}</td><td>${Number(c.upOrderPrice||0).toFixed(3)} / ${c.upFillPrice?Number(c.upFillPrice).toFixed(3):'—'}</td><td>${Number(c.downOrderPrice||0).toFixed(3)} / ${c.downFillPrice?Number(c.downFillPrice).toFixed(3):'—'}</td><td>${Number(c.orderSize||0).toFixed(2)}</td><td>${c.reprices||0}</td><td>${c.completionMs?c.completionMs+' ms':'—'}</td><td class="${signClass(c.paperPnl||0)}">${usd(c.paperPnl||0)}</td><td>${reasonTr(c.reason)}</td></tr>`).join('');
}
'''
replace_once('web/static/index.html', marker, js + marker)
replace_once('web/static/index.html', 'await Promise.all([updateHistory(),updatePaper(),updateHedge(),updateComparison(),updateArbHistory()])', 'await Promise.all([updateHistory(),updatePaper(),updateHedge(),updateComparison(),updateArbHistory(),updateArbPaper()])')
