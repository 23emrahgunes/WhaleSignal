from pathlib import Path


def replace(path, old, new):
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f'pattern not found in {path}: {old[:120]!r}')
    p.write_text(text.replace(old, new, 1))

# -----------------------------------------------------------------------------
# Binance: make aggTrade dedupe out-of-order safe and expose PTB corridor coverage
# -----------------------------------------------------------------------------
p = Path('internal/binance/microstructure.go')
text = p.read_text()
text = text.replace(
'''\tPTBPrice           float64       `json:"ptbPrice"`\n}''',
'''\tPTBPrice           float64       `json:"ptbPrice"`\n\tPTBDistanceUSD     float64       `json:"ptbDistanceUsd"`\n\tPTBCorridorCovered bool          `json:"ptbCorridorCovered"`\n}''')
text = text.replace(
'''\tlastTradeTime  time.Time\n\tlastAggTradeID int64\n\tsource         string''',
'''\tlastTradeTime   time.Time\n\tlastAggTradeID  int64\n\tseenAggTradeIDs map[int64]time.Time\n\tsource          string''')
text = text.replace(
'''\t\ttrades:     make([]aggressiveTrade, 0, 4096),\n\t\tsource:     "UNINITIALIZED",''',
'''\t\ttrades:          make([]aggressiveTrade, 0, 4096),\n\t\tseenAggTradeIDs: make(map[int64]time.Time, 8192),\n\t\tsource:          "UNINITIALIZED",''')
old_record = '''func (c *MicrostructureClient) recordTradeWithID(price, qty float64, buyerIsMaker bool, ts time.Time, aggregateID int64) {\n\tnotional := price * qty\n\ttr := aggressiveTrade{Time: ts}\n\tif buyerIsMaker {\n\t\ttr.SellUSD = notional\n\t} else {\n\t\ttr.BuyUSD = notional\n\t}\n\tcutoff := ts.Add(-tradeFlowRetention)\n\tc.mu.Lock()\n\tdefer c.mu.Unlock()\n\tif aggregateID > 0 {\n\t\tif aggregateID <= c.lastAggTradeID {\n\t\t\treturn\n\t\t}\n\t\tc.lastAggTradeID = aggregateID\n\t}\n\tc.trades = append(c.trades, tr)\n\tfirst := 0\n\tfor first < len(c.trades) && c.trades[first].Time.Before(cutoff) {\n\t\tfirst++\n\t}\n\tif first > 0 {\n\t\tc.trades = append([]aggressiveTrade(nil), c.trades[first:]...)\n\t}\n\tif c.lastTradeTime.IsZero() || ts.After(c.lastTradeTime) {\n\t\tc.lastTradeTime = ts\n\t}\n}\n'''
new_record = '''func (c *MicrostructureClient) recordTradeWithID(price, qty float64, buyerIsMaker bool, ts time.Time, aggregateID int64) {\n\tnow := time.Now().UTC()\n\tcutoff := now.Add(-tradeFlowRetention)\n\tif ts.Before(cutoff) {\n\t\treturn\n\t}\n\tnotional := price * qty\n\ttr := aggressiveTrade{Time: ts}\n\tif buyerIsMaker {\n\t\ttr.SellUSD = notional\n\t} else {\n\t\ttr.BuyUSD = notional\n\t}\n\tc.mu.Lock()\n\tdefer c.mu.Unlock()\n\tif aggregateID > 0 {\n\t\tif _, seen := c.seenAggTradeIDs[aggregateID]; seen {\n\t\t\treturn\n\t\t}\n\t\tc.seenAggTradeIDs[aggregateID] = ts\n\t\tif aggregateID > c.lastAggTradeID {\n\t\t\tc.lastAggTradeID = aggregateID\n\t\t}\n\t}\n\tc.trades = append(c.trades, tr)\n\tkept := c.trades[:0]\n\tfor _, row := range c.trades {\n\t\tif !row.Time.Before(cutoff) {\n\t\t\tkept = append(kept, row)\n\t\t}\n\t}\n\tc.trades = kept\n\tfor id, seenAt := range c.seenAggTradeIDs {\n\t\tif seenAt.Before(cutoff) {\n\t\t\tdelete(c.seenAggTradeIDs, id)\n\t\t}\n\t}\n\tif c.lastTradeTime.IsZero() || ts.After(c.lastTradeTime) {\n\t\tc.lastTradeTime = ts\n\t}\n}\n'''
if old_record not in text:
    raise SystemExit('recordTradeWithID block not found')
text = text.replace(old_record, new_record, 1)
text = text.replace(
'''\t\tif c.tradeNeedsREST(now) {\n\t\t\tif err := c.loadRESTAggTradesFallback(); err != nil {\n\t\t\t\tutil.Logger.Warn("Binance aggTrades REST fallback failed", zap.Error(err))\n\t\t\t}\n\t\t}\n''',
'''\t\t// Always backfill aggregate trades from REST. WebSocket and REST can arrive\n\t\t// out of order; the ID seen-set deduplicates them without dropping valid\n\t\t// BUY trades that have a lower ID than a newer WS event.\n\t\tif err := c.loadRESTAggTradesFallback(); err != nil {\n\t\t\tutil.Logger.Warn("Binance aggTrades REST backfill failed", zap.Error(err))\n\t\t}\n''')
text = text.replace(
'''\tout.MidPrice = 0.5 * (out.BestBid + out.BestAsk)\n\tif currentPrice <= 0 {\n\t\tcurrentPrice = out.MidPrice\n\t}\n\tout.BidRangeUSD, out.AskRangeUSD = fullRanges(c.bids, c.asks, out.BestBid, out.BestAsk)\n''',
'''\tout.MidPrice = 0.5 * (out.BestBid + out.BestAsk)\n\tif currentPrice <= 0 {\n\t\tcurrentPrice = out.MidPrice\n\t}\n\tout.BidRangeUSD, out.AskRangeUSD = fullRanges(c.bids, c.asks, out.BestBid, out.BestAsk)\n\tif priceToBeat > 0 {\n\t\tout.PTBDistanceUSD = math.Abs(priceToBeat - currentPrice)\n\t\tout.PTBCorridorCovered = out.PTBDistanceUSD == 0 || (out.BidRangeUSD >= out.PTBDistanceUSD && out.AskRangeUSD >= out.PTBDistanceUSD)\n\t}\n''')
p.write_text(text)

# Regression test: a newer SELL ID arriving first must not suppress an older valid BUY.
p = Path('internal/binance/microstructure_test.go')
text = p.read_text()
insert = '''\nfunc TestRecordTradeWithIDPreservesOutOfOrderRESTBackfill(t *testing.T) {\n\tc := NewMicrostructureClient()\n\tnow := time.Now().UTC()\n\tc.recordTradeWithID(100, 2, true, now.Add(-time.Second), 105)  // newer aggressive sell arrives first\n\tc.recordTradeWithID(100, 3, false, now.Add(-2*time.Second), 103) // older aggressive buy arrives later via REST\n\tc.recordTradeWithID(100, 3, false, now.Add(-2*time.Second), 103) // duplicate must still be ignored\n\tc.mu.RLock()\n\tbuy, sell := tradeWindow(c.trades, now.Add(-5*time.Second))\n\tc.mu.RUnlock()\n\tif buy != 300 || sell != 200 {\n\t\tt.Fatalf("out-of-order backfill lost flow: buy %.2f sell %.2f", buy, sell)\n\t}\n}\n'''
anchor = '\nfunc TestReconcileLifePreservesFirstSeen'
if anchor not in text:
    raise SystemExit('microstructure test anchor missing')
text = text.replace(anchor, insert + anchor, 1)
p.write_text(text)

# -----------------------------------------------------------------------------
# New PTB terminal microstructure probability engine.
# Price/time/vol forecast is the prior; microstructure adjusts prior log-odds.
# -----------------------------------------------------------------------------
Path('internal/engine/ptb_terminal_probability.go').write_text(r'''package engine

import (
    "math"

    "pm-edge/internal/binance"
)

// PTBTerminalEstimate answers the market question directly: what is the
// probability that the terminal BTC reference closes above/below the PTB after
// conditioning the price/time/volatility prior on current Binance liquidity and
// aggressive trade flow? Coefficients are research priors and must be calibrated
// on out-of-sample settled markets before they can drive live/paper entries.
type PTBTerminalEstimate struct {
    Ready                 bool    `json:"ready"`
    CorridorCovered       bool    `json:"corridorCovered"`
    PTBDistanceUSD        float64 `json:"ptbDistanceUsd"`
    PriorPAbove           float64 `json:"priorPAbove"`
    PriorPBelow           float64 `json:"priorPBelow"`
    PAbove                float64 `json:"pAbove"`
    PBelow                float64 `json:"pBelow"`
    Decision              string  `json:"decision"`
    Confidence            float64 `json:"confidence"`
    BuyRateUSDPerSec      float64 `json:"buyRateUsdPerSec"`
    SellRateUSDPerSec     float64 `json:"sellRateUsdPerSec"`
    UpCoverage            float64 `json:"upCoverage"`
    DownCoverage          float64 `json:"downCoverage"`
    FlowCapacityScore     float64 `json:"flowCapacityScore"`
    MicroEvidenceScore    float64 `json:"microEvidenceScore"`
    Urgency               float64 `json:"urgency"`
    LogOddsAdjustment     float64 `json:"logOddsAdjustment"`
}

func EstimatePTBTerminalMicroProbability(priorPAbove, secondsRemaining, currentPrice, priceToBeat float64, s binance.DeepMicroSnapshot, m MicrostructureScores) PTBTerminalEstimate {
    prior := clampProbability(priorPAbove)
    out := PTBTerminalEstimate{
        CorridorCovered: s.PTBCorridorCovered,
        PTBDistanceUSD:  math.Abs(priceToBeat - currentPrice),
        PriorPAbove:     prior,
        PriorPBelow:     1 - prior,
        PAbove:          prior,
        PBelow:          1 - prior,
        Decision:        terminalDecision(prior),
        Confidence:      math.Abs(prior-0.5) * 200,
    }
    if secondsRemaining <= 0 || currentPrice <= 0 || priceToBeat <= 0 || !m.Ready || !s.TradeFlowAvailable || !s.PTBCorridorCovered {
        return out
    }

    buyRate, sellRate := blendedAggressiveRates(s.Trades)
    out.BuyRateUSDPerSec = buyRate
    out.SellRateUSDPerSec = sellRate
    if buyRate+sellRate <= 0 {
        return out
    }

    // Do not project a transient flow regime across an entire 15m market. At
    // most 60 seconds of current aggressor flow is assumed to persist.
    flowHorizon := math.Min(secondsRemaining, 60)
    if flowHorizon < 5 {
        flowHorizon = 5
    }

    if currentPrice < priceToBeat {
        // To finish above PTB, aggressive buyers must consume asks on the path;
        // sellers are compared with the bid support behind spot.
        upBarrier := s.PTBPathAskUSD + 0.35*s.PTBBeyondUSD
        downSupport := s.PTBPathBidUSD
        out.UpCoverage = safeCoverage(buyRate*flowHorizon, upBarrier)
        out.DownCoverage = safeCoverage(sellRate*flowHorizon, downSupport)
    } else {
        // Already above PTB: selling pressure threatens to consume the bid path
        // down to PTB, while buying pressure reinforces staying above it.
        downBarrier := s.PTBPathBidUSD + 0.35*s.PTBBeyondUSD
        upResistance := s.PTBPathAskUSD
        out.UpCoverage = safeCoverage(buyRate*flowHorizon, upResistance)
        out.DownCoverage = safeCoverage(sellRate*flowHorizon, downBarrier)
    }
    out.FlowCapacityScore = math.Tanh(math.Log((1+out.UpCoverage)/(1+out.DownCoverage)))

    // PTB path and actual executed flow receive most weight. ±$50/$75 book and
    // wall dynamics are supporting evidence, not substitutes for the target path.
    out.MicroEvidenceScore = clampScore(
        0.35*m.PTBBarrierScore+
            0.30*out.FlowCapacityScore+
            0.20*m.TradeFlowScore+
            0.10*m.DeepBookScore+
            0.05*m.WallDynamicsScore,
    )

    // Microstructure becomes more informative as expiry approaches, while being
    // deliberately damped far from expiry. 120s is the neutral reference point.
    out.Urgency = math.Sqrt(120 / math.Max(secondsRemaining, 15))
    if out.Urgency < 0.35 {
        out.Urgency = 0.35
    } else if out.Urgency > 1.50 {
        out.Urgency = 1.50
    }

    // Bayesian-style odds update: posterior odds = prior odds * exp(adjustment).
    // 1.35 is an intentionally bounded shadow research scale, not a fitted beta.
    out.LogOddsAdjustment = 1.35 * out.Urgency * out.MicroEvidenceScore
    priorLogOdds := math.Log(prior / (1 - prior))
    out.PAbove = logistic(priorLogOdds + out.LogOddsAdjustment)
    out.PBelow = 1 - out.PAbove
    out.Decision = terminalDecision(out.PAbove)
    out.Confidence = math.Abs(out.PAbove-0.5) * 200
    out.Ready = true
    return out
}

func blendedAggressiveRates(rows []binance.TradeWindow) (float64, float64) {
    weights := map[int]float64{15: 0.55, 30: 0.30, 60: 0.15}
    buyRate, sellRate, totalWeight := 0.0, 0.0, 0.0
    for _, row := range rows {
        w, ok := weights[row.Seconds]
        if !ok || row.Seconds <= 0 {
            continue
        }
        buyRate += w * row.BuyUSD / float64(row.Seconds)
        sellRate += w * row.SellUSD / float64(row.Seconds)
        totalWeight += w
    }
    if totalWeight <= 0 {
        return 0, 0
    }
    return buyRate / totalWeight, sellRate / totalWeight
}

func safeCoverage(flowCapacity, barrier float64) float64 {
    if flowCapacity <= 0 {
        return 0
    }
    if barrier <= 0 {
        return 10
    }
    v := flowCapacity / barrier
    if v > 10 {
        return 10
    }
    return v
}

func clampProbability(p float64) float64 {
    if p < 0.02 {
        return 0.02
    }
    if p > 0.98 {
        return 0.98
    }
    return p
}

func logistic(x float64) float64 {
    if x >= 0 {
        z := math.Exp(-x)
        return 1 / (1 + z)
    }
    z := math.Exp(x)
    return z / (1 + z)
}

func terminalDecision(pAbove float64) string {
    if pAbove >= 0.55 {
        return "UP"
    }
    if pAbove <= 0.45 {
        return "DOWN"
    }
    return "NEUTRAL"
}
''')

Path('internal/engine/ptb_terminal_probability_test.go').write_text(r'''package engine

import (
    "testing"

    "pm-edge/internal/binance"
)

func terminalSnapshot() binance.DeepMicroSnapshot {
    return binance.DeepMicroSnapshot{
        Ready: true, Synchronized: true, TradeFlowAvailable: true,
        PTBCorridorCovered: true, PTBPathBidUSD: 4_000_000, PTBPathAskUSD: 2_000_000, PTBBeyondUSD: 1_000_000,
        Trades: []binance.TradeWindow{
            {Seconds: 15, BuyUSD: 3_000_000, SellUSD: 1_000_000, Imbalance: 0.5},
            {Seconds: 30, BuyUSD: 5_000_000, SellUSD: 2_000_000, Imbalance: 0.4286},
            {Seconds: 60, BuyUSD: 8_000_000, SellUSD: 4_000_000, Imbalance: 0.3333},
        },
    }
}

func TestPTBTerminalBullishMicroRaisesPrior(t *testing.T) {
    s := terminalSnapshot()
    m := MicrostructureScores{Ready: true, PTBBarrierScore: 0.6, TradeFlowScore: 0.5, DeepBookScore: 0.3, WallDynamicsScore: 0.2}
    got := EstimatePTBTerminalMicroProbability(0.60, 60, 100, 105, s, m)
    if !got.Ready || got.PAbove <= 0.60 || got.FlowCapacityScore <= 0 {
        t.Fatalf("bullish microstructure did not raise prior: %+v", got)
    }
}

func TestPTBTerminalBearishMicroLowersPrior(t *testing.T) {
    s := terminalSnapshot()
    s.PTBPathBidUSD, s.PTBPathAskUSD = 1_000_000, 7_000_000
    s.Trades = []binance.TradeWindow{
        {Seconds: 15, BuyUSD: 300_000, SellUSD: 2_000_000, Imbalance: -0.739},
        {Seconds: 30, BuyUSD: 700_000, SellUSD: 4_000_000, Imbalance: -0.702},
        {Seconds: 60, BuyUSD: 1_200_000, SellUSD: 7_000_000, Imbalance: -0.707},
    }
    m := MicrostructureScores{Ready: true, PTBBarrierScore: -0.7, TradeFlowScore: -0.7, DeepBookScore: -0.4, WallDynamicsScore: -0.2}
    got := EstimatePTBTerminalMicroProbability(0.60, 60, 100, 105, s, m)
    if !got.Ready || got.PAbove >= 0.60 || got.FlowCapacityScore >= 0 {
        t.Fatalf("bearish microstructure did not lower prior: %+v", got)
    }
}

func TestPTBTerminalRequiresFullCorridorCoverage(t *testing.T) {
    s := terminalSnapshot()
    s.PTBCorridorCovered = false
    got := EstimatePTBTerminalMicroProbability(0.70, 60, 100, 180, s, MicrostructureScores{Ready: true})
    if got.Ready || got.PAbove != got.PriorPAbove {
        t.Fatalf("uncovered corridor should stay at prior and not be ready: %+v", got)
    }
}

func TestPTBTerminalNearExpiryAmplifiesSameEvidence(t *testing.T) {
    s := terminalSnapshot()
    m := MicrostructureScores{Ready: true, PTBBarrierScore: 0.4, TradeFlowScore: 0.4, DeepBookScore: 0.2, WallDynamicsScore: 0.1}
    near := EstimatePTBTerminalMicroProbability(0.50, 30, 100, 105, s, m)
    far := EstimatePTBTerminalMicroProbability(0.50, 300, 100, 105, s, m)
    if !near.Ready || !far.Ready || near.PAbove <= far.PAbove {
        t.Fatalf("near-expiry evidence should have larger effect: near=%+v far=%+v", near, far)
    }
}
''')

# -----------------------------------------------------------------------------
# Evaluator: compute and expose the direct PTB terminal probability.
# -----------------------------------------------------------------------------
p = Path('internal/engine/evaluator.go')
text = p.read_text()
text = text.replace(
'''\tShadowModelBScore   float64                   `json:"shadowModelBScore"`\n\tShadowDecision      string                    `json:"shadowDecision"`\n\tShadowConfidence    float64                   `json:"shadowConfidence"`\n}''',
'''\tShadowModelBScore   float64                   `json:"shadowModelBScore"`\n\tShadowDecision      string                    `json:"shadowDecision"`\n\tShadowConfidence    float64                   `json:"shadowConfidence"`\n\tPTBTerminal         PTBTerminalEstimate       `json:"ptbTerminal"`\n}''')
text = text.replace(
'''\tshadowDecision := "WAITING"\n\tshadowConfidence := 0.0\n\tif e.micro != nil {\n\t\tbinancePTB := binanceEquivalentPTB(priceToBeat, currentPrice, binanceSpot)\n\t\tdeep = e.micro.Snapshot(binanceSpot, binancePTB, nowTime)\n\t\tmicroScores = ScoreMicrostructure(deep)\n\t\tif microScores.Ready {\n\t\t\tshadowScore = ShadowModelB(probabilityScore, technicalScore, microScores)\n\t\t\tshadowDecision, shadowConfidence = ShadowDecision(shadowScore)\n\t\t}\n\t}\n''',
'''\tshadowDecision := "WAITING"\n\tshadowConfidence := 0.0\n\tptbTerminal := PTBTerminalEstimate{}\n\tif e.micro != nil {\n\t\tbinancePTB := binanceEquivalentPTB(priceToBeat, currentPrice, binanceSpot)\n\t\tdeep = e.micro.Snapshot(binanceSpot, binancePTB, nowTime)\n\t\tmicroScores = ScoreMicrostructure(deep)\n\t\tif microScores.Ready {\n\t\t\tshadowScore = ShadowModelB(probabilityScore, technicalScore, microScores)\n\t\t\tshadowDecision, shadowConfidence = ShadowDecision(shadowScore)\n\t\t\tptbTerminal = EstimatePTBTerminalMicroProbability(pUp, secondsRemaining, binanceSpot, binancePTB, deep, microScores)\n\t\t}\n\t}\n''')
text = text.replace(
'''\t\tShadowModelBScore:   shadowScore,\n\t\tShadowDecision:      shadowDecision,\n\t\tShadowConfidence:    shadowConfidence,''',
'''\t\tShadowModelBScore:   shadowScore,\n\t\tShadowDecision:      shadowDecision,\n\t\tShadowConfidence:    shadowConfidence,\n\t\tPTBTerminal:         ptbTerminal,''')
p.write_text(text)

# -----------------------------------------------------------------------------
# Persist PTB terminal estimate as JSON in microstructure history.
# -----------------------------------------------------------------------------
p = Path('internal/storage/microstructure.go')
text = p.read_text()
text = text.replace('import (\n\t"fmt"', 'import (\n\t"encoding/json"\n\t"fmt"')
text = text.replace(
'''\tShadowConfidence    float64 `json:"shadowConfidence"`\n}''',
'''\tShadowConfidence    float64                    `json:"shadowConfidence"`\n\tPTBTerminal         engine.PTBTerminalEstimate `json:"ptbTerminal"`\n}''')
text = text.replace(
'''\t\tshadow_model_b_score REAL NOT NULL, shadow_decision TEXT NOT NULL, shadow_confidence REAL NOT NULL,\n\t\tUNIQUE(timestamp, slug)''',
'''\t\tshadow_model_b_score REAL NOT NULL, shadow_decision TEXT NOT NULL, shadow_confidence REAL NOT NULL,\n\t\tptb_terminal_json TEXT NOT NULL DEFAULT '{}',\n\t\tUNIQUE(timestamp, slug)''')
text = text.replace(
'''\t`)\n\treturn err\n}\n\nfunc (d *Database) InsertMicrostructureSnapshot''',
'''\t`)\n\tif err != nil {\n\t\treturn err\n\t}\n\treturn d.ensureMicrostructureTerminalColumn()\n}\n\nfunc (d *Database) ensureMicrostructureTerminalColumn() error {\n\trows, err := d.db.Query("PRAGMA table_info(microstructure_snapshots)")\n\tif err != nil {\n\t\treturn err\n\t}\n\thas := false\n\tfor rows.Next() {\n\t\tvar cid int\n\t\tvar name, typ string\n\t\tvar notnull, pk int\n\t\tvar defaultValue interface{}\n\t\tif err := rows.Scan(&cid, &name, &typ, &notnull, &defaultValue, &pk); err != nil {\n\t\t\trows.Close()\n\t\t\treturn err\n\t\t}\n\t\tif name == "ptb_terminal_json" {\n\t\t\thas = true\n\t\t}\n\t}\n\tif err := rows.Close(); err != nil {\n\t\treturn err\n\t}\n\tif has {\n\t\treturn nil\n\t}\n\t_, err = d.db.Exec("ALTER TABLE microstructure_snapshots ADD COLUMN ptb_terminal_json TEXT NOT NULL DEFAULT '{}'")\n\treturn err\n}\n\nfunc (d *Database) InsertMicrostructureSnapshot''')
text = text.replace(
'''\t\tm.ShadowModelBScore, m.ShadowDecision, m.ShadowConfidence)\n\treturn err\n}\n''',
'''\t\tm.ShadowModelBScore, m.ShadowDecision, m.ShadowConfidence)\n\tif err != nil {\n\t\treturn err\n\t}\n\tpayload, err := json.Marshal(r.PTBTerminal)\n\tif err != nil {\n\t\treturn err\n\t}\n\t_, err = d.db.Exec(`UPDATE microstructure_snapshots SET ptb_terminal_json=? WHERE timestamp=? AND slug=?`, string(payload), r.Timestamp, r.Slug)\n\treturn err\n}\n''')
text = text.replace(
'''\t\tshadow_model_b_score, shadow_decision, shadow_confidence\n\t\tFROM microstructure_snapshots''',
'''\t\tshadow_model_b_score, shadow_decision, shadow_confidence, ptb_terminal_json\n\t\tFROM microstructure_snapshots''')
text = text.replace(
'''\t\tvar m MicrostructureSnapshot\n\t\tvar ready, synced int\n''',
'''\t\tvar m MicrostructureSnapshot\n\t\tvar ready, synced int\n\t\tvar terminalJSON string\n''')
text = text.replace(
'''\t\t\t&m.ShadowModelBScore, &m.ShadowDecision, &m.ShadowConfidence); err != nil {\n\t\t\treturn nil, err\n\t\t}\n\t\tm.Ready = ready == 1''',
'''\t\t\t&m.ShadowModelBScore, &m.ShadowDecision, &m.ShadowConfidence, &terminalJSON); err != nil {\n\t\t\treturn nil, err\n\t\t}\n\t\tif terminalJSON != "" {\n\t\t\t_ = json.Unmarshal([]byte(terminalJSON), &m.PTBTerminal)\n\t\t}\n\t\tm.Ready = ready == 1''')
text = text.replace(
'''ShadowConfidence: r.ShadowConfidence}''',
'''ShadowConfidence: r.ShadowConfidence, PTBTerminal: r.PTBTerminal}''')
p.write_text(text)

p = Path('internal/storage/microstructure_test.go')
text = p.read_text()
text = text.replace(
'''\t\tMicrostructureScore: 0.08, ShadowModelBScore: 0.24, ShadowDecision: "UP", ShadowConfidence: 24,\n\t}''',
'''\t\tMicrostructureScore: 0.08, ShadowModelBScore: 0.24, ShadowDecision: "UP", ShadowConfidence: 24,\n\t\tPTBTerminal: engine.PTBTerminalEstimate{Ready: true, CorridorCovered: true, PTBDistanceUSD: 25, PriorPAbove: 0.60, PriorPBelow: 0.40, PAbove: 0.68, PBelow: 0.32, Decision: "UP", Confidence: 36, FlowCapacityScore: 0.2},\n\t}''')
text = text.replace(
'''\tif got.Band10BidUSD != 1000 || got.Trade5BuyUSD != 2000 || got.ShadowDecision != "UP" || got.PTBBarrierScore != -0.42 {''',
'''\tif got.Band10BidUSD != 1000 || got.Trade5BuyUSD != 2000 || got.ShadowDecision != "UP" || got.PTBBarrierScore != -0.42 || !got.PTBTerminal.Ready || got.PTBTerminal.PAbove != 0.68 {''')
p.write_text(text)

# -----------------------------------------------------------------------------
# Dashboard: make the direct terminal probability visible in Turkish.
# -----------------------------------------------------------------------------
p = Path('web/static/index.html')
text = p.read_text()
text = text.replace(
'''      <div class="mini"><span>Gölge Model B</span><strong style="font-size:13px">Yeni ±$10/$25/$50/$75 derinlik ve gerçek alış/satış akışını ölçen test modelidir. Şimdilik kağıt işlemi açmaz.</strong></div>''',
'''      <div class="mini"><span>Gölge Model B</span><strong style="font-size:13px">Yeni ±$10/$25/$50/$75 derinlik ve gerçek alış/satış akışını ölçen test modelidir. Şimdilik kağıt işlemi açmaz.</strong></div>\n      <div class="mini"><span>PTB Terminal Olasılığı</span><strong style="font-size:13px">Fiyat-zaman-oynaklık tahminini başlangıç kabul eder; PTB yolundaki likidite, gerçek agresif alış/satış hızı ve duvar dinamikleriyle güncelleyerek kapanışın hedef fiyatın üstünde/altında olma olasılığını hesaplar.</strong></div>''')
text = text.replace(
'''      <div class="mini"><span>Giriş Ekonomik Avantajı</span><strong id="economicEdge">—</strong></div>''',
'''      <div class="mini"><span>PTB Üstünde / Altında Kapanış Olasılığı</span><strong id="ptbTerminalProb">—</strong></div>\n      <div class="mini"><span>PTB Terminal Yönü / Güveni</span><strong id="ptbTerminalDecision">—</strong></div>\n      <div class="mini"><span>Agresif Alış / Satış Hızı</span><strong id="ptbFlowRate">—</strong></div>\n      <div class="mini"><span>PTB Akış Kapasitesi / Mikroyapı Kanıtı</span><strong id="ptbFlowCapacity">—</strong></div>\n      <div class="mini"><span>PTB Koridoru Kapsanıyor mu?</span><strong id="ptbCorridor">—</strong></div>\n      <div class="mini"><span>Giriş Ekonomik Avantajı</span><strong id="economicEdge">—</strong></div>''')
text = text.replace(
'''  document.getElementById('ptbPath').textContent=`Alış desteği ${usdCompact(dm.ptbPathBidUsd||0)} / Satış bariyeri ${usdCompact(dm.ptbPathAskUsd||0)} · ${pct(data.ptbBarrierScore||0)}`;\n''',
'''  document.getElementById('ptbPath').textContent=`Alış desteği ${usdCompact(dm.ptbPathBidUsd||0)} / Satış bariyeri ${usdCompact(dm.ptbPathAskUsd||0)} · ${pct(data.ptbBarrierScore||0)}`;\n  const pt=data.ptbTerminal||{};\n  document.getElementById('ptbTerminalProb').textContent=pt.ready?`${pct(pt.pAbove,1)} / ${pct(pt.pBelow,1)}`:`Öncül: ${pct(pt.priorPAbove||data.pUp,1)} / ${pct(pt.priorPBelow||data.pDown,1)}`;\n  document.getElementById('ptbTerminalDecision').innerHTML=pt.ready?`${decisionChip(pt.decision)} · ${pctDirect(pt.confidence||0,1)}`:chip('VERİ TAMAMLANIYOR','warn');\n  document.getElementById('ptbFlowRate').textContent=pt.ready?`Alış ${usdCompact(pt.buyRateUsdPerSec)}/sn · Satış ${usdCompact(pt.sellRateUsdPerSec)}/sn`:'—';\n  document.getElementById('ptbFlowCapacity').textContent=pt.ready?`${pct(pt.flowCapacityScore,1)} / ${pct(pt.microEvidenceScore,1)}`:'—';\n  document.getElementById('ptbCorridor').innerHTML=pt.corridorCovered?chip(`EVET · ${usd(pt.ptbDistanceUsd||0)}`,'fresh'):chip(`HAYIR · ${usd(pt.ptbDistanceUsd||0)}`,'warn');\n''')
p.write_text(text)

print('PTB terminal microstructure patch applied')
