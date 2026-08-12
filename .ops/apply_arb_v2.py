from pathlib import Path


def read(path):
    return Path(path).read_text()


def write(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def replace_once(path, old, new):
    s = read(path)
    if old not in s:
        raise SystemExit(f'marker missing in {path}: {old[:120]!r}')
    write(path, s.replace(old, new, 1))


completion_model = r'''package arb

import (
    "math"
    "sort"
    "strings"
)

type CompletionPolicy struct {
    Lookback                    int     `json:"lookback"`
    MinSamples                  int     `json:"minSamples"`
    MinStrandedSamples          int     `json:"minStrandedSamples"`
    MinPComplete5sLower95       float64 `json:"minPComplete5sLower95"`
    MinCycleEV                  float64 `json:"minCycleEv"`
    MaxStrandedLossMultiple     float64 `json:"maxStrandedLossMultiple"`
}

type CompletionEstimate struct {
    Ready                       bool    `json:"ready"`
    Scope                       string  `json:"scope"`
    FirstLeg                    string  `json:"firstLeg"`
    FirstFillSamples            int     `json:"firstFillSamples"`
    FirstFillCount              int     `json:"firstFillCount"`
    FirstFullCount              int     `json:"firstFullCount"`
    PFirstFill                  float64 `json:"pFirstFill"`
    PFirstFullGivenFill         float64 `json:"pFirstFullGivenFill"`
    CompletionSamples           int     `json:"completionSamples"`
    Completed250ms              int     `json:"completed250ms"`
    Completed1s                 int     `json:"completed1s"`
    Completed2s                 int     `json:"completed2s"`
    Completed5s                 int     `json:"completed5s"`
    PComplete250ms              float64 `json:"pComplete250ms"`
    PComplete1s                 float64 `json:"pComplete1s"`
    PComplete2s                 float64 `json:"pComplete2s"`
    PComplete5s                 float64 `json:"pComplete5s"`
    PComplete5sLower95          float64 `json:"pComplete5sLower95"`
    AverageCompletionMs         float64 `json:"averageCompletionMs"`
    FullStrandedSamples         int     `json:"fullStrandedSamples"`
    PartialStrandedSamples      int     `json:"partialStrandedSamples"`
    ExpectedPairProfit          float64 `json:"expectedPairProfit"`
    ExpectedFullStrandedPnL     float64 `json:"expectedFullStrandedPnl"`
    ExpectedPartialStrandedPnL  float64 `json:"expectedPartialStrandedPnl"`
    FullCycleEV                 float64 `json:"fullCycleEv"`
    ConservativeFullCycleEV     float64 `json:"conservativeFullCycleEv"`
    CycleEV                     float64 `json:"cycleEv"`
    ConservativeCycleEV         float64 `json:"conservativeCycleEv"`
    OpportunityEV               float64 `json:"opportunityEv"`
    StrandedLossMultiple        float64 `json:"strandedLossMultiple"`
    ProbabilityPass             bool    `json:"probabilityPass"`
    CycleEVPass                 bool    `json:"cycleEvPass"`
    StrandedLossPass            bool    `json:"strandedLossPass"`
}

func DefaultCompletionPolicy() CompletionPolicy {
    return CompletionPolicy{
        Lookback: 500, MinSamples: 30, MinStrandedSamples: 3,
        MinPComplete5sLower95: 0.70, MinCycleEV: 0.01, MaxStrandedLossMultiple: 4,
    }
}

func ApplyCompletionModel(s *Snapshot, cycles []PaperCycle, policy CompletionPolicy) {
    if s == nil {
        return
    }
    policy = normalizeCompletionPolicy(policy)
    s.CompletionMinSamples = policy.MinSamples
    s.CompletionMinStrandedSamples = policy.MinStrandedSamples
    s.MinPComplete5sLower95 = policy.MinPComplete5sLower95
    s.MinCycleEV = policy.MinCycleEV
    s.MaxStrandedLossMultiple = policy.MaxStrandedLossMultiple

    upEst, upOK := estimatePath(cycles, s, "UP", policy)
    downEst, downOK := estimatePath(cycles, s, "DOWN", policy)
    if upOK {
        s.UpPathModelReady = upEst.Ready
        s.UpPathOpportunityEV = upEst.OpportunityEV
    }
    if downOK {
        s.DownPathModelReady = downEst.Ready
        s.DownPathOpportunityEV = downEst.OpportunityEV
    }

    // Fundamental paper gates remain authoritative. Completion-model warmup must
    // never turn an invalid book/PTB/path into a candidate.
    if !s.PTBReady || (!s.UpPathEligible && !s.DownPathEligible) {
        s.PairEdgePass = false
        return
    }

    type pathChoice struct {
        leg string
        est CompletionEstimate
    }
    live := make([]pathChoice, 0, 2)
    if upOK && pathLivePass(s, "UP", upEst, policy) {
        live = append(live, pathChoice{"UP", upEst})
    }
    if downOK && pathLivePass(s, "DOWN", downEst, policy) {
        live = append(live, pathChoice{"DOWN", downEst})
    }
    if len(live) > 0 {
        best := live[0]
        for _, c := range live[1:] {
            if c.est.OpportunityEV > best.est.OpportunityEV {
                best = c
            }
        }
        setSnapshotPath(s, best.leg)
        applyEstimate(s, best.est)
        s.SelectedBy = "COMPLETION_OPPORTUNITY_EV"
        s.Status = StatusCandidate
        s.Reason = "READY_COMPLETION_EV"
        s.PairEdgePass = true
        return
    }

    // Paper research remains on so the empirical model can warm up. If one or
    // both paths have enough data, use the higher conservative opportunity EV;
    // otherwise retain the PTB/stranded-risk safe-first selection from Engine.
    var selected CompletionEstimate
    selectedOK := false
    if upOK && upEst.Ready && s.UpPathEligible {
        selected = upEst
        selectedOK = true
        setSnapshotPath(s, "UP")
    }
    if downOK && downEst.Ready && s.DownPathEligible && (!selectedOK || downEst.OpportunityEV > selected.OpportunityEV) {
        selected = downEst
        selectedOK = true
        setSnapshotPath(s, "DOWN")
    }
    if selectedOK {
        applyEstimate(s, selected)
        s.SelectedBy = "COMPLETION_OPPORTUNITY_EV_PAPER"
    } else {
        if strings.EqualFold(s.FirstLeg, "DOWN") && downOK {
            selected, selectedOK = downEst, true
        } else if upOK {
            selected, selectedOK = upEst, true
        }
        if selectedOK {
            applyEstimate(s, selected)
        }
        s.SelectedBy = "STRANDED_RISK_WARMUP"
    }

    if !s.PaperEdgePass {
        s.Status = StatusBlocked
        s.Reason = "PAIR_EDGE_BELOW_PAPER_MIN"
        s.PairEdgePass = false
        return
    }
    s.Status = StatusPaperCandidate
    s.PairEdgePass = false
    if !selectedOK || !selected.Ready {
        s.Reason = "COMPLETION_MODEL_WARMUP"
        return
    }
    if !s.LiveEdgePass {
        s.Reason = "PAPER_READY_LIVE_EDGE_BELOW_TARGET"
        return
    }
    if !selected.ProbabilityPass {
        s.Reason = "PCOMPLETE_5S_BELOW_THRESHOLD"
        return
    }
    if !selected.CycleEVPass {
        s.Reason = "CYCLE_EV_BELOW_THRESHOLD"
        return
    }
    if !selected.StrandedLossPass {
        s.Reason = "STRANDED_LOSS_MULTIPLE_TOO_HIGH"
        return
    }
    s.Reason = "PAPER_READY_MODEL_PATH_NOT_LIVE_SELECTED"
}

func estimatePath(cycles []PaperCycle, s *Snapshot, leg string, policy CompletionPolicy) (CompletionEstimate, bool) {
    clone := *s
    if !setSnapshotPath(&clone, leg) {
        return CompletionEstimate{}, false
    }
    return EstimateCompletionModel(cycles, &clone, policy), true
}

func EstimateCompletionModel(cycles []PaperCycle, s *Snapshot, policy CompletionPolicy) CompletionEstimate {
    policy = normalizeCompletionPolicy(policy)
    out := CompletionEstimate{FirstLeg: s.FirstLeg, Scope: "NO_DATA"}
    if s == nil || s.FirstLeg == "" {
        return out
    }

    scopes := []struct {
        name string
        band float64
    }{{"LEG_EDGE_0_5PP", .005}, {"LEG_EDGE_1_5PP", .015}, {"LEG_ALL", -1}}
    var rows []PaperCycle
    for _, scope := range scopes {
        candidate := filterTrainingCycles(cycles, s.FirstLeg, s.NetEdge, scope.band)
        rows = candidate
        out.Scope = scope.name
        if countCompletionSamples(candidate) >= policy.MinSamples && len(candidate) >= policy.MinSamples {
            break
        }
    }

    out.FirstFillSamples = len(rows)
    completionMs := make([]float64, 0)
    fullStranded := make([]float64, 0)
    partialStranded := make([]float64, 0)
    for _, c := range rows {
        firstFilled := c.FirstPartialAt != "" || c.ActualFirstLeg != "" || c.FirstFilledShares > 0
        firstFull := c.FirstFullAt != "" || c.FirstFilledShares+1e-9 >= c.OrderSize && c.OrderSize > 0
        if firstFilled {
            out.FirstFillCount++
        }
        if firstFull {
            out.FirstFullCount++
        }
        if firstFull && (c.Status == PaperStatusCompleted || c.Status == PaperStatusStrandedTimeout) {
            out.CompletionSamples++
            if c.Status == PaperStatusCompleted {
                if c.CompletionMs > 0 {
                    completionMs = append(completionMs, float64(c.CompletionMs))
                }
                if c.CompletionMs > 0 && c.CompletionMs <= 250 {
                    out.Completed250ms++
                }
                if c.CompletionMs > 0 && c.CompletionMs <= 1000 {
                    out.Completed1s++
                }
                if c.CompletionMs > 0 && c.CompletionMs <= 2000 {
                    out.Completed2s++
                }
                if c.CompletionMs > 0 && c.CompletionMs <= 5000 {
                    out.Completed5s++
                }
            } else {
                fullStranded = append(fullStranded, c.PaperPnL)
            }
        } else if firstFilled && !firstFull && c.Status == PaperStatusStrandedTimeout {
            partialStranded = append(partialStranded, c.PaperPnL)
        }
    }

    out.PFirstFill = betaMean(out.FirstFillCount, out.FirstFillSamples)
    out.PFirstFullGivenFill = betaMean(out.FirstFullCount, out.FirstFillCount)
    out.PComplete250ms = betaMean(out.Completed250ms, out.CompletionSamples)
    out.PComplete1s = betaMean(out.Completed1s, out.CompletionSamples)
    out.PComplete2s = betaMean(out.Completed2s, out.CompletionSamples)
    out.PComplete5s = betaMean(out.Completed5s, out.CompletionSamples)
    out.PComplete5sLower95 = wilsonLower95(out.Completed5s, out.CompletionSamples)
    if len(completionMs) > 0 {
        sum := 0.0
        for _, v := range completionMs { sum += v }
        out.AverageCompletionMs = sum / float64(len(completionMs))
    }
    out.FullStrandedSamples = len(fullStranded)
    out.PartialStrandedSamples = len(partialStranded)

    currentExit := immediateExitPnL(s)
    out.ExpectedFullStrandedPnL = conservativePnL(fullStranded, currentExit)
    out.ExpectedPartialStrandedPnL = conservativePnL(partialStranded, currentExit)
    out.ExpectedPairProfit = math.Max(0, s.OrderSize*s.NetEdge)
    out.FullCycleEV = out.PComplete5s*out.ExpectedPairProfit + (1-out.PComplete5s)*out.ExpectedFullStrandedPnL
    out.ConservativeFullCycleEV = out.PComplete5sLower95*out.ExpectedPairProfit + (1-out.PComplete5sLower95)*out.ExpectedFullStrandedPnL
    out.CycleEV = out.PFirstFullGivenFill*out.FullCycleEV + (1-out.PFirstFullGivenFill)*out.ExpectedPartialStrandedPnL
    out.ConservativeCycleEV = out.PFirstFullGivenFill*out.ConservativeFullCycleEV + (1-out.PFirstFullGivenFill)*out.ExpectedPartialStrandedPnL
    out.OpportunityEV = out.PFirstFill * out.ConservativeCycleEV

    worstLoss := math.Min(out.ExpectedFullStrandedPnL, out.ExpectedPartialStrandedPnL)
    if out.ExpectedPairProfit > 1e-12 {
        out.StrandedLossMultiple = math.Abs(math.Min(0, worstLoss)) / out.ExpectedPairProfit
    } else {
        out.StrandedLossMultiple = math.Inf(1)
    }
    out.Ready = out.FirstFillSamples >= policy.MinSamples && out.CompletionSamples >= policy.MinSamples && out.FullStrandedSamples >= policy.MinStrandedSamples
    out.ProbabilityPass = out.Ready && out.PComplete5sLower95+1e-12 >= policy.MinPComplete5sLower95
    out.CycleEVPass = out.Ready && out.ConservativeCycleEV+1e-12 >= policy.MinCycleEV
    out.StrandedLossPass = out.Ready && out.StrandedLossMultiple <= policy.MaxStrandedLossMultiple+1e-12
    return out
}

func pathLivePass(s *Snapshot, leg string, est CompletionEstimate, policy CompletionPolicy) bool {
    net := s.UpPathNetEdge
    eligible := s.UpPathEligible
    if strings.EqualFold(leg, "DOWN") {
        net = s.DownPathNetEdge
        eligible = s.DownPathEligible
    }
    return eligible && net+1e-12 >= s.TargetEdge && est.Ready && est.ProbabilityPass && est.CycleEVPass && est.StrandedLossPass
}

func setSnapshotPath(s *Snapshot, leg string) bool {
    if s == nil { return false }
    if strings.EqualFold(leg, "DOWN") {
        if !s.DownPathEligible { return false }
        s.FirstLeg = "DOWN"
        s.DownMakerPrice = s.DownPathFirstPrice
        s.UpMakerPrice = s.DownPathCompletionPrice
        s.NetEdge = s.DownPathNetEdge
        s.FirstLegQueueAhead = s.DownPathQueueAhead
        s.QuoteSkew = "SAFE_FIRST_DOWN_THEN_UP"
    } else {
        if !s.UpPathEligible { return false }
        s.FirstLeg = "UP"
        s.UpMakerPrice = s.UpPathFirstPrice
        s.DownMakerPrice = s.UpPathCompletionPrice
        s.NetEdge = s.UpPathNetEdge
        s.FirstLegQueueAhead = s.UpPathQueueAhead
        s.QuoteSkew = "SAFE_FIRST_UP_THEN_DOWN"
    }
    s.PairCost = s.UpMakerPrice + s.DownMakerPrice
    s.GrossEdge = 1 - s.PairCost
    s.PaperEdgePass = s.NetEdge+1e-12 >= s.PaperMinEdge
    s.LiveEdgePass = s.NetEdge+1e-12 >= s.TargetEdge
    s.PairEdgePass = false
    if s.PaperEdgePass {
        s.ExpectedLockedProfit = s.OrderSize * s.NetEdge
    } else {
        s.ExpectedLockedProfit = 0
    }
    return true
}

func applyEstimate(s *Snapshot, e CompletionEstimate) {
    s.CompletionModelReady = e.Ready
    s.CompletionModelScope = e.Scope
    s.FirstFillSamples = e.FirstFillSamples
    s.FirstFillCount = e.FirstFillCount
    s.PFirstFill = e.PFirstFill
    s.PFirstFullGivenFill = e.PFirstFullGivenFill
    s.CompletionSamples = e.CompletionSamples
    s.FullStrandedSamples = e.FullStrandedSamples
    s.PComplete250ms = e.PComplete250ms
    s.PComplete1s = e.PComplete1s
    s.PComplete2s = e.PComplete2s
    s.PComplete5s = e.PComplete5s
    s.PComplete5sLower95 = e.PComplete5sLower95
    s.AverageCompletionMs = e.AverageCompletionMs
    s.ExpectedPairProfitEV = e.ExpectedPairProfit
    s.ExpectedFullStrandedPnL = e.ExpectedFullStrandedPnL
    s.ExpectedPartialStrandedPnL = e.ExpectedPartialStrandedPnL
    s.FullCycleEV = e.FullCycleEV
    s.ConservativeCycleEV = e.ConservativeCycleEV
    s.OpportunityEV = e.OpportunityEV
    s.StrandedLossMultiple = e.StrandedLossMultiple
    s.CompletionProbabilityPass = e.ProbabilityPass
    s.CycleEVPass = e.CycleEVPass
    s.StrandedLossPass = e.StrandedLossPass
}

func filterTrainingCycles(cycles []PaperCycle, leg string, edge, band float64) []PaperCycle {
    out := make([]PaperCycle, 0, len(cycles))
    for _, c := range cycles {
        if c.FillModel != "WS_SELL_TRADES_PRICE_TIME_QUEUE_PARTIAL" || !strings.EqualFold(c.PreferredFirstLeg, leg) {
            continue
        }
        switch c.Status {
        case PaperStatusCompleted, PaperStatusStrandedTimeout, PaperStatusExpiredNoFill:
        default:
            continue
        }
        if band >= 0 && math.Abs(c.EntryNetEdge-edge) > band+1e-12 {
            continue
        }
        out = append(out, c)
    }
    return out
}

func countCompletionSamples(rows []PaperCycle) int {
    n := 0
    for _, c := range rows {
        firstFull := c.FirstFullAt != "" || c.FirstFilledShares+1e-9 >= c.OrderSize && c.OrderSize > 0
        if firstFull && (c.Status == PaperStatusCompleted || c.Status == PaperStatusStrandedTimeout) { n++ }
    }
    return n
}

func immediateExitPnL(s *Snapshot) float64 {
    if s == nil || s.OrderSize <= 0 { return 0 }
    if strings.EqualFold(s.FirstLeg, "DOWN") {
        return s.OrderSize * (s.DownBestBid - s.DownMakerPrice)
    }
    return s.OrderSize * (s.UpBestBid - s.UpMakerPrice)
}

func conservativePnL(values []float64, fallback float64) float64 {
    fallback = math.Min(0, fallback)
    if len(values) == 0 { return fallback }
    cp := append([]float64(nil), values...)
    sort.Float64s(cp)
    sum := 0.0
    for _, v := range cp { sum += v }
    avg := sum / float64(len(cp))
    q25 := cp[int(math.Floor(.25*float64(len(cp)-1)))]
    return math.Min(0, math.Min(fallback, math.Min(avg, q25)))
}

func betaMean(success, n int) float64 {
    if n <= 0 { return 0 }
    return (float64(success)+.5)/(float64(n)+1)
}

func wilsonLower95(success, n int) float64 {
    if n <= 0 { return 0 }
    z := 1.959963984540054
    nn := float64(n)
    p := float64(success)/nn
    z2 := z*z
    den := 1+z2/nn
    center := (p+z2/(2*nn))/den
    margin := z*math.Sqrt((p*(1-p)+z2/(4*nn))/nn)/den
    return math.Max(0, center-margin)
}

func normalizeCompletionPolicy(p CompletionPolicy) CompletionPolicy {
    d := DefaultCompletionPolicy()
    if p.Lookback <= 0 { p.Lookback = d.Lookback }
    if p.MinSamples <= 0 { p.MinSamples = d.MinSamples }
    if p.MinStrandedSamples <= 0 { p.MinStrandedSamples = d.MinStrandedSamples }
    if p.MinPComplete5sLower95 <= 0 { p.MinPComplete5sLower95 = d.MinPComplete5sLower95 }
    if p.MinCycleEV <= 0 { p.MinCycleEV = d.MinCycleEV }
    if p.MaxStrandedLossMultiple <= 0 { p.MaxStrandedLossMultiple = d.MaxStrandedLossMultiple }
    return p
}
'''
write('internal/arb/completion_model.go', completion_model)

completion_test = r'''package arb

import (
    "fmt"
    "math"
    "testing"
)

func modelSnap() *Snapshot {
    return &Snapshot{
        Timeframe:"5m", FirstLeg:"UP", OrderSize:5, TargetEdge:.02, PaperMinEdge:.002, PTBReady:true,
        UpBestBid:.40, DownBestBid:.53, UpMakerPrice:.41, DownMakerPrice:.56, NetEdge:.028,
        UpPathEligible:true, UpPathFirstPrice:.41, UpPathCompletionPrice:.56, UpPathNetEdge:.028, UpPathQueueAhead:0,
        DownPathEligible:true, DownPathFirstPrice:.54, DownPathCompletionPrice:.43, DownPathNetEdge:.028, DownPathQueueAhead:0,
        UpCompletionMax:.56, DownCompletionMax:.56,
    }
}

func trainingCycle(i int, leg, status string, completionMs int64, pnl float64, full bool) PaperCycle {
    c := PaperCycle{ID:int64(i+1), PreferredFirstLeg:leg, ActualFirstLeg:leg, FillModel:"WS_SELL_TRADES_PRICE_TIME_QUEUE_PARTIAL", StrategyMode:"SAFE_FIRST_SEQUENTIAL_MAKER", Status:status, OrderSize:5, FirstFilledShares:5, EntryNetEdge:.028, PaperPnL:pnl, CompletionMs:completionMs, FirstPartialAt:"2026-08-12T00:00:00Z"}
    if full { c.FirstFullAt="2026-08-12T00:00:00.100Z" } else { c.FirstFilledShares=2 }
    return c
}

func TestCompletionModelWarmupFailClosedButPaperCandidate(t *testing.T) {
    s:=modelSnap(); s.Status=StatusPaperCandidate; s.PaperEdgePass=true; s.LiveEdgePass=true
    rows:=[]PaperCycle{trainingCycle(1,"UP",PaperStatusCompleted,800,.12,true)}
    p:=DefaultCompletionPolicy(); ApplyCompletionModel(s,rows,p)
    if s.Status!=StatusPaperCandidate || s.Reason!="COMPLETION_MODEL_WARMUP" || s.PairEdgePass { t.Fatalf("warmup %+v",s) }
}

func TestCompletionModelPromotesOnlyWithProbabilityAndPositiveCycleEV(t *testing.T) {
    s:=modelSnap(); s.Status=StatusPaperCandidate; s.PaperEdgePass=true; s.LiveEdgePass=true
    rows:=make([]PaperCycle,0,50)
    for i:=0;i<36;i++ { rows=append(rows,trainingCycle(i,"UP",PaperStatusCompleted,900,.14,true)) }
    for i:=36;i<40;i++ { rows=append(rows,trainingCycle(i,"UP",PaperStatusStrandedTimeout,0,-.12,true)) }
    for i:=40;i<45;i++ { c:=trainingCycle(i,"UP",PaperStatusExpiredNoFill,0,0,false); c.ActualFirstLeg=""; c.FirstPartialAt=""; c.FirstFilledShares=0; rows=append(rows,c) }
    // Also provide DOWN warmup data so path selection cannot accidentally use an empty alternative.
    for i:=45;i<85;i++ { rows=append(rows,trainingCycle(i,"DOWN",PaperStatusStrandedTimeout,0,-.40,true)) }
    p:=DefaultCompletionPolicy(); p.MinSamples=30; p.MinStrandedSamples=3; p.MinPComplete5sLower95=.70; p.MinCycleEV=.01
    ApplyCompletionModel(s,rows,p)
    if s.Status!=StatusCandidate || s.Reason!="READY_COMPLETION_EV" || !s.CompletionModelReady || !s.PairEdgePass { t.Fatalf("candidate %+v",s) }
    if s.PComplete5sLower95<.70 || s.ConservativeCycleEV<=0 || s.OpportunityEV<=0 { t.Fatalf("model metrics %+v",s) }
}

func TestNegativeCycleEVBlocksLiveEvenWhenRawPairEdgeLooksGood(t *testing.T) {
    s:=modelSnap(); s.Status=StatusPaperCandidate; s.PaperEdgePass=true; s.LiveEdgePass=true
    rows:=make([]PaperCycle,0,40)
    for i:=0;i<30;i++ { rows=append(rows,trainingCycle(i,"UP",PaperStatusCompleted,1000,.14,true)) }
    for i:=30;i<40;i++ { rows=append(rows,trainingCycle(i,"UP",PaperStatusStrandedTimeout,0,-1.0,true)) }
    p:=DefaultCompletionPolicy(); p.MinSamples=30; p.MinStrandedSamples=3; p.MinPComplete5sLower95=.40; p.MinCycleEV=.01; p.MaxStrandedLossMultiple=20
    ApplyCompletionModel(s,rows,p)
    if s.Status==StatusCandidate || s.ConservativeCycleEV>=p.MinCycleEV { t.Fatalf("must fail cycle EV %+v",s) }
}

func TestWilsonLowerIsConservative(t *testing.T) {
    got:=wilsonLower95(27,30)
    if !(got<.90 && got>.70) { t.Fatalf("wilson %.6f",got) }
}

func TestCompletionScopeFallsBackWhenNarrowBandSparse(t *testing.T) {
    s:=modelSnap(); rows:=make([]PaperCycle,0,30)
    for i:=0;i<30;i++ { c:=trainingCycle(i,"UP",PaperStatusCompleted,1000,.14,true); c.EntryNetEdge=.006; rows=append(rows,c) }
    p:=DefaultCompletionPolicy(); p.MinSamples=30; p.MinStrandedSamples=1
    e:=EstimateCompletionModel(rows,s,p)
    if e.Scope!="LEG_ALL" { t.Fatalf("scope=%s %s",e.Scope,fmt.Sprint(e)) }
    if math.IsNaN(e.PComplete5s) { t.Fatal("nan") }
}
'''
write('internal/arb/completion_model_test.go', completion_test)

# Engine: add path diagnostics + empirical model output fields.
replace_once('internal/arb/engine.go', '\tExpectedLockedProfit float64 `json:"expectedLockedProfit"`\n', '''\tExpectedLockedProfit float64 `json:"expectedLockedProfit"`\n\n\tUpPathEligible          bool    `json:"upPathEligible"`\n\tUpPathFirstPrice        float64 `json:"upPathFirstPrice"`\n\tUpPathCompletionPrice   float64 `json:"upPathCompletionPrice"`\n\tUpPathNetEdge           float64 `json:"upPathNetEdge"`\n\tUpPathQueueAhead        float64 `json:"upPathQueueAhead"`\n\tDownPathEligible        bool    `json:"downPathEligible"`\n\tDownPathFirstPrice      float64 `json:"downPathFirstPrice"`\n\tDownPathCompletionPrice float64 `json:"downPathCompletionPrice"`\n\tDownPathNetEdge         float64 `json:"downPathNetEdge"`\n\tDownPathQueueAhead      float64 `json:"downPathQueueAhead"`\n\tUpPathModelReady        bool    `json:"upPathModelReady"`\n\tDownPathModelReady      bool    `json:"downPathModelReady"`\n\tUpPathOpportunityEV     float64 `json:"upPathOpportunityEv"`\n\tDownPathOpportunityEV   float64 `json:"downPathOpportunityEv"`\n\tSelectedBy              string  `json:"selectedBy"`\n\n\tCompletionModelReady       bool    `json:"completionModelReady"`\n\tCompletionModelScope       string  `json:"completionModelScope"`\n\tCompletionMinSamples       int     `json:"completionMinSamples"`\n\tCompletionMinStrandedSamples int   `json:"completionMinStrandedSamples"`\n\tFirstFillSamples           int     `json:"firstFillSamples"`\n\tFirstFillCount             int     `json:"firstFillCount"`\n\tPFirstFill                 float64 `json:"pFirstFill"`\n\tPFirstFullGivenFill        float64 `json:"pFirstFullGivenFill"`\n\tCompletionSamples          int     `json:"completionSamples"`\n\tFullStrandedSamples        int     `json:"fullStrandedSamples"`\n\tPComplete250ms             float64 `json:"pComplete250ms"`\n\tPComplete1s                float64 `json:"pComplete1s"`\n\tPComplete2s                float64 `json:"pComplete2s"`\n\tPComplete5s                float64 `json:"pComplete5s"`\n\tPComplete5sLower95         float64 `json:"pComplete5sLower95"`\n\tAverageCompletionMs        float64 `json:"averageCompletionMs"`\n\tExpectedPairProfitEV       float64 `json:"expectedPairProfitEv"`\n\tExpectedFullStrandedPnL    float64 `json:"expectedFullStrandedPnl"`\n\tExpectedPartialStrandedPnL float64 `json:"expectedPartialStrandedPnl"`\n\tFullCycleEV                float64 `json:"fullCycleEv"`\n\tConservativeCycleEV        float64 `json:"conservativeCycleEv"`\n\tOpportunityEV              float64 `json:"opportunityEv"`\n\tStrandedLossMultiple       float64 `json:"strandedLossMultiple"`\n\tMinPComplete5sLower95      float64 `json:"minPComplete5sLower95"`\n\tMinCycleEV                 float64 `json:"minCycleEv"`\n\tMaxStrandedLossMultiple    float64 `json:"maxStrandedLossMultiple"`\n\tCompletionProbabilityPass  bool    `json:"completionProbabilityPass"`\n\tCycleEVPass                bool    `json:"cycleEvPass"`\n\tStrandedLossPass           bool    `json:"strandedLossPass"`\n''')
replace_once('internal/arb/engine.go', '\t\tStrategyMode:      "SAFE_FIRST_SEQUENTIAL_MAKER",\n', '\t\tStrategyMode:      "COMPLETION_PROBABILITY_SAFE_FIRST_V2",\n')
replace_once('internal/arb/engine.go', '''\tupEligible := upFirstNet+1e-12 >= e.cfg.PaperMinEdge\n\tdownEligible := downFirstNet+1e-12 >= e.cfg.PaperMinEdge\n\n\tif !upEligible && !downEligible {\n''', '''\tupEligible := upFirstNet+1e-12 >= e.cfg.PaperMinEdge\n\tdownEligible := downFirstNet+1e-12 >= e.cfg.PaperMinEdge\n\tsnap.UpPathEligible = upEligible\n\tsnap.UpPathFirstPrice = upFirst\n\tsnap.UpPathCompletionPrice = downAfterUp\n\tsnap.UpPathNetEdge = upFirstNet\n\tsnap.UpPathQueueAhead = buyQueueAhead(upBook, upFirst)\n\tsnap.DownPathEligible = downEligible\n\tsnap.DownPathFirstPrice = downFirst\n\tsnap.DownPathCompletionPrice = upAfterDown\n\tsnap.DownPathNetEdge = downFirstNet\n\tsnap.DownPathQueueAhead = buyQueueAhead(downBook, downFirst)\n\n\tif !upEligible && !downEligible {\n''')
replace_once('internal/arb/engine.go', '''\tif first == "UP" {\n\t\tsnap.UpMakerPrice = upFirst\n\t\tsnap.DownMakerPrice = downAfterUp\n\t\tsnap.NetEdge = upFirstNet\n\t\tsnap.QuoteSkew = "SAFE_FIRST_UP_THEN_DOWN"\n\t\tsnap.FirstLegQueueAhead = buyQueueAhead(upBook, upFirst)\n\t} else {\n\t\tsnap.DownMakerPrice = downFirst\n\t\tsnap.UpMakerPrice = upAfterDown\n\t\tsnap.NetEdge = downFirstNet\n\t\tsnap.QuoteSkew = "SAFE_FIRST_DOWN_THEN_UP"\n\t\tsnap.FirstLegQueueAhead = buyQueueAhead(downBook, downFirst)\n\t}\n\tsnap.FirstLeg = first\n\tsnap.PairCost = snap.UpMakerPrice + snap.DownMakerPrice\n\tsnap.GrossEdge = 1 - snap.PairCost\n\tsnap.PaperEdgePass = snap.NetEdge+1e-12 >= e.cfg.PaperMinEdge\n\tsnap.LiveEdgePass = snap.NetEdge+1e-12 >= e.cfg.TargetEdge\n\tsnap.PairEdgePass = snap.LiveEdgePass\n\tif snap.PaperEdgePass {\n\t\tsnap.ExpectedLockedProfit = snap.OrderSize * snap.NetEdge\n\t}\n''', '''\tsnap.FirstLeg = first\n\tif !setSnapshotPath(snap, first) {\n\t\tsnap.Reason = "NO_COMPETITIVE_COMPLETION_WITHIN_EDGE"\n\t\treturn snap\n\t}\n''')
replace_once('internal/arb/engine.go', '''\tif snap.LiveEdgePass {\n\t\tsnap.Status = StatusCandidate\n\t\tsnap.Reason = "READY"\n\t\treturn snap\n\t}\n\tsnap.Status = StatusPaperCandidate\n\tsnap.Reason = "PAPER_READY_LIVE_EDGE_BELOW_TARGET"\n''', '''\tsnap.Status = StatusPaperCandidate\n\tif snap.LiveEdgePass {\n\t\tsnap.Reason = "AWAITING_COMPLETION_MODEL"\n\t} else {\n\t\tsnap.Reason = "PAPER_READY_LIVE_EDGE_BELOW_TARGET"\n\t}\n''')

# Paper: 2s soft chase, 5s hard risk window, event-time completion latency.
replace_once('internal/arb/paper.go', '''type PaperConfig struct {\n\tEnabled       bool\n\tOrderTTL      time.Duration\n\tMaxStranded   time.Duration\n\tStopBeforeEnd time.Duration\n}\n''', '''type PaperConfig struct {\n\tEnabled        bool\n\tOrderTTL       time.Duration\n\tSoftCompletion time.Duration\n\tMaxStranded    time.Duration\n\tStopBeforeEnd  time.Duration\n}\n''')
replace_once('internal/arb/paper.go', '\tFirstFillAt      string  `json:"firstFillAt"`\n', '\tFirstFillAt      string  `json:"firstFillAt"`\n\tFirstFillMs      int64   `json:"firstFillMs"`\n')
replace_once('internal/arb/paper.go', '''func DefaultPaperConfig() PaperConfig {\n\treturn PaperConfig{Enabled: true, OrderTTL: 12 * time.Second, MaxStranded: 20 * time.Second, StopBeforeEnd: 12 * time.Second}\n}\n''', '''func DefaultPaperConfig() PaperConfig {\n\treturn PaperConfig{Enabled: true, OrderTTL: 12 * time.Second, SoftCompletion: 2 * time.Second, MaxStranded: 5 * time.Second, StopBeforeEnd: 12 * time.Second}\n}\n''')
replace_once('internal/arb/paper.go', 'FillModel: "WS_SELL_TRADES_PRICE_TIME_QUEUE_PARTIAL", OrderMode: "GTC_GTD_POST_ONLY", StrategyMode: "SAFE_FIRST_SEQUENTIAL_MAKER",', 'FillModel: "WS_SELL_TRADES_PRICE_TIME_QUEUE_PARTIAL", OrderMode: "GTC_GTD_POST_ONLY", StrategyMode: s.StrategyMode,')
replace_once('internal/arb/paper.go', '''\tif cfg.MaxStranded <= 0 {\n\t\tcfg.MaxStranded = 20 * time.Second\n\t}\n''', '''\tif cfg.SoftCompletion <= 0 {\n\t\tcfg.SoftCompletion = 2 * time.Second\n\t}\n\tif cfg.MaxStranded <= 0 {\n\t\tcfg.MaxStranded = 5 * time.Second\n\t}\n''')
replace_once('internal/arb/paper.go', '''\t\tdelta, q := makerBuyFillFromTrades(tokenForSide(c.FirstOrderSide, c), c.FirstOrderPrice, c.FirstFilledShares, c.OrderSize, c.FirstQueueAhead, trades)\n\t\tc.FirstQueueAhead = q\n\t\tif delta > 0 {\n\t\t\taddFill(c, c.FirstOrderSide, delta, c.FirstOrderPrice, now)\n\t\t\tc.FirstFilledShares += delta\n\t\t\tif c.FirstPartialAt == "" {\n\t\t\t\tc.FirstPartialAt = now.Format(time.RFC3339Nano)\n\t\t\t\tc.FirstFillAt = c.FirstPartialAt\n''', '''\t\tfill := makerBuyFillFromTradesDetailed(tokenForSide(c.FirstOrderSide, c), c.FirstOrderPrice, c.FirstFilledShares, c.OrderSize, c.FirstQueueAhead, trades)\n\t\tc.FirstQueueAhead = fill.QueueAhead\n\t\tif fill.Filled > 0 {\n\t\t\tfillAt := eventOrNow(fill.LastAt, now)\n\t\t\taddFill(c, c.FirstOrderSide, fill.Filled, c.FirstOrderPrice, fillAt)\n\t\t\tc.FirstFilledShares += fill.Filled\n\t\t\tif c.FirstPartialAt == "" {\n\t\t\t\tfirstAt := eventOrNow(fill.FirstAt, now)\n\t\t\t\tc.FirstPartialAt = firstAt.Format(time.RFC3339Nano)\n\t\t\t\tc.FirstFillAt = c.FirstPartialAt\n\t\t\t\tif created, ok := parseTime(c.CreatedAt); ok { c.FirstFillMs = maxInt64(0, firstAt.Sub(created).Milliseconds()) }\n''')
replace_once('internal/arb/paper.go', '''\t\t\tc.FirstFilledShares = c.OrderSize\n\t\t\tc.FirstFullAt = now.Format(time.RFC3339Nano)\n\t\t\tc.Status = PaperStatusCompleting\n\t\t\tc.CompletionPostedAt = c.FirstFullAt\n''', '''\t\t\tc.FirstFilledShares = c.OrderSize\n\t\t\tfullAt := eventOrNow(fill.LastAt, now)\n\t\t\tc.FirstFullAt = fullAt.Format(time.RFC3339Nano)\n\t\t\tc.Status = PaperStatusCompleting\n\t\t\t// A real completion order can only be posted after our process observes\n\t\t\t// the first-leg fill, so the completion clock starts at detection time.\n\t\t\tc.CompletionPostedAt = now.Format(time.RFC3339Nano)\n''')
replace_once('internal/arb/paper.go', '''\t\tdelta, q := makerBuyFillFromTrades(tokenForSide(c.SecondOrderSide, c), c.SecondOrderPrice, c.SecondFilledShares, c.OrderSize, c.SecondQueueAhead, trades)\n\t\tc.SecondQueueAhead = q\n\t\tif delta > 0 {\n\t\t\taddFill(c, c.SecondOrderSide, delta, c.SecondOrderPrice, now)\n\t\t\tc.SecondFilledShares += delta\n\t\t\tchanged = true\n\t\t}\n''', '''\t\tfill := makerBuyFillFromTradesDetailed(tokenForSide(c.SecondOrderSide, c), c.SecondOrderPrice, c.SecondFilledShares, c.OrderSize, c.SecondQueueAhead, trades)\n\t\tc.SecondQueueAhead = fill.QueueAhead\n\t\tif fill.Filled > 0 {\n\t\t\tfillAt := eventOrNow(fill.LastAt, now)\n\t\t\taddFill(c, c.SecondOrderSide, fill.Filled, c.SecondOrderPrice, fillAt)\n\t\t\tc.SecondFilledShares += fill.Filled\n\t\t\tchanged = true\n\t\t}\n''')
replace_once('internal/arb/paper.go', '''\t\tif c.SecondFilledShares+1e-9 >= c.OrderSize {\n\t\t\tc.SecondFilledShares = c.OrderSize\n\t\t\tfirstAt, _ := parseTime(c.FirstPartialAt)\n\t\t\tif !firstAt.IsZero() {\n\t\t\t\tc.CompletionMs = now.Sub(firstAt).Milliseconds()\n\t\t\t}\n\t\t\tcompleteCycle(c, now)\n''', '''\t\tif c.SecondFilledShares+1e-9 >= c.OrderSize {\n\t\t\tc.SecondFilledShares = c.OrderSize\n\t\t\tpostedAt, _ := parseTime(c.CompletionPostedAt)\n\t\t\tfillAt := eventOrNow(fill.LastAt, now)\n\t\t\tif !postedAt.IsZero() {\n\t\t\t\tc.CompletionMs = maxInt64(0, fillAt.Sub(postedAt).Milliseconds())\n\t\t\t}\n\t\t\tcompleteCycle(c, now)\n''')
replace_once('internal/arb/paper.go', '''\t\tif p, ok := completionReprice(c.SecondOrderPrice, ceiling, secondBook); ok && p > c.SecondOrderPrice+1e-12 {\n''', '''\t\turgent := false\n\t\tif postedAt, ok := parseTime(c.CompletionPostedAt); ok && now.Sub(postedAt) >= cfg.SoftCompletion { urgent = true }\n\t\tp, ok := completionRepriceWithUrgency(c.SecondOrderPrice, ceiling, secondBook, urgent)\n\t\tif ok && p > c.SecondOrderPrice+1e-12 {\n''')
replace_once('internal/arb/paper.go', '''func makerBuyFillFromTrades(tokenID string, orderPrice, alreadyFilled, orderSize, queueAhead float64, trades []polymarket.MarketTrade) (float64, float64) {\n\tremaining := math.Max(0, orderSize-alreadyFilled)\n\tfilled := 0.0\n\tq := math.Max(0, queueAhead)\n''', '''type makerFillResult struct {\n\tFilled     float64\n\tQueueAhead float64\n\tFirstAt    time.Time\n\tLastAt     time.Time\n}\n\nfunc makerBuyFillFromTrades(tokenID string, orderPrice, alreadyFilled, orderSize, queueAhead float64, trades []polymarket.MarketTrade) (float64, float64) {\n\tr := makerBuyFillFromTradesDetailed(tokenID, orderPrice, alreadyFilled, orderSize, queueAhead, trades)\n\treturn r.Filled, r.QueueAhead\n}\n\nfunc makerBuyFillFromTradesDetailed(tokenID string, orderPrice, alreadyFilled, orderSize, queueAhead float64, trades []polymarket.MarketTrade) makerFillResult {\n\tremaining := math.Max(0, orderSize-alreadyFilled)\n\tfilled := 0.0\n\tq := math.Max(0, queueAhead)\n\tvar firstAt, lastAt time.Time\n''')
replace_once('internal/arb/paper.go', '''\t\t} else if tr.Price < orderPrice-1e-9 {\n\t\t\t// A lower SELL print cannot occur while our higher resting BUY is\n\t\t\t// still unfilled. The sweep necessarily consumed our full remainder.\n\t\t\tq = 0\n\t\t\tfilled += remaining\n\t\t\tremaining = 0\n\t\t\tcontinue\n\t\t}\n\t\tif available > 0 && q <= 1e-9 {\n\t\t\tf := math.Min(remaining, available)\n\t\t\tfilled += f\n\t\t\tremaining -= f\n\t\t}\n\t}\n\treturn filled, q\n}\n''', '''\t\t} else if tr.Price < orderPrice-1e-9 {\n\t\t\t// A lower SELL print cannot occur while our higher resting BUY is\n\t\t\t// still unfilled. The sweep necessarily consumed our full remainder.\n\t\t\tq = 0\n\t\t\tif remaining > 0 {\n\t\t\t\tif firstAt.IsZero() { firstAt = tr.Timestamp }\n\t\t\t\tlastAt = tr.Timestamp\n\t\t\t}\n\t\t\tfilled += remaining\n\t\t\tremaining = 0\n\t\t\tcontinue\n\t\t}\n\t\tif available > 0 && q <= 1e-9 {\n\t\t\tf := math.Min(remaining, available)\n\t\t\tif f > 0 {\n\t\t\t\tif firstAt.IsZero() { firstAt = tr.Timestamp }\n\t\t\t\tlastAt = tr.Timestamp\n\t\t\t}\n\t\t\tfilled += f\n\t\t\tremaining -= f\n\t\t}\n\t}\n\treturn makerFillResult{Filled: filled, QueueAhead: q, FirstAt: firstAt, LastAt: lastAt}\n}\n''')
replace_once('internal/arb/paper.go', '''func completionReprice(current, economicCeiling float64, book polymarket.BookSnapshot) (float64, bool) {\n\tif current <= 0 || economicCeiling <= 0 || !validBook(book) {\n\t\treturn 0, false\n\t}\n\tcandidate := floorToTick(book.BestBid+book.TickSize, book.TickSize)\n''', '''func completionReprice(current, economicCeiling float64, book polymarket.BookSnapshot) (float64, bool) {\n\treturn completionRepriceWithUrgency(current, economicCeiling, book, false)\n}\n\nfunc completionRepriceWithUrgency(current, economicCeiling float64, book polymarket.BookSnapshot, urgent bool) (float64, bool) {\n\tif current <= 0 || economicCeiling <= 0 || !validBook(book) {\n\t\treturn 0, false\n\t}\n\tcandidate := floorToTick(book.BestBid+book.TickSize, book.TickSize)\n''')
replace_once('internal/arb/paper.go', '''\tpostOnlyCeiling := floorToTick(book.BestAsk-book.TickSize, book.TickSize)\n\tif candidate > postOnlyCeiling {\n''', '''\tpostOnlyCeiling := floorToTick(book.BestAsk-book.TickSize, book.TickSize)\n\tif urgent { candidate = floorToTick(math.Min(economicCeiling, postOnlyCeiling), book.TickSize) }\n\tif candidate > postOnlyCeiling {\n''')
replace_once('internal/arb/paper.go', '''func parseTime(v string) (time.Time, bool) {\n''', '''func eventOrNow(t, now time.Time) time.Time {\n\tif t.IsZero() { return now.UTC() }\n\treturn t.UTC()\n}\n\nfunc maxInt64(a, b int64) int64 { if a > b { return a }; return b }\n\nfunc parseTime(v string) (time.Time, bool) {\n''')

# Tests: event timestamp precision + soft/hard completion behavior.
replace_once('internal/arb/paper_test.go', '''func sellTrade(seq int64, token string, price, size float64) polymarket.MarketTrade {\n\treturn polymarket.MarketTrade{Seq: seq, TokenID: token, Price: price, Size: size, Side: "SELL", Timestamp: time.Now().UTC()}\n}\n''', '''func sellTrade(seq int64, token string, price, size float64) polymarket.MarketTrade {\n\treturn sellTradeAt(seq, token, price, size, time.Now().UTC())\n}\n\nfunc sellTradeAt(seq int64, token string, price, size float64, ts time.Time) polymarket.MarketTrade {\n\treturn polymarket.MarketTrade{Seq: seq, TokenID: token, Price: price, Size: size, Side: "SELL", Timestamp: ts.UTC()}\n}\n''')
write('internal/arb/paper_test.go', read('internal/arb/paper_test.go') + r'''

func TestCompletionMsUsesExecutionTimestampNotPollingInterval(t *testing.T) {
    now := time.Date(2026,8,12,0,0,0,0,time.UTC)
    up:=paperBook("up",.40,.44,100); down:=paperBook("down",.53,.58,100)
    c:=NewPaperCycle(paperSnap(),up,down,now,0,0)
    AdvancePaperCycle(c,up,down,[]polymarket.MarketTrade{sellTradeAt(1,"up",.41,5,now.Add(100*time.Millisecond))},1,now.Add(time.Second),now.Add(time.Minute),DefaultPaperConfig())
    if c.Status!=PaperStatusCompleting { t.Fatalf("first %+v",c) }
    AdvancePaperCycle(c,up,down,[]polymarket.MarketTrade{sellTradeAt(2,"down",.54,5,now.Add(1180*time.Millisecond))},2,now.Add(2*time.Second),now.Add(time.Minute),DefaultPaperConfig())
    if c.Status!=PaperStatusCompleted || c.CompletionMs!=180 { t.Fatalf("completionMs=%d %+v",c.CompletionMs,c) }
}

func TestSoftCompletionJumpsToEconomicCeiling(t *testing.T) {
    book:=paperBook("d",.53,.58,100)
    if got,ok:=completionRepriceWithUrgency(.54,.56,book,false); !ok || got!=.54 { // bestBid+tick equals current; no move
        if ok || got!=.54 { t.Fatalf("soft pre-window %.4f %v",got,ok) }
    }
    got,ok:=completionRepriceWithUrgency(.54,.56,book,true)
    if !ok || got!=.56 { t.Fatalf("urgent %.4f %v",got,ok) }
}
''')

# Config + runtime policy.
replace_once('internal/config/config.go', '\tArbPaperStopBeforeEndSec int\n', '''\tArbPaperStopBeforeEndSec int\n\tArbPaperSoftCompletionSec int\n\tArbCompletionLookback int\n\tArbCompletionMinSamples int\n\tArbCompletionMinStrandedSamples int\n\tArbCompletionMinP5Lower float64\n\tArbCompletionMinCycleEV float64\n\tArbCompletionMaxLossMultiple float64\n''')
replace_once('internal/config/config.go', '''\t\tArbPaperMaxStrandedSec:    envInt("ARB_PAPER_MAX_STRANDED_SEC", 20),\n\t\tArbPaperStopBeforeEndSec:  envInt("ARB_PAPER_STOP_BEFORE_END_SEC", 12),\n''', '''\t\tArbPaperMaxStrandedSec:    envInt("ARB_PAPER_MAX_STRANDED_SEC", 5),\n\t\tArbPaperStopBeforeEndSec:  envInt("ARB_PAPER_STOP_BEFORE_END_SEC", 12),\n\t\tArbPaperSoftCompletionSec: envInt("ARB_PAPER_SOFT_COMPLETION_SEC", 2),\n\t\tArbCompletionLookback: envInt("ARB_COMPLETION_LOOKBACK", 500),\n\t\tArbCompletionMinSamples: envInt("ARB_COMPLETION_MIN_SAMPLES", 30),\n\t\tArbCompletionMinStrandedSamples: envInt("ARB_COMPLETION_MIN_STRANDED_SAMPLES", 3),\n\t\tArbCompletionMinP5Lower: envFloat("ARB_COMPLETION_MIN_P5_LOWER", 0.70),\n\t\tArbCompletionMinCycleEV: envFloat("ARB_COMPLETION_MIN_CYCLE_EV", 0.01),\n\t\tArbCompletionMaxLossMultiple: envFloat("ARB_COMPLETION_MAX_LOSS_MULTIPLE", 4.0),\n''')
replace_once('cmd/pm-edge/arb_shadow.go', '\tactive              *arb.PaperCycle\n', '\tactive              *arb.PaperCycle\n\tcompletionPolicy    arb.CompletionPolicy\n')
replace_once('cmd/pm-edge/arb_shadow.go', '''\t\tpaperCfg:            arb.PaperConfig{Enabled: enabled && cfg.ArbPaperEnabled, OrderTTL: time.Duration(cfg.ArbPaperOrderTTLSec) * time.Second, MaxStranded: time.Duration(cfg.ArbPaperMaxStrandedSec) * time.Second, StopBeforeEnd: time.Duration(cfg.ArbPaperStopBeforeEndSec) * time.Second},\n\t\tpaperInitialBalance: cfg.PaperInitialBalance, maxBookFetchMs: int64(cfg.ArbMaxBookFetchMs), tradeStreamMaxAge: time.Duration(cfg.ArbTradeStreamMaxAgeSec) * time.Second}\n''', '''\t\tpaperCfg:            arb.PaperConfig{Enabled: enabled && cfg.ArbPaperEnabled, OrderTTL: time.Duration(cfg.ArbPaperOrderTTLSec) * time.Second, SoftCompletion: time.Duration(cfg.ArbPaperSoftCompletionSec) * time.Second, MaxStranded: time.Duration(cfg.ArbPaperMaxStrandedSec) * time.Second, StopBeforeEnd: time.Duration(cfg.ArbPaperStopBeforeEndSec) * time.Second},\n\t\tpaperInitialBalance: cfg.PaperInitialBalance, maxBookFetchMs: int64(cfg.ArbMaxBookFetchMs), tradeStreamMaxAge: time.Duration(cfg.ArbTradeStreamMaxAgeSec) * time.Second,\n\t\tcompletionPolicy: arb.CompletionPolicy{Lookback: cfg.ArbCompletionLookback, MinSamples: cfg.ArbCompletionMinSamples, MinStrandedSamples: cfg.ArbCompletionMinStrandedSamples, MinPComplete5sLower95: cfg.ArbCompletionMinP5Lower, MinCycleEV: cfg.ArbCompletionMinCycleEV, MaxStrandedLossMultiple: cfg.ArbCompletionMaxLossMultiple}}\n''')
replace_once('cmd/pm-edge/arb_shadow.go', '''\t\tsnap.BookFetchMs = fetchMs\n\t\tif snap.Status != arb.StatusBlocked && fetchMs > r.maxBookFetchMs {\n''', '''\t\tsnap.BookFetchMs = fetchMs\n\t\ttraining, trainErr := r.db.GetArbPaperCyclesByTimeframe(r.completionPolicy.Lookback, snap.Timeframe)\n\t\tif trainErr != nil {\n\t\t\tutil.Logger.Warn("Maker arb completion training read failed", zap.String("tf", snap.Timeframe), zap.Error(trainErr))\n\t\t}\n\t\tarb.ApplyCompletionModel(snap, training, r.completionPolicy)\n\t\tif snap.Status != arb.StatusBlocked && fetchMs > r.maxBookFetchMs {\n''')
replace_once('cmd/pm-edge/arb_shadow.go', '''zap.Float64("liveTargetEdge", snap.TargetEdge), zap.Int64("bookFetchMs", snap.BookFetchMs))''', '''zap.Float64("liveTargetEdge", snap.TargetEdge), zap.Bool("completionReady", snap.CompletionModelReady), zap.Float64("pComplete5sLower95", snap.PComplete5sLower95), zap.Float64("cycleEV", snap.ConservativeCycleEV), zap.Float64("opportunityEV", snap.OpportunityEV), zap.Int64("bookFetchMs", snap.BookFetchMs))''')

# Environment defaults and docs.
env = read('.env.example')
env = env.replace('ARB_PAPER_MAX_STRANDED_SEC=20', 'ARB_PAPER_MAX_STRANDED_SEC=5')
if 'ARB_PAPER_SOFT_COMPLETION_SEC=' not in env:
    env += '''\n# Arb v2 completion-probability / CycleEV policy\nARB_PAPER_SOFT_COMPLETION_SEC=2\nARB_COMPLETION_LOOKBACK=500\nARB_COMPLETION_MIN_SAMPLES=30\nARB_COMPLETION_MIN_STRANDED_SAMPLES=3\nARB_COMPLETION_MIN_P5_LOWER=0.70\nARB_COMPLETION_MIN_CYCLE_EV=0.01\nARB_COMPLETION_MAX_LOSS_MULTIPLE=4.0\n'''
write('.env.example', env)
write('docs/maker-arb-shadow.md', read('docs/maker-arb-shadow.md') + r'''

## Arb v2: completion probability + CycleEV
A displayed maker pair edge is not treated as locked arbitrage. It is only a planned completion path. The research/live-eligibility layer is empirical and uses only queue-aware WebSocket paper cycles. For each safe-first leg it estimates P(first fill), P(first full | fill), P(second-leg completion within 250ms/1s/2s/5s), and the Wilson 95% lower bound for 5-second completion. The model also measures conservative stranded PnL, conditional CycleEV and opportunity EV.

Paper sampling remains enabled during warmup, but live-eligible status fails closed until the selected path has enough resolved first-full samples and enough stranded observations. Default live research gates are: >=30 samples, >=3 full-stranded samples, Pcomplete(<=5s) Wilson lower bound >=70%, conservative CycleEV >= $0.01 per cycle, stranded-loss multiple <=4x expected pair profit, plus the existing 2% planned live edge. No live signing/submission is added.

Completion execution uses a 2-second soft maker window and a 5-second hard stranded-risk window. After the soft window, completion may move directly to the post-only economic ceiling. Completion latency is measured from order activation to actual Polymarket WebSocket execution timestamps rather than the polling interval.
''')

# Dashboard: explicitly separate planned pair edge from empirical completion EV.
html = read('web/static/index.html')
html = html.replace('Maker Arbitraj — Ters Bacak Risk Motoru (Gölge)', 'Maker Completion Arb v2 — Queue + P(Tamamlama) + CycleEV')
html = html.replace('<span>Net Arbitraj Avantajı / Hedef</span><strong id="arbEdge">—</strong>', '<span>Planlanan Pair Edge / Live Hedef</span><strong id="arbEdge">—</strong>')
html = html.replace('Konservatif: fiyat limitin altından geçmeli + tam pay kadar çapraz likidite', 'WS execution + exact-price FIFO + partial fill; ilk bacak dolmadan karşı bacak yok')
marker = '''    <div class="grid4" style="margin-top:14px">\n      <div class="mini"><span>Arbitraj Paper Bakiyesi</span>'''
model_grid = '''    <div class="grid4" style="margin-top:14px">\n      <div class="mini"><span>P(İlk Fill) / P(Full | Fill)</span><strong id="arbFirstFillProb">—</strong></div>\n      <div class="mini"><span>P(Tamamlama) 250ms / 1s</span><strong id="arbPCompleteFast">—</strong></div>\n      <div class="mini"><span>P(Tamamlama) 2s / 5s</span><strong id="arbPCompleteSlow">—</strong></div>\n      <div class="mini"><span>5s %95 Alt Sınır / Örnek</span><strong id="arbPCompleteLower">—</strong></div>\n      <div class="mini"><span>Pair Kârı / Stranded Beklenti</span><strong id="arbEVInputs">—</strong></div>\n      <div class="mini"><span>Konservatif CycleEV</span><strong id="arbCycleEV">—</strong></div>\n      <div class="mini"><span>Opportunity EV</span><strong id="arbOpportunityEV">—</strong></div>\n      <div class="mini"><span>Model / Seçim</span><strong id="arbModelGate">—</strong></div>\n    </div>\n'''
if marker not in html: raise SystemExit('dashboard grid marker missing')
html = html.replace(marker, model_grid + marker, 1)
old_js = '''  document.getElementById('arbPTB').textContent=a.ptbReady?`${pct(a.ptbPUp,1)} / ${pct(a.ptbPDown,1)} · ${directionText(a.ptbDecision)}`:'Hazır değil';\n}'''
new_js = '''  document.getElementById('arbPTB').textContent=a.ptbReady?`${pct(a.ptbPUp,1)} / ${pct(a.ptbPDown,1)} · ${directionText(a.ptbDecision)}`:'Hazır değil';\n  document.getElementById('arbFirstFillProb').textContent=`${pct(a.pFirstFill||0,1)} / ${pct(a.pFirstFullGivenFill||0,1)} · n=${a.firstFillSamples||0}`;\n  document.getElementById('arbPCompleteFast').textContent=`${pct(a.pComplete250ms||0,1)} / ${pct(a.pComplete1s||0,1)}`;\n  document.getElementById('arbPCompleteSlow').textContent=`${pct(a.pComplete2s||0,1)} / ${pct(a.pComplete5s||0,1)}`;\n  document.getElementById('arbPCompleteLower').textContent=`${pct(a.pComplete5sLower95||0,1)} / min ${pct(a.minPComplete5sLower95||0,1)} · n=${a.completionSamples||0}`;\n  document.getElementById('arbEVInputs').textContent=`${usd(a.expectedPairProfitEv||0)} / ${usd(a.expectedFullStrandedPnl||0)}`;\n  const cev=document.getElementById('arbCycleEV');cev.textContent=`${usd(a.conservativeCycleEv||0)} / min ${usd(a.minCycleEv||0)}`;cev.className=signClass(a.conservativeCycleEv||0);\n  const oev=document.getElementById('arbOpportunityEV');oev.textContent=usd(a.opportunityEv||0);oev.className=signClass(a.opportunityEv||0);\n  document.getElementById('arbModelGate').innerHTML=a.completionModelReady?`${chip('HAZIR','fresh')} · ${a.completionModelScope||'—'} · ${a.selectedBy||'—'}`:`${chip('WARMUP','warn')} · ${a.completionModelScope||'—'} · n=${a.completionSamples||0}/${a.completionMinSamples||0}`;\n}'''
if old_js not in html: raise SystemExit('updateArbLive marker missing')
html = html.replace(old_js,new_js,1)
# New reason translations.
html = html.replace("'PAIR_EDGE_BELOW_TARGET':'Net maker arbitraj avantajı hedefin altında',", "'PAIR_EDGE_BELOW_TARGET':'Planlanan maker pair edge hedefin altında','AWAITING_COMPLETION_MODEL':'Completion modeli uygulanıyor','COMPLETION_MODEL_WARMUP':'Completion modeli örnek topluyor; live fail-closed','PCOMPLETE_5S_BELOW_THRESHOLD':'5 sn completion olasılığının %95 alt sınırı yetersiz','CYCLE_EV_BELOW_THRESHOLD':'Konservatif CycleEV eşiğin altında','STRANDED_LOSS_MULTIPLE_TOO_HIGH':'Ters-bacak zarar katsayısı fazla','READY_COMPLETION_EV':'P(complete) + CycleEV kapıları geçti','PAPER_READY_MODEL_PATH_NOT_LIVE_SELECTED':'Paper araştırma yolu; live model kapıları tamamlanmadı',")
write('web/static/index.html', html)
