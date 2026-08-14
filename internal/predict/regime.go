// Package predict: Predictability/Regime + Direction motorlari (SHADOW-first).
// Onak: once market TAHMIN EDILEBILIR mi/kaotik mi karar ver; yalniz temiz
// mikroyapida yon uret; aksi halde ABSTAIN. Esikler DISARIDAN (config) verilir,
// matematiksel gercek gibi hard-code EDILMEZ — shadow veriden kalibre edilir.
package predict

import "math"

type Regime string

const (
	RegimeChop      Regime = "CHOP"
	RegimeTrendUp   Regime = "TREND_UP"
	RegimeTrendDown Regime = "TREND_DOWN"
	RegimeHighVol   Regime = "HIGH_VOL"
	RegimeChaotic   Regime = "CHAOTIC"
	RegimeUnsafe    Regime = "UNSAFE"
)

// Features: regime/predictability icin ozet mikroyapi ozellikleri. microfeat
// cekirdeginden (ve toplayicidan) doldurulur.
type Features struct {
	BandCoherence        float64 // 0..1 (bantlar arasi yon-uyumu)
	OBIFlipRate          float64 // 0..1 (yuksek -> kaotik book)
	OBISignPersistence   float64 // 0..1
	FlowPersistence      float64 // 0..1
	FlowAcceleration     float64
	DirectionConsistency float64 // isaretli [-1,1] (net yon egilimi)
	RealizedVolBps       float64 // kisa-vade gerceklesmis vol (bps)
	TrendZ               float64 // vol-normalize net drift
	Spread               float64
	DataAgeMs            float64 // en taze feature yasi
	HistorySamples       int     // ring-buffer'daki ornek sayisi
}

// Thresholds: config'ten gelir. Kalibrasyona acik baslangic degerleri.
type Thresholds struct {
	PredictabilityMin float64 // >= ise PREDICTABLE (regime uygunsa)
	CoherenceMin      float64 // < ise BOOK_FLOW_CONFLICT / kaotik
	MaxFlipRate       float64 // > ise HIGH_OBI_FLIP_RATE / kaotik
	HighVolBps        float64 // > ise HIGH_VOL
	TrendZMin         float64 // |trend_z| >= ve tutarli ise TREND_*
	MaxDataAgeMs      float64 // > ise STALE_DATA / UNSAFE
	MinHistory        int     // < ise INSUFFICIENT_HISTORY / UNSAFE
}

// Result: predictability motoru cikti.
type Result struct {
	PredictabilityScore float64  `json:"predictabilityScore"`
	Regime              Regime   `json:"regime"`
	Predictable         bool     `json:"predictable"`
	Reasons             []string `json:"reasons"` // TUM aktif red nedenleri
}

// Classify: feature + esiklerden predictability_score + regime + PREDICTABLE?
// uretir. Kaotik/unsafe/highvol -> Predictable=false (ust katman ABSTAIN eder).
func Classify(f Features, t Thresholds) Result {
	var reasons []string

	// 1) Guvenlik/veri saglik -> UNSAFE (yon uretme).
	unsafe := false
	if t.MinHistory > 0 && f.HistorySamples < t.MinHistory {
		reasons = append(reasons, "INSUFFICIENT_HISTORY")
		unsafe = true
	}
	if t.MaxDataAgeMs > 0 && f.DataAgeMs > t.MaxDataAgeMs {
		reasons = append(reasons, "STALE_DATA")
		unsafe = true
	}

	// 2) Kaotik gostergeleri (esikler config).
	lowCoherence := t.CoherenceMin > 0 && f.BandCoherence < t.CoherenceMin
	highFlip := t.MaxFlipRate > 0 && f.OBIFlipRate > t.MaxFlipRate
	if lowCoherence {
		reasons = append(reasons, "LOW_BAND_COHERENCE")
	}
	if highFlip {
		reasons = append(reasons, "HIGH_OBI_FLIP_RATE")
	}
	// book (OBI) ve flow yonu celisiyorsa -> BOOK_FLOW_CONFLICT
	bookSign := signOf(f.DirectionConsistency)
	flowSign := signOf(f.FlowAcceleration)
	conflict := bookSign != 0 && flowSign != 0 && bookSign != flowSign && f.BandCoherence < 0.5
	if conflict {
		reasons = append(reasons, "BOOK_FLOW_CONFLICT")
	}
	highVol := t.HighVolBps > 0 && f.RealizedVolBps > t.HighVolBps
	if highVol {
		reasons = append(reasons, "HIGH_VOLATILITY")
	}

	// 3) predictability_score: uyum + dusuk flip + flow persistence, vol cezasi.
	//    Agirliklar baslangic; kalibrasyona acik.
	score := 0.4*clamp01(f.BandCoherence) +
		0.3*(1-clamp01(f.OBIFlipRate)) +
		0.3*clamp01(f.FlowPersistence)
	if highVol {
		score *= 0.5 // yuksek volde tahmin edilebilirlik duser
	}
	score = clamp01(score)

	// 4) Regime.
	var regime Regime
	switch {
	case unsafe:
		regime = RegimeUnsafe
	case highVol:
		regime = RegimeHighVol
	case lowCoherence || highFlip || conflict:
		regime = RegimeChaotic
	case t.TrendZMin > 0 && math.Abs(f.TrendZ) >= t.TrendZMin && f.BandCoherence >= t.CoherenceMin:
		if f.TrendZ > 0 {
			regime = RegimeTrendUp
		} else {
			regime = RegimeTrendDown
		}
	default:
		regime = RegimeChop
	}

	predictable := !unsafe && regime != RegimeChaotic && regime != RegimeHighVol &&
		regime != RegimeUnsafe && score >= t.PredictabilityMin
	if !predictable && score < t.PredictabilityMin && len(reasons) == 0 {
		reasons = append(reasons, "LOW_PREDICTABILITY")
	}

	return Result{PredictabilityScore: score, Regime: regime, Predictable: predictable, Reasons: reasons}
}

func signOf(x float64) int {
	switch {
	case x > 1e-12:
		return 1
	case x < -1e-12:
		return -1
	default:
		return 0
	}
}

func clamp01(x float64) float64 {
	if x < 0 {
		return 0
	}
	if x > 1 {
		return 1
	}
	return x
}
