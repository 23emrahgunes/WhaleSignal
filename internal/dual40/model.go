package dual40

import (
	"math"
	"sort"
)

const StrategyMode = "DUAL_40_REGIME_HEDGE_V1"

type Config struct {
	EntryPrice          float64
	Shares              float64
	EntrySeconds        []int
	MinChopScore        float64
	MinRangeBps         float64
	MaxRangeBps         float64
	MaxAbsDriftBps      float64
	MaxAbsFlowImbalance float64
	MaxPolySkew         float64
	OrderTTLSec         int
	HedgeMaxWaitSec     int
	HedgeTriggerPrice   float64
	StopBeforeEndSec    int
	// GateMode: "feature" (varsayilan) => ChopScore/skew/drift/flow VETO DEGIL,
	// yalnizca feature olarak loglanir; trial kitap-gate gecince POST edilir
	// (genis-shadow veri toplamak icin). "hard" => eski davranis (Eligible veto).
	GateMode string
}

func DefaultConfig() Config {
	return Config{
		EntryPrice:          0.40,
		Shares:              5,
		EntrySeconds:        []int{5, 10, 20},
		MinChopScore:        70,
		MinRangeBps:         0.8,
		MaxRangeBps:         8.0,
		MaxAbsDriftBps:      4.0,
		MaxAbsFlowImbalance: 0.35,
		MaxPolySkew:         0.12,
		OrderTTLSec:         60,
		HedgeMaxWaitSec:     12,
		HedgeTriggerPrice:   0.70,
		StopBeforeEndSec:    20,
		GateMode:            "feature",
	}
}

type Sample struct {
	ElapsedSec    float64 `json:"elapsedSec"`
	Price         float64 `json:"price"`
	FlowImbalance float64 `json:"flowImbalance"`
	UpMid         float64 `json:"upMid"`
	DownMid       float64 `json:"downMid"`
}

type Metrics struct {
	Samples          int     `json:"samples"`
	WindowSec        float64 `json:"windowSec"`
	DriftBps         float64 `json:"driftBps"`
	RangeBps         float64 `json:"rangeBps"`
	RealizedVolBps   float64 `json:"realizedVolBps"`
	ReversalRate     float64 `json:"reversalRate"`
	MeanFlow         float64 `json:"meanFlow"`
	MeanAbsFlow      float64 `json:"meanAbsFlow"`
	PolySkew         float64 `json:"polySkew"`
	DirectionalRatio float64 `json:"directionalRatio"`
	ChopScore        float64 `json:"chopScore"`
	Regime           string  `json:"regime"`
	Eligible         bool    `json:"eligible"`
	Reason           string  `json:"reason"`
}

func NormalizeConfig(cfg Config) Config {
	def := DefaultConfig()
	if cfg.EntryPrice <= 0 || cfg.EntryPrice >= 1 {
		cfg.EntryPrice = def.EntryPrice
	}
	if cfg.Shares <= 0 {
		cfg.Shares = def.Shares
	}
	if len(cfg.EntrySeconds) == 0 {
		cfg.EntrySeconds = append([]int(nil), def.EntrySeconds...)
	}
	seen := make(map[int]struct{}, len(cfg.EntrySeconds))
	clean := make([]int, 0, len(cfg.EntrySeconds))
	for _, sec := range cfg.EntrySeconds {
		if sec <= 0 {
			continue
		}
		if _, ok := seen[sec]; ok {
			continue
		}
		seen[sec] = struct{}{}
		clean = append(clean, sec)
	}
	if len(clean) == 0 {
		clean = append(clean, def.EntrySeconds...)
	}
	sort.Ints(clean)
	cfg.EntrySeconds = clean
	if cfg.MinChopScore <= 0 {
		cfg.MinChopScore = def.MinChopScore
	}
	if cfg.MinRangeBps <= 0 {
		cfg.MinRangeBps = def.MinRangeBps
	}
	if cfg.MaxRangeBps <= cfg.MinRangeBps {
		cfg.MaxRangeBps = def.MaxRangeBps
	}
	if cfg.MaxAbsDriftBps <= 0 {
		cfg.MaxAbsDriftBps = def.MaxAbsDriftBps
	}
	if cfg.MaxAbsFlowImbalance <= 0 || cfg.MaxAbsFlowImbalance > 1 {
		cfg.MaxAbsFlowImbalance = def.MaxAbsFlowImbalance
	}
	if cfg.MaxPolySkew <= 0 || cfg.MaxPolySkew >= 1 {
		cfg.MaxPolySkew = def.MaxPolySkew
	}
	if cfg.OrderTTLSec <= 0 {
		cfg.OrderTTLSec = def.OrderTTLSec
	}
	if cfg.HedgeMaxWaitSec <= 0 {
		cfg.HedgeMaxWaitSec = def.HedgeMaxWaitSec
	}
	if cfg.HedgeTriggerPrice <= cfg.EntryPrice || cfg.HedgeTriggerPrice >= 1 {
		cfg.HedgeTriggerPrice = def.HedgeTriggerPrice
	}
	if cfg.StopBeforeEndSec <= 0 {
		cfg.StopBeforeEndSec = def.StopBeforeEndSec
	}
	if cfg.GateMode != "hard" {
		cfg.GateMode = "feature"
	}
	return cfg
}

func SamplesThrough(samples []Sample, maxElapsed float64) []Sample {
	out := make([]Sample, 0, len(samples))
	for _, s := range samples {
		if s.ElapsedSec <= maxElapsed+0.75 {
			out = append(out, s)
		}
	}
	return out
}

func OpeningWindowCovered(samples []Sample, entrySecond int) bool {
	if entrySecond <= 0 || len(samples) < 4 {
		return false
	}
	first := samples[0].ElapsedSec
	last := samples[len(samples)-1].ElapsedSec
	if first > 3.0 || last+0.75 < float64(entrySecond) {
		return false
	}
	minSamples := entrySecond / 2
	if minSamples < 4 {
		minSamples = 4
	}
	return len(samples) >= minSamples
}

func Classify(samples []Sample, cfg Config) Metrics {
	cfg = NormalizeConfig(cfg)
	m := Metrics{Regime: "INSUFFICIENT", Reason: "INSUFFICIENT_OPENING_WINDOW"}
	valid := make([]Sample, 0, len(samples))
	for _, s := range samples {
		if s.Price > 0 {
			valid = append(valid, s)
		}
	}
	if len(valid) < 4 {
		return m
	}
	m.Samples = len(valid)
	m.WindowSec = valid[len(valid)-1].ElapsedSec - valid[0].ElapsedSec
	firstPrice := valid[0].Price
	lastPrice := valid[len(valid)-1].Price
	m.DriftBps = (lastPrice - firstPrice) / firstPrice * 10000

	minPrice, maxPrice := firstPrice, firstPrice
	returns := make([]float64, 0, len(valid)-1)
	signs := make([]int, 0, len(valid)-1)
	flowSum, absFlowSum, polySkewSum := 0.0, 0.0, 0.0
	polyCount := 0
	for i, s := range valid {
		if s.Price < minPrice {
			minPrice = s.Price
		}
		if s.Price > maxPrice {
			maxPrice = s.Price
		}
		flow := clamp(s.FlowImbalance, -1, 1)
		flowSum += flow
		absFlowSum += math.Abs(flow)
		if s.UpMid > 0 && s.DownMid > 0 {
			polySkewSum += math.Abs(s.UpMid - s.DownMid)
			polyCount++
		}
		if i == 0 {
			continue
		}
		prev := valid[i-1].Price
		if prev <= 0 {
			continue
		}
		r := (s.Price - prev) / prev * 10000
		returns = append(returns, r)
		if math.Abs(r) < 0.01 {
			continue
		}
		if r > 0 {
			signs = append(signs, 1)
		} else {
			signs = append(signs, -1)
		}
	}
	m.RangeBps = (maxPrice - minPrice) / firstPrice * 10000
	m.MeanFlow = flowSum / float64(len(valid))
	m.MeanAbsFlow = absFlowSum / float64(len(valid))
	if polyCount > 0 {
		m.PolySkew = polySkewSum / float64(polyCount)
	}
	if len(returns) > 0 {
		mean := 0.0
		for _, r := range returns {
			mean += r
		}
		mean /= float64(len(returns))
		variance := 0.0
		for _, r := range returns {
			d := r - mean
			variance += d * d
		}
		variance /= float64(len(returns))
		m.RealizedVolBps = math.Sqrt(variance)
	}
	if len(signs) > 1 {
		changes := 0
		for i := 1; i < len(signs); i++ {
			if signs[i] != signs[i-1] {
				changes++
			}
		}
		m.ReversalRate = float64(changes) / float64(len(signs)-1)
	}
	if m.RangeBps > 1e-9 {
		m.DirectionalRatio = math.Abs(m.DriftBps) / m.RangeBps
	}

	driftScore := clamp01(1 - math.Abs(m.DriftBps)/cfg.MaxAbsDriftBps)
	idealRange := cfg.MinRangeBps + 0.35*(cfg.MaxRangeBps-cfg.MinRangeBps)
	volScore := triangularBand(m.RangeBps, cfg.MinRangeBps, idealRange, cfg.MaxRangeBps)
	reversalScore := clamp01(m.ReversalRate / 0.50)
	flowScore := clamp01(1 - math.Abs(m.MeanFlow)/cfg.MaxAbsFlowImbalance)
	polyScore := clamp01(1 - m.PolySkew/cfg.MaxPolySkew)
	m.ChopScore = 100 * (0.25*driftScore + 0.20*volScore + 0.20*reversalScore + 0.20*flowScore + 0.15*polyScore)

	switch {
	case m.RangeBps < cfg.MinRangeBps:
		m.Regime = "DEAD_FLAT"
		m.Reason = "RANGE_TOO_LOW"
	case m.RangeBps > cfg.MaxRangeBps:
		m.Regime = "VOLATILE"
		m.Reason = "RANGE_TOO_HIGH"
	case math.Abs(m.DriftBps) > cfg.MaxAbsDriftBps:
		if m.DriftBps > 0 {
			m.Regime = "TREND_UP"
		} else {
			m.Regime = "TREND_DOWN"
		}
		m.Reason = "DIRECTIONAL_DRIFT"
	case math.Abs(m.MeanFlow) > cfg.MaxAbsFlowImbalance:
		if m.MeanFlow > 0 {
			m.Regime = "FLOW_UP"
		} else {
			m.Regime = "FLOW_DOWN"
		}
		m.Reason = "FLOW_IMBALANCE"
	case m.PolySkew > cfg.MaxPolySkew:
		m.Regime = "POLY_SKEWED"
		m.Reason = "POLYMARKET_SKEW"
	case m.ChopScore < cfg.MinChopScore:
		m.Regime = "MIXED"
		m.Reason = "CHOP_SCORE_LOW"
	default:
		m.Regime = "CHOP"
		m.Reason = "ELIGIBLE_CHOP"
		m.Eligible = true
	}
	return m
}

func AdaptiveHedgeTrigger(m Metrics, cfg Config) float64 {
	cfg = NormalizeConfig(cfg)
	trigger := cfg.HedgeTriggerPrice
	switch {
	case m.Regime == "TREND_UP" || m.Regime == "TREND_DOWN" || m.Regime == "FLOW_UP" || m.Regime == "FLOW_DOWN":
		trigger = math.Min(trigger, 0.60)
	case m.ChopScore < 50:
		trigger = math.Min(trigger, 0.62)
	case m.ChopScore < 60:
		trigger = math.Min(trigger, 0.65)
	case m.ChopScore < cfg.MinChopScore:
		trigger = math.Min(trigger, 0.68)
	}
	if trigger <= cfg.EntryPrice {
		trigger = cfg.EntryPrice + 0.01
	}
	return trigger
}

func triangularBand(v, low, peak, high float64) float64 {
	if high <= low || peak <= low || peak >= high || v <= low || v >= high {
		return 0
	}
	if v <= peak {
		return clamp01((v - low) / (peak - low))
	}
	return clamp01((high - v) / (high - peak))
}

func clamp01(v float64) float64 { return clamp(v, 0, 1) }

func clamp(v, lo, hi float64) float64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}
