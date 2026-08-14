package main

import (
	"fmt"
	"math"
	"time"

	"pm-edge/internal/complete"
	"pm-edge/internal/dual40"
	"pm-edge/internal/engine"
	"pm-edge/internal/microfeat"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/predict"
)

// ModelBResult: yeni mikroyapi beyninin (SHADOW) o anki ciktisi — dashboard'a
// aynen JSON olarak sunulur. Karar VERMEZ; yalniz olcum/gozlem (paper).
type ModelBResult struct {
	Ts                  string   `json:"ts"`
	Regime              string   `json:"regime"`
	PredictabilityScore float64  `json:"predictabilityScore"`
	Predictable         bool     `json:"predictable"`
	Direction           string   `json:"direction"` // UP|DOWN|ABSTAIN
	PUp                 float64  `json:"pUp"`
	Confidence          float64  `json:"confidence"`
	BandCoherence       float64  `json:"bandCoherence"`
	OBIPersistence5s    float64  `json:"obiPersistence5s"`
	OBIFlipRate         float64  `json:"obiFlipRate"`
	FlowPersistence     float64  `json:"flowPersistence"`
	FlowAcceleration    float64  `json:"flowAcceleration"`
	AskDepletion        float64  `json:"askDepletion"`
	BidDepletion        float64  `json:"bidDepletion"`
	RealizedVolBps      float64  `json:"realizedVolBps"`
	QueueSymmetry       float64  `json:"queueSymmetry"`
	ExpectedFillUpSec   float64  `json:"expectedFillUpSec"`
	ExpectedFillDownSec float64  `json:"expectedFillDownSec"`
	HistorySamples      int      `json:"historySamples"`
	DataAgeMs           float64  `json:"dataAgeMs"`
	NoTradeReasons      []string `json:"noTradeReasons"`
}

// defaultRegimeThresholds: shadow baslangic esikleri (config/kalibrasyona acik).
func defaultRegimeThresholds() predict.Thresholds {
	return predict.Thresholds{
		PredictabilityMin: 0.55, CoherenceMin: 0.40, MaxFlipRate: 0.50,
		HighVolBps: 8.0, TrendZMin: 1.5, MaxDataAgeMs: 5000, MinHistory: 8,
	}
}

// defaultDirModel: kalibrasyona kadar tohum logistic (zayif; cogu zaman ABSTAIN).
// Agirliklar shadow veriden Update ile ogrenilecek.
func defaultDirModel() predict.LogisticModel {
	return predict.LogisticModel{Bias: 0, Weights: map[string]float64{
		"obi": 1.5, "flow": 1.0, "coherentLean": 1.0,
	}}
}

// snapshotFromDeep: canli derin mikroyapidan microfeat.Snapshot uretir.
func snapshotFromDeep(deep engine.EvaluationResult, now time.Time, realizedVolBps float64) microfeat.Snapshot {
	d := deep.DeepMicrostructure
	bands := make([]float64, 0, len(d.Bands))
	for _, b := range d.Bands {
		bands = append(bands, b.Imbalance)
	}
	obi := 0.0
	if len(d.Bands) > 0 {
		obi = d.Bands[0].Imbalance // en yakin band (±$10)
	}
	flow := 0.0
	if len(d.Trades) > 0 {
		flow = d.Trades[0].Imbalance // 5s agresif akis
	}
	return microfeat.Snapshot{T: now, BandOBIs: bands, OBI: obi, Flow: flow, RealizedVolBps: realizedVolBps}
}

// realizedVolBps: son Chainlink ornek getirilerinin std'si (bps).
func realizedVolBps(samples []dual40.Sample) float64 {
	if len(samples) < 3 {
		return 0
	}
	rets := make([]float64, 0, len(samples)-1)
	for i := 1; i < len(samples); i++ {
		p0, p1 := samples[i-1].Price, samples[i].Price
		if p0 > 0 {
			rets = append(rets, (p1-p0)/p0*10000)
		}
	}
	return microfeat.Stddev(rets)
}

// queueAt: kitaptaki 'price' seviyesindeki toplam bid boyutu (kuyruk).
func queueAt(book polymarket.BookSnapshot, price float64) float64 {
	var q float64
	for _, lvl := range book.Bids {
		if math.Abs(lvl.Price-price) <= 1e-9 {
			q += lvl.Size
		}
	}
	return q
}

// runModelB: bir tick'te feature topla, motorlari calistir, sonucu dondur (SHADOW).
func (r *dual40Runtime) runModelB(res *engine.EvaluationResult, m dual40.Metrics, upBook, downBook polymarket.BookSnapshot, now time.Time) ModelBResult {
	rv := realizedVolBps(r.samples)
	r.mfCollector.Add(snapshotFromDeep(*res, now, rv))
	fs := r.mfCollector.Compute(now)

	feats := predict.Features{
		BandCoherence:        fs.BandCoherence,
		OBIFlipRate:          fs.OBIFlipRate,
		OBISignPersistence:   fs.OBIPersistence5s,
		FlowPersistence:      fs.FlowPersistence,
		FlowAcceleration:     fs.FlowAcceleration,
		DirectionConsistency: fs.DirectionConsistency,
		RealizedVolBps:       fs.RealizedVolBps,
		TrendZ:               trendZ(m.DriftBps, rv),
		DataAgeMs:            fs.DataAgeMs,
		HistorySamples:       fs.HistorySamples,
	}
	reg := predict.Classify(feats, r.regimeThresh)

	// yon feature'lari (isaretli); coherentLean = coherence * baskin akis yonu
	coherentLean := fs.BandCoherence
	if fs.DirectionConsistency < 0 {
		coherentLean = -coherentLean
	}
	dirFeats := map[string]float64{"obi": fs.OBIMean, "flow": fs.DirectionConsistency, "coherentLean": coherentLean}
	dir := predict.Predict(dirFeats, r.dirModel, reg.Predictable, r.dirConfMin)

	// queue symmetry + tahmini dolum (Polymarket 0.40 kuyrugu; akis ~ 5s flow buyuklugu)
	qUp := queueAt(upBook, r.cfg.EntryPrice)
	qDown := queueAt(downBook, r.cfg.EntryPrice)
	arr := 1.0 // kaba varsayim: hisse/sn (gercek arrival F4.4 wiring'de olculuer)
	tUp := complete.EstimatedFillSeconds(complete.QueueInput{QueueAhead: qUp, OurSize: r.cfg.Shares, ArrivalRatePerSec: arr})
	tDown := complete.EstimatedFillSeconds(complete.QueueInput{QueueAhead: qDown, OurSize: r.cfg.Shares, ArrivalRatePerSec: arr})

	out := ModelBResult{
		Ts: now.UTC().Format(time.RFC3339), Regime: string(reg.Regime),
		PredictabilityScore: reg.PredictabilityScore, Predictable: reg.Predictable,
		Direction: dir.Direction, PUp: dir.PUp, Confidence: dir.Confidence,
		BandCoherence: fs.BandCoherence, OBIPersistence5s: fs.OBIPersistence5s,
		OBIFlipRate: fs.OBIFlipRate, FlowPersistence: fs.FlowPersistence,
		FlowAcceleration: fs.FlowAcceleration,
		AskDepletion:     res.DeepMicrostructure.AskDepletionScore,
		BidDepletion:     res.DeepMicrostructure.BidDepletionScore,
		RealizedVolBps:   rv, QueueSymmetry: complete.QueueSymmetry(qUp, qDown),
		ExpectedFillUpSec: capInf(tUp), ExpectedFillDownSec: capInf(tDown),
		HistorySamples: fs.HistorySamples, DataAgeMs: fs.DataAgeMs,
		NoTradeReasons: append(reg.Reasons, dir.Reasons...),
	}
	r.lastModelB.Store(&out)
	return out
}

// ModelB: dashboard/API icin son shadow sonucu (nil-safe).
func (r *dual40Runtime) ModelB() *ModelBResult {
	if r == nil {
		return nil
	}
	return r.lastModelB.Load()
}

// modelBGateBlocks: Model B kutu girisini engelliyorsa nedenini dondurur (bos =
// engel yok). Hazir degilse (yetersiz ornek) filtrelemez. F4.8 giris filtresi.
func (r *dual40Runtime) modelBGateBlocks() string {
	if !r.cfg.ModelBGate {
		return ""
	}
	mb := r.lastModelB.Load()
	if mb == nil || mb.HistorySamples < r.regimeThresh.MinHistory {
		return "" // hazir degil -> eski gate'ler karar versin
	}
	switch mb.Regime {
	case string(predict.RegimeChaotic), string(predict.RegimeUnsafe), string(predict.RegimeHighVol):
		return "MODELB_" + mb.Regime
	}
	if r.cfg.ModelBMinCoherence > 0 && mb.BandCoherence < r.cfg.ModelBMinCoherence {
		return fmt.Sprintf("MODELB_LOW_COHERENCE(%.2f<%.2f)", mb.BandCoherence, r.cfg.ModelBMinCoherence)
	}
	if r.cfg.ModelBMinQueueSym > 0 && mb.QueueSymmetry < r.cfg.ModelBMinQueueSym {
		return fmt.Sprintf("MODELB_QUEUE_ASYMMETRY(%.2f<%.2f)", mb.QueueSymmetry, r.cfg.ModelBMinQueueSym)
	}
	return ""
}

func trendZ(driftBps, volBps float64) float64 {
	if volBps <= 1e-9 {
		return 0
	}
	return driftBps / volBps
}

func capInf(x float64) float64 {
	if math.IsInf(x, 1) {
		return 9999
	}
	return x
}
