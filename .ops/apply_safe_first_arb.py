from pathlib import Path
import re


def replace_once(path, old, new):
    p = Path(path)
    s = p.read_text()
    if old not in s:
        raise SystemExit(f'marker not found in {path}: {old[:120]!r}')
    p.write_text(s.replace(old, new, 1))

# Prevent pre-completion trades in the same batch from filling the second leg.
replace_once('internal/arb/paper.go', '''\t\t\tc.SecondQueueAhead = buyQueueAhead(secondBook, c.SecondOrderPrice)\n\t\t\tchanged = true\n\t\t} else if c.FirstFilledShares > 0 {''', '''\t\t\tc.SecondQueueAhead = buyQueueAhead(secondBook, c.SecondOrderPrice)\n\t\t\tc.LastTradeSeq = latestSeq\n\t\t\tc.UpdatedAt = now.Format(time.RFC3339Nano)\n\t\t\t// The completion order did not exist during the trade batch that filled\n\t\t\t// the first leg. Start evaluating it only from the next batch.\n\t\t\treturn true\n\t\t} else if c.FirstFilledShares > 0 {''')

# Config: paper research edge is intentionally lower than future live threshold.
p = Path('internal/config/config.go')
s = p.read_text()
s = s.replace('\tArbMaxStrandedUnits      int\n\tArbMaxBookFetchMs        int\n', '\tArbMaxStrandedUnits      int\n\tArbPaperMinEdge          float64\n\tArbMaxBookFetchMs        int\n\tArbTradeStreamMaxAgeSec  int\n')
s = s.replace('\t\tArbMaxStrandedUnits:       envInt("ARB_MAX_STRANDED_UNITS", 1),\n\t\tArbMaxBookFetchMs:         envInt("ARB_MAX_BOOK_FETCH_MS", 1000),\n', '\t\tArbMaxStrandedUnits:       envInt("ARB_MAX_STRANDED_UNITS", 1),\n\t\tArbPaperMinEdge:           envFloat("ARB_PAPER_MIN_EDGE", 0.002),\n\t\tArbMaxBookFetchMs:         envInt("ARB_MAX_BOOK_FETCH_MS", 1000),\n\t\tArbTradeStreamMaxAgeSec:   envInt("ARB_TRADE_STREAM_MAX_AGE_SEC", 20),\n')
p.write_text(s)

# Runtime: public Polymarket trade stream + safe-first sequential paper lifecycle.
Path('cmd/pm-edge/arb_shadow.go').write_text(r'''package main

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
    tradeStream         *polymarket.MarketTradeStream
    busy                atomic.Bool
    paperEnabled        bool
    paperCfg            arb.PaperConfig
    paperInitialBalance float64
    maxBookFetchMs      int64
    tradeStreamMaxAge   time.Duration
    active              *arb.PaperCycle
}

func newArbShadowRuntime(tf string, cfg *config.Config, db *storage.Database, pmClient *polymarket.Client, enabled bool) *arbShadowRuntime {
    stream := polymarket.NewMarketTradeStream()
    r := &arbShadowRuntime{engine: arb.NewEngine(arb.Config{
        Timeframe: tf, Enabled: enabled && cfg.ArbShadowEnabled,
        TargetEdge: cfg.ArbTargetEdge, PaperMinEdge: cfg.ArbPaperMinEdge,
        OperationalBuffer: cfg.ArbOperationalBuffer,
        UncertaintyPenalty: cfg.ArbUncertaintyPenalty, MaxStrandedUnits: cfg.ArbMaxStrandedUnits,
    }), db: db, pmClient: pmClient, tradeStream: stream, paperEnabled: enabled && cfg.ArbPaperEnabled,
        paperCfg: arb.PaperConfig{Enabled: enabled && cfg.ArbPaperEnabled, OrderTTL: time.Duration(cfg.ArbPaperOrderTTLSec)*time.Second, MaxStranded: time.Duration(cfg.ArbPaperMaxStrandedSec)*time.Second, StopBeforeEnd: time.Duration(cfg.ArbPaperStopBeforeEndSec)*time.Second},
        paperInitialBalance: cfg.PaperInitialBalance, maxBookFetchMs: int64(cfg.ArbMaxBookFetchMs), tradeStreamMaxAge: time.Duration(cfg.ArbTradeStreamMaxAgeSec)*time.Second}
    if r.maxBookFetchMs <= 0 { r.maxBookFetchMs = 1000 }
    if r.tradeStreamMaxAge <= 0 { r.tradeStreamMaxAge = 20*time.Second }
    if enabled && cfg.ArbShadowEnabled { stream.Start() }
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
        r.tradeStream.SetAssets([]string{upID, downID})

        started := time.Now()
        upBook, downBook, err := r.fetchPairBooks(upID, downID)
        fetchMs := time.Since(started).Milliseconds()
        if err != nil { util.Logger.Warn("Maker arb pair book unavailable", zap.Error(err), zap.String("market", mc.Slug)); return }
        snap := r.engine.Evaluate(&rc, &mc, upBook, downBook)
        if snap == nil { return }
        snap.BookFetchMs = fetchMs
        if snap.Status != arb.StatusBlocked && fetchMs > r.maxBookFetchMs {
            snap.Status = arb.StatusBlocked
            snap.Reason = "BOOK_FETCH_TOO_SLOW"
        }
        if err := r.db.InsertArbSnapshot(snap); err != nil { util.Logger.Error("Maker arb snapshot store failed", zap.Error(err)); return }

        now := time.Now().UTC()
        r.processPaper(snap, upBook, downBook, &mc, now)
        if snap.Status == arb.StatusCandidate || snap.Status == arb.StatusPaperCandidate {
            util.Logger.Info("MAKER ARB SAFE-FIRST SHADOW", zap.String("market", snap.MarketSlug), zap.String("status", snap.Status), zap.String("firstLeg", snap.FirstLeg), zap.Float64("firstQueueAhead", snap.FirstLegQueueAhead), zap.Float64("up", snap.UpMakerPrice), zap.Float64("down", snap.DownMakerPrice), zap.Float64("netEdge", snap.NetEdge), zap.Float64("paperMinEdge", snap.PaperMinEdge), zap.Float64("liveTargetEdge", snap.TargetEdge), zap.Int64("bookFetchMs", snap.BookFetchMs))
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
        r.logPaperTerminal(r.active); r.active=nil
    }

    if r.active != nil {
        if r.tradeStream.GapCount() != r.active.StreamGapCount {
            if arb.InvalidatePaperCycleDataGap(r.active, now) {
                if err := r.db.UpdateArbPaperCycle(r.active); err != nil { util.Logger.Error("Maker arb data-gap invalidation failed", zap.Error(err)); return }
            }
        } else if r.tradeStream.Healthy(r.tradeStreamMaxAge) {
            trades, latest := r.tradeStream.TradesAfter(r.active.LastTradeSeq)
            if arb.AdvancePaperCycle(r.active, upBook, downBook, trades, latest, now, market.EndTime, r.paperCfg) {
                if err := r.db.UpdateArbPaperCycle(r.active); err != nil { util.Logger.Error("Maker arb paper update failed", zap.Error(err)); return }
            }
        }
        if r.active.IsTerminal() { r.logPaperTerminal(r.active); r.active=nil }
    }
    if r.active != nil || snap.Status == arb.StatusBlocked || !snap.PaperEdgePass || !snap.PTBReady { return }
    if !r.tradeStream.Healthy(r.tradeStreamMaxAge) { return }

    stats, err := r.db.GetArbPaperStatsByTimeframe(r.paperInitialBalance, snap.Timeframe)
    if err != nil { util.Logger.Warn("Maker arb paper balance unavailable", zap.Error(err)); return }
    required := snap.OrderSize * (snap.UpMakerPrice + snap.DownMakerPrice)
    if stats.CashBalance+1e-9 < required { util.Logger.Warn("Maker arb paper insufficient balance", zap.Float64("cash", stats.CashBalance), zap.Float64("required", required)); return }
    c := arb.NewPaperCycle(snap, upBook, downBook, now, r.tradeStream.LastSeq(), r.tradeStream.GapCount())
    if c == nil { return }
    if err := r.db.InsertArbPaperCycle(c); err != nil { util.Logger.Error("Maker arb paper cycle create failed", zap.Error(err)); return }
    r.active = c
    util.Logger.Info("MAKER ARB PAPER SAFE-FIRST POSTED", zap.Int64("id", c.ID), zap.String("market", c.MarketSlug), zap.String("firstSide", c.FirstOrderSide), zap.Float64("firstPrice", c.FirstOrderPrice), zap.Float64("queueAhead", c.FirstQueueAhead), zap.String("completionSide", c.SecondOrderSide), zap.Float64("plannedCompletion", c.SecondOrderPrice), zap.Float64("shares", c.OrderSize), zap.Float64("entryNetEdge", c.EntryNetEdge))
}

func (r *arbShadowRuntime) logPaperTerminal(c *arb.PaperCycle) {
    if c == nil { return }
    util.Logger.Info("MAKER ARB PAPER CYCLE CLOSED", zap.Int64("id", c.ID), zap.String("market", c.MarketSlug), zap.String("status", c.Status), zap.String("firstLeg", c.ActualFirstLeg), zap.Float64("firstFilled", c.FirstFilledShares), zap.Float64("secondFilled", c.SecondFilledShares), zap.Int64("completionMs", c.CompletionMs), zap.Float64("lockedPnL", c.LockedPnL), zap.Float64("paperPnL", c.PaperPnL), zap.Int("reprices", c.Reprices), zap.String("reason", c.Reason))
}
''')

# Storage recognizes all new open phases and excludes data-gap cycles from PnL inference.
p = Path('internal/storage/arb.go')
s = p.read_text()
s = s.replace("COALESCE(SUM(CASE WHEN status='CANDIDATE' THEN 1 ELSE 0 END),0),\n        COALESCE(AVG(CASE WHEN status='CANDIDATE' THEN net_edge END),0)", "COALESCE(SUM(CASE WHEN status IN ('CANDIDATE','PAPER_CANDIDATE') THEN 1 ELSE 0 END),0),\n        COALESCE(AVG(CASE WHEN status IN ('CANDIDATE','PAPER_CANDIDATE') THEN net_edge END),0)")
s = s.replace('\tAverageLockedProfit     float64 `json:"averageLockedProfit"`\n', '\tAverageLockedProfit     float64 `json:"averageLockedProfit"`\n\tInvalidDataGap          int     `json:"invalidDataGap"`\n')
start = s.index('func (d *Database) GetOpenArbPaperCycle')
end = s.index('\nfunc (d *Database) GetArbPaperCyclesByTimeframe', start)
s = s[:start] + r'''func (d *Database) GetOpenArbPaperCycle(tf string) (*arb.PaperCycle, error) {
    var raw string
    err := d.db.QueryRow(`SELECT payload FROM arb_paper_cycles WHERE timeframe=? AND status IN ('RESTING_FIRST','FIRST_PARTIAL','COMPLETING','COMPLETION_PARTIAL','RESTING_PAIR','ONE_LEG_FILLED') ORDER BY id DESC LIMIT 1`, NormalizeTimeframe(tf)).Scan(&raw)
    if err == sql.ErrNoRows { return nil, nil }
    if err != nil { return nil, err }
    var c arb.PaperCycle
    if err := json.Unmarshal([]byte(raw), &c); err != nil { return nil, err }
    return &c, nil
}
''' + s[end:]
start = s.index('func (d *Database) GetArbPaperStatsByTimeframe')
s = s[:start] + r'''func (d *Database) GetArbPaperStatsByTimeframe(initial float64, tf string) (ArbPaperStats, error) {
    tf = NormalizeTimeframe(tf)
    if initial <= 0 { initial = 1000 }
    out := ArbPaperStats{Timeframe: tf, InitialBalance: initial}
    err := d.db.QueryRow(`SELECT
        COUNT(*),
        COALESCE(SUM(CASE WHEN status IN ('RESTING_FIRST','FIRST_PARTIAL','COMPLETING','COMPLETION_PARTIAL','RESTING_PAIR','ONE_LEG_FILLED') THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status=? THEN 1 ELSE 0 END),0),
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
        arb.PaperStatusCompleted, arb.PaperStatusExpiredNoFill, arb.PaperStatusStrandedTimeout, arb.PaperStatusDataGapInvalid,
        arb.PaperStatusCompleted, arb.PaperStatusCompleted, arb.PaperStatusStrandedTimeout, tf).Scan(
        &out.TotalCycles, &out.OpenCycles, &out.CompletedCycles, &out.ExpiredNoFill, &out.StrandedTimeout, &out.InvalidDataGap,
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
p.write_text(s)

# Paper state machine tests.
Path('internal/arb/paper_test.go').write_text(r'''package arb

import (
    "math"
    "testing"
    "time"

    "pm-edge/internal/polymarket"
)

func paperBook(token string, bid, ask float64, bidSize float64) polymarket.BookSnapshot {
    if bidSize <= 0 { bidSize = 100 }
    return polymarket.BookSnapshot{TokenID:token, BestBid:bid, BestAsk:ask, TickSize:.01, MinOrderSize:5,
        Bids:[]polymarket.CLOBLevel{{Price:bid,Size:bidSize}}, Asks:[]polymarket.CLOBLevel{{Price:ask,Size:100}}}
}

func paperSnap() *Snapshot {
    return &Snapshot{Timestamp:"2026-08-12T00:00:00Z", Timeframe:"5m", MarketSlug:"btc-updown-5m-1", Status:StatusPaperCandidate,
        OrderSize:5, FirstLeg:"UP", UpTokenID:"up", DownTokenID:"down", UpMakerPrice:.41, DownMakerPrice:.54,
        UpBestBid:.40, UpBestAsk:.44, DownBestBid:.53, DownBestAsk:.58, UpCompletionMax:.43, DownCompletionMax:.56,
        PTBReady:true, PTBPUp:.8, PTBPDown:.2, PTBDecision:"UP", NetEdge:.048, TargetEdge:.02, PaperMinEdge:.002,
        OperationalBuffer:.002, PaperEdgePass:true}
}

func sellTrade(seq int64, token string, price,size float64) polymarket.MarketTrade {
    return polymarket.MarketTrade{Seq:seq,TokenID:token,Price:price,Size:size,Side:"SELL",Timestamp:time.Now().UTC()}
}

func TestSafeFirstOnlyAndPartialFill(t *testing.T) {
    now:=time.Date(2026,8,12,0,0,0,0,time.UTC)
    up:=paperBook("up",.40,.44,100); down:=paperBook("down",.53,.58,100)
    c:=NewPaperCycle(paperSnap(),up,down,now,10,0)
    if c.Status!=PaperStatusRestingFirst || c.FirstOrderSide!="UP" || c.SecondOrderSide!="DOWN" { t.Fatalf("bad start %+v",c) }
    if c.FirstQueueAhead!=0 { t.Fatalf("improved bid should have zero queue, got %.2f",c.FirstQueueAhead) }
    AdvancePaperCycle(c,up,down,[]polymarket.MarketTrade{sellTrade(11,"up",.41,2)},11,now.Add(time.Second),now.Add(time.Minute),DefaultPaperConfig())
    if c.Status!=PaperStatusFirstPartial || math.Abs(c.FirstFilledShares-2)>1e-9 || c.DownFilledShares!=0 { t.Fatalf("partial %+v",c) }
}

func TestLowerPrintCreditsOnlyPrintedSize(t *testing.T) {
    now:=time.Date(2026,8,12,0,0,0,0,time.UTC)
    up:=paperBook("up",.40,.44,100); down:=paperBook("down",.53,.58,100)
    c:=NewPaperCycle(paperSnap(),up,down,now,0,0)
    AdvancePaperCycle(c,up,down,[]polymarket.MarketTrade{sellTrade(1,"up",.40,1.25)},1,now.Add(time.Second),now.Add(time.Minute),DefaultPaperConfig())
    if math.Abs(c.FirstFilledShares-1.25)>1e-9 { t.Fatalf("must not fake full fill %+v",c) }
}

func TestQueueAheadRequiresSellVolume(t *testing.T) {
    now:=time.Date(2026,8,12,0,0,0,0,time.UTC)
    s:=paperSnap(); s.UpMakerPrice=.40
    up:=paperBook("up",.40,.44,7); down:=paperBook("down",.53,.58,100)
    c:=NewPaperCycle(s,up,down,now,0,0)
    if c.FirstQueueAhead!=7 { t.Fatalf("queue=%.2f",c.FirstQueueAhead) }
    AdvancePaperCycle(c,up,down,[]polymarket.MarketTrade{sellTrade(1,"up",.40,5)},1,now.Add(time.Second),now.Add(time.Minute),DefaultPaperConfig())
    if c.FirstFilledShares!=0 || math.Abs(c.FirstQueueAhead-2)>1e-9 { t.Fatalf("queue consumption %+v",c) }
    AdvancePaperCycle(c,up,down,[]polymarket.MarketTrade{sellTrade(2,"up",.40,4)},2,now.Add(2*time.Second),now.Add(time.Minute),DefaultPaperConfig())
    if math.Abs(c.FirstFilledShares-2)>1e-9 { t.Fatalf("expected 2 shares after queue %+v",c) }
}

func TestFirstFullStartsCompletionOnlyNextBatch(t *testing.T) {
    now:=time.Date(2026,8,12,0,0,0,0,time.UTC)
    up:=paperBook("up",.40,.44,100); down:=paperBook("down",.53,.58,100)
    c:=NewPaperCycle(paperSnap(),up,down,now,0,0)
    trades:=[]polymarket.MarketTrade{sellTrade(1,"up",.41,5), sellTrade(2,"down",.54,5)}
    AdvancePaperCycle(c,up,down,trades,2,now.Add(time.Second),now.Add(time.Minute),DefaultPaperConfig())
    if c.Status!=PaperStatusCompleting || c.SecondFilledShares!=0 { t.Fatalf("same-batch second fill forbidden %+v",c) }
    AdvancePaperCycle(c,up,down,[]polymarket.MarketTrade{sellTrade(3,"down",.54,5)},3,now.Add(2*time.Second),now.Add(time.Minute),DefaultPaperConfig())
    if c.Status!=PaperStatusCompleted { t.Fatalf("expected completion %+v",c) }
    want:=5*(1-.41-.54)
    if math.Abs(c.PaperPnL-want)>1e-9 { t.Fatalf("pnl %.4f want %.4f",c.PaperPnL,want) }
}

func TestPartialRiskStartsAtFirstPartialAndTimesOutVWAP(t *testing.T) {
    now:=time.Date(2026,8,12,0,0,0,0,time.UTC)
    up:=paperBook("up",.40,.44,100); down:=paperBook("down",.53,.58,100)
    c:=NewPaperCycle(paperSnap(),up,down,now,0,0)
    cfg:=DefaultPaperConfig(); cfg.MaxStranded=2*time.Second
    AdvancePaperCycle(c,up,down,[]polymarket.MarketTrade{sellTrade(1,"up",.41,2)},1,now.Add(time.Second),now.Add(time.Minute),cfg)
    upLater:=paperBook("up",.38,.42,100)
    AdvancePaperCycle(c,upLater,down,nil,1,now.Add(4*time.Second),now.Add(time.Minute),cfg)
    if c.Status!=PaperStatusStrandedTimeout { t.Fatalf("status %+v",c) }
    want:=2*(.38-.41)
    if math.Abs(c.PaperPnL-want)>1e-9 { t.Fatalf("pnl %.4f want %.4f",c.PaperPnL,want) }
}

func TestDataGapInvalidatesWithoutInventingPnL(t *testing.T) {
    now:=time.Date(2026,8,12,0,0,0,0,time.UTC)
    c:=NewPaperCycle(paperSnap(),paperBook("up",.40,.44,100),paperBook("down",.53,.58,100),now,0,2)
    if !InvalidatePaperCycleDataGap(c,now.Add(time.Second)) || c.Status!=PaperStatusDataGapInvalid || c.PaperPnL!=0 { t.Fatalf("gap %+v",c) }
}

func TestCompletionRepriceNeverBreaksCeilingOrPostOnly(t *testing.T) {
    book:=paperBook("d",.55,.58,100)
    got,ok:=completionReprice(.54,.56,book)
    if !ok || got!=.56 { t.Fatalf("got %.4f ok=%v",got,ok) }
    book=paperBook("d",.56,.57,100)
    got,ok=completionReprice(.56,.56,book)
    if ok || got!=.56 { t.Fatalf("ceiling %.4f %v",got,ok) }
}
''')

# Engine tests cover sequential pricing and separate paper/live edge gates.
Path('internal/arb/engine_test.go').write_text(r'''package arb

import (
    "math"
    "testing"

    "pm-edge/internal/engine"
    "pm-edge/internal/polymarket"
)

func book(token string,bid,ask float64) polymarket.BookSnapshot {
    return polymarket.BookSnapshot{TokenID:token,BestBid:bid,BestAsk:ask,TickSize:.01,MinOrderSize:5,
        Bids:[]polymarket.CLOBLevel{{Price:bid,Size:10}},Asks:[]polymarket.CLOBLevel{{Price:ask,Size:10}}}
}
func baseResult()*engine.EvaluationResult { return &engine.EvaluationResult{Timestamp:"2026-08-12T00:00:00Z",PUp:.7,PDown:.3,PTBTerminal:engine.PTBTerminalEstimate{Ready:true,Decision:"UP",PAbove:.80,PBelow:.20,Confidence:60}} }

func TestMakerBuyPriceNeverCrossesAsk(t *testing.T){
    p,ok:=MakerBuyPrice(book("u",.42,.44),true); if !ok||math.Abs(p-.43)>1e-9{t.Fatalf("%.4f %v",p,ok)}
    p,ok=MakerBuyPrice(book("u",.42,.43),true); if !ok||math.Abs(p-.42)>1e-9{t.Fatalf("one tick %.4f",p)}
}

func TestSafeFirstSequentialAndDynamicMinSize(t *testing.T){
    e:=NewEngine(Config{Enabled:true,Timeframe:"5m",TargetEdge:.02,PaperMinEdge:.002,OperationalBuffer:.002,UncertaintyPenalty:.02,MaxStrandedUnits:1})
    up:=book("up",.40,.44); down:=book("down",.53,.58); down.MinOrderSize=7
    s:=e.Evaluate(baseResult(),&polymarket.Market{Slug:"m"},up,down)
    if s.OrderSize!=7||s.FirstLeg!="UP"{t.Fatalf("%+v",s)}
    if s.StrategyMode!="SAFE_FIRST_SEQUENTIAL_MAKER" || s.UpMakerPrice!=.41 || s.DownMakerPrice!=.54 {t.Fatalf("sequential %+v",s)}
    if !s.PaperEdgePass || !s.LiveEdgePass || s.Status!=StatusCandidate {t.Fatalf("candidate %+v",s)}
}

func TestPaperCandidateBelowLiveTargetStillCollects(t *testing.T){
    e:=NewEngine(Config{Enabled:true,TargetEdge:.02,PaperMinEdge:.002,OperationalBuffer:.002})
    // 0.99 planned pair -> 0.8% net, below 2% live but above 0.2% paper.
    s:=e.Evaluate(baseResult(),&polymarket.Market{Slug:"m"},book("up",.12,.13),book("down",.87,.88))
    if !s.PaperEdgePass || s.LiveEdgePass || s.Status!=StatusPaperCandidate || s.Reason!="PAPER_READY_LIVE_EDGE_BELOW_TARGET" {t.Fatalf("%+v",s)}
}

func TestBelowPaperMinBlocked(t *testing.T){
    e:=NewEngine(Config{Enabled:true,TargetEdge:.02,PaperMinEdge:.01,OperationalBuffer:.002})
    s:=e.Evaluate(baseResult(),&polymarket.Market{Slug:"m"},book("up",.49,.50),book("down",.50,.51))
    if s.PaperEdgePass || s.Status!=StatusBlocked || s.Reason!="PAIR_EDGE_BELOW_PAPER_MIN" {t.Fatalf("%+v",s)}
}

func TestPTBNotReadyFailsClosed(t *testing.T){
    r:=baseResult(); r.PTBTerminal.Ready=false
    e:=NewEngine(Config{Enabled:true,TargetEdge:.02,PaperMinEdge:.002,OperationalBuffer:.002})
    s:=e.Evaluate(r,&polymarket.Market{Slug:"m"},book("up",.40,.44),book("down",.53,.58))
    if s.Status!=StatusBlocked||s.Reason!="PTB_TERMINAL_NOT_READY"{t.Fatalf("%+v",s)}
}

func TestQueueAheadCountsDisplayedPriority(t *testing.T){
    b:=book("u",.40,.44); b.Bids=[]polymarket.CLOBLevel{{Price:.41,Size:3},{Price:.40,Size:7}}
    if q:=buyQueueAhead(b,.40); q!=10 {t.Fatalf("q %.2f",q)}
    if q:=buyQueueAhead(b,.42); q!=0 {t.Fatalf("improved q %.2f",q)}
}
''')

# Environment/documentation.
p=Path('.env.example'); s=p.read_text()
s=s.replace('ARB_TARGET_EDGE=0.02\n', 'ARB_TARGET_EDGE=0.02\n# Paper research samples smaller positive maker edges; this is NOT a live threshold.\nARB_PAPER_MIN_EDGE=0.002\n')
s=s.replace('ARB_MAX_BOOK_FETCH_MS=1000\n', 'ARB_MAX_BOOK_FETCH_MS=1000\nARB_TRADE_STREAM_MAX_AGE_SEC=20\n')
p.write_text(s)

p=Path('docs/maker-arb-shadow.md'); s=p.read_text()
s += r'''\n\n## Safe-first queue-aware paper model\nThe paper executor no longer pretends that a resting BUY fills when a REST ask snapshot crosses its limit. It posts only the PTB/risk-selected first leg, observes public Polymarket `last_trade_price` SELL executions from the market WebSocket, debits displayed price-time queue ahead, supports partial fills, and only activates the opposite completion order after the first leg is fully filled. Trades from the batch that completed the first leg cannot retroactively fill the second leg. A WebSocket data gap invalidates the cycle instead of inventing PnL. `ARB_PAPER_MIN_EDGE` controls research sampling separately from the future-live `ARB_TARGET_EDGE`.\n'''
p.write_text(s)

# Dashboard copy/statuses.
p=Path('web/static/index.html'); s=p.read_text()
s=s.replace('Maker Arbitraj — Ters Bacak Risk Motoru (Gölge)', 'Maker Arbitraj — SAFE-FIRST Ters Bacak Motoru (Gölge)')
s=s.replace('Gerçek Polymarket UP/DOWN emir defteriyle GTC/GTD + post-only maker arbitraj matematiği izlenir. Minimum pay adedi marketin min_order_size değerinden okunur.', 'Aynı anda iki bacak açılmaz. PTB/risk motorunun seçtiği güvenli ilk bacak post-only maker olarak simüle edilir; tam dolunca karşı bacak devreye girer. Fill kanıtı Polymarket public trade WebSocket + fiyat-zaman kuyruğu + partial fill modelidir.')
s=s.replace("'PAIR_EDGE_BELOW_TARGET':'Net maker arbitraj avantajı hedefin altında'", "'PAIR_EDGE_BELOW_TARGET':'Net maker arbitraj avantajı hedefin altında','PAIR_EDGE_BELOW_PAPER_MIN':'Net maker avantajı paper araştırma eşiğinin altında','PAPER_READY_LIVE_EDGE_BELOW_TARGET':'Paper adayı; canlı hedefin altında','MARKET_TRADE_STREAM_GAP':'Trade WebSocket veri boşluğu; cycle geçersiz','SAFE_FIRST_ORDER_TTL_EXPIRED':'Güvenli ilk bacak süre içinde dolmadı','STRANDED_TIMEOUT_MARK_TO_BID_VWAP':'Ters bacak süresi doldu; bid VWAP ile kapatıldı'")
s=s.replace("a.status==='CANDIDATE'?chip('ADAY · '+reasonTr(a.reason),'fresh'):chip('BEKLE · '+reasonTr(a.reason),'warn')", "a.status==='CANDIDATE'?chip('CANLI EŞİK ADAYI · '+reasonTr(a.reason),'fresh'):a.status==='PAPER_CANDIDATE'?chip('PAPER ADAYI · '+reasonTr(a.reason),'open'):chip('BEKLE · '+reasonTr(a.reason),'warn')")
s=s.replace("a.status==='CANDIDATE'?chip('ADAY','fresh'):chip('BEKLE','neutral')", "a.status==='CANDIDATE'?chip('CANLI ADAY','fresh'):a.status==='PAPER_CANDIDATE'?chip('PAPER ADAY','open'):chip('BEKLE','neutral')")
s=s.replace("const st=x=>x==='COMPLETED'?chip('TAMAMLANDI','fresh'):x==='ONE_LEG_FILLED'?chip('TEK BACAK','warn'):x==='RESTING_PAIR'?chip('EMİRLER BEKLİYOR','open'):x==='STRANDED_TIMEOUT'?chip('TERS BACAK ZARARI','down'):chip('DOLMADI','neutral');", "const st=x=>x==='COMPLETED'?chip('TAMAMLANDI','fresh'):x==='FIRST_PARTIAL'?chip('İLK BACAK PARTIAL','warn'):x==='COMPLETING'?chip('KARŞI BACAK BEKLİYOR','open'):x==='COMPLETION_PARTIAL'?chip('KARŞI BACAK PARTIAL','warn'):x==='RESTING_FIRST'?chip('GÜVENLİ İLK EMİR','open'):x==='STRANDED_TIMEOUT'?chip('TERS BACAK ZARARI','down'):x==='DATA_GAP_INVALID'?chip('VERİ GAP · GEÇERSİZ','neutral'):chip('DOLMADI','neutral');")
s=s.replace("${Number(c.upOrderPrice||0).toFixed(3)} / ${c.upFillPrice?Number(c.upFillPrice).toFixed(3):'—'}</td><td>${Number(c.downOrderPrice||0).toFixed(3)} / ${c.downFillPrice?Number(c.downFillPrice).toFixed(3):'—'}</td>", "${Number(c.upOrderPrice||0).toFixed(3)} / ${c.upFillPrice?Number(c.upFillPrice).toFixed(3):'—'} [${Number(c.upFilledShares||0).toFixed(2)}]</td><td>${Number(c.downOrderPrice||0).toFixed(3)} / ${c.downFillPrice?Number(c.downFillPrice).toFixed(3):'—'} [${Number(c.downFilledShares||0).toFixed(2)}]</td>")
p.write_text(s)
