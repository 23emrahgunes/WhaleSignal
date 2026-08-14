// Package complete: Dual40 COMPLETION + QUEUE motorlari (SHADOW-first).
// Ana hedef yon degil, iki 0.40 bacaginin TAMAMLANMA ihtimali (P(BOTH),
// P(second|first)) + queue simetrisi + tahmini dolum suresi. Saf hesaplama.
package complete

import (
	"math"

	"pm-edge/internal/predict"
)

const eps = 1e-9

// QueueInput: bir tarafin (UP veya DOWN) 0.40 kuyruk dinamigi.
type QueueInput struct {
	QueueAhead        float64 // onumuzdeki hisse (0.40'ta bizden once)
	OurSize           float64 // bizim emrimiz (5)
	ArrivalRatePerSec float64 // 0.40'a gelen SATIS akisi (kuyrugu tuketir), hisse/sn
	CancelRatePerSec  float64 // 0.40 seviyesinden iptal (kuyrugu azaltir), hisse/sn
}

// EstimatedFillSeconds: (queueAhead + ourSize) tuketilmesi icin tahmini sure.
// Net tuketim = arrival + cancel (ikisi de kuyrugu azaltir; iptal onumuzu acar).
// Net akis ~0 ise cok buyuk (pratikte "dolmaz") dondurur.
func EstimatedFillSeconds(in QueueInput) float64 {
	net := in.ArrivalRatePerSec + in.CancelRatePerSec
	if net <= eps {
		return math.Inf(1)
	}
	need := math.Max(0, in.QueueAhead) + math.Max(0, in.OurSize)
	return need / net
}

// QueueSymmetry: min(Q_up,Q_dn)/(max+eps), [0,1]. 1 = simetrik (iyi), 0 = asimetrik.
func QueueSymmetry(qUp, qDown float64) float64 {
	mx := math.Max(qUp, qDown)
	if mx <= eps {
		return 1 // ikisi de bos -> simetrik kabul
	}
	return math.Min(qUp, qDown) / (mx + eps)
}

// FillTimeSymmetry: iki tarafin tahmini dolum sureleri ne kadar simetrik. Inf'ler
// icin: ikisi de Inf -> 1; biri Inf -> 0.
func FillTimeSymmetry(tUp, tDown float64) float64 {
	uInf, dInf := math.IsInf(tUp, 1), math.IsInf(tDown, 1)
	if uInf && dInf {
		return 1
	}
	if uInf || dInf {
		return 0
	}
	mx := math.Max(tUp, tDown)
	if mx <= eps {
		return 1
	}
	return math.Min(tUp, tDown) / (mx + eps)
}

// SecondFillModel: P(second_fill | first_fill) icin aciklanabilir logistic
// (predict.LogisticModel yeniden kullanilir). Ilk-fill ani feature'larindan
// (drift, flow, coherence, queue symmetry, regime chop skoru) ogrenilir.
type SecondFillModel struct {
	Model predict.LogisticModel
}

// PSecond: ilk-fill feature'larindan P(second|first).
func (m SecondFillModel) PSecond(features map[string]float64) float64 {
	return m.Model.PUp(features) // sigmoid, [0,1]
}

// Train: gozlemle ogren (label 1 = ikinci bacak doldu/BOTH, 0 = dolmadi).
func (m *SecondFillModel) Train(features map[string]float64, secondFilled bool, lr float64) {
	label := 0.0
	if secondFilled {
		label = 1.0
	}
	m.Model.Update(features, label, lr)
}

// Outcome siniflari.
const (
	OutBoth     = "BOTH"
	OutUpOnly   = "UP_ONLY"
	OutDownOnly = "DOWN_ONLY"
	OutNone     = "NONE"
)

// CompletionStats: ampirik P(BOTH)/P(UP_ONLY)/P(DOWN_ONLY)/P(NONE) + P(second|first)
// biriktirir (rejim/timeframe bazinda ayri tutulabilir).
type CompletionStats struct {
	Both, UpOnly, DownOnly, None int
	FirstFills                   int // en az bir bacak dolan
}

// Observe: bir trial'in terminal sonucunu isle.
func (s *CompletionStats) Observe(outcome string) {
	switch outcome {
	case OutBoth:
		s.Both++
		s.FirstFills++
	case OutUpOnly:
		s.UpOnly++
		s.FirstFills++
	case OutDownOnly:
		s.DownOnly++
		s.FirstFills++
	case OutNone:
		s.None++
	}
}

func (s *CompletionStats) total() int { return s.Both + s.UpOnly + s.DownOnly + s.None }

// PBoth: ampirik P(BOTH) tum trial'lar uzerinden.
func (s *CompletionStats) PBoth() float64 {
	t := s.total()
	if t == 0 {
		return 0
	}
	return float64(s.Both) / float64(t)
}

// PSecondGivenFirst: en az bir bacak dolanlar arasinda ikinci de dolma orani.
func (s *CompletionStats) PSecondGivenFirst() float64 {
	if s.FirstFills == 0 {
		return 0
	}
	return float64(s.Both) / float64(s.FirstFills)
}
