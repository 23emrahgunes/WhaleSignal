package predict

import "math"

// Yon secenekleri.
const (
	DirUp      = "UP"
	DirDown    = "DOWN"
	DirAbstain = "ABSTAIN"
)

// LogisticModel: aciklanabilir yon modeli. z = Bias + Σ w_i·x_i, P(UP)=sigmoid(z).
// Agirliklar HARD-CODE degil; config'ten yuklenir veya shadow veriden Update ile
// ogrenilir (online SGD). Deneyim yoksa bos map -> P(UP)=sigmoid(Bias)=0.5.
type LogisticModel struct {
	Bias    float64            `json:"bias"`
	Weights map[string]float64 `json:"weights"`
}

func sigmoid(z float64) float64 { return 1.0 / (1.0 + math.Exp(-z)) }

// PUp: feature -> P(UP). Bilinmeyen feature'lar (agirligi yoksa) atlanir.
func (m LogisticModel) PUp(features map[string]float64) float64 {
	z := m.Bias
	for k, w := range m.Weights {
		z += w * features[k]
	}
	return sigmoid(z)
}

// Update: tek ornekle online lojistik SGD adimi (label 1=UP, 0=DOWN). Kalibrasyon/
// ogrenme icin. lr ogrenme orani. Weights nil ise olusturur.
func (m *LogisticModel) Update(features map[string]float64, label float64, lr float64) {
	if m.Weights == nil {
		m.Weights = map[string]float64{}
	}
	pred := m.PUp(features)
	err := pred - label // dL/dz (logloss)
	m.Bias -= lr * err
	for k, x := range features {
		m.Weights[k] -= lr * err * x
	}
}

// DirectionResult: yon motoru cikti.
type DirectionResult struct {
	PUp        float64  `json:"pUp"`
	PDown      float64  `json:"pDown"`
	Confidence float64  `json:"confidence"` // 0..1, max(PUp,PDown)
	Direction  string   `json:"direction"`  // UP|DOWN|ABSTAIN
	Reasons    []string `json:"reasons"`
}

// Predict: predictable degilse ABSTAIN. Aksi halde P(UP) hesapla; guven esigin
// altindaysa ABSTAIN (zorla UP/DOWN uretme). confidenceMin config'ten.
func Predict(features map[string]float64, model LogisticModel, predictable bool, confidenceMin float64) DirectionResult {
	if !predictable {
		return DirectionResult{PUp: 0.5, PDown: 0.5, Confidence: 0, Direction: DirAbstain,
			Reasons: []string{"NOT_PREDICTABLE"}}
	}
	pUp := model.PUp(features)
	pDown := 1 - pUp
	conf := math.Max(pUp, pDown)
	res := DirectionResult{PUp: pUp, PDown: pDown, Confidence: conf}
	if conf < confidenceMin {
		res.Direction = DirAbstain
		res.Reasons = []string{"LOW_CONFIDENCE"}
		return res
	}
	if pUp >= pDown {
		res.Direction = DirUp
	} else {
		res.Direction = DirDown
	}
	return res
}

// CalibTracker: coverage + Brier + guven-kovasi winrate biriktirir (§28/§29).
// Yalniz PREDICTION yapilan (ABSTAIN olmayan) ornekler brier/winrate'e girer.
type CalibTracker struct {
	Total    int // gorulen tum market
	Abstains int // tahminden kacinilan
	Correct  int // dogru yon
	Wrong    int // yanlis yon
	brierSum float64
	binHit   [6]int // 50-55,55-60,60-65,65-70,70-80,80+
	binWin   [6]int
}

// Observe: bir sonucu isle. dir = uretilen yon; conf = guven; upWon = gercekte UP mi.
func (c *CalibTracker) Observe(dir string, conf float64, upWon bool) {
	c.Total++
	if dir == DirAbstain {
		c.Abstains++
		return
	}
	predUp := dir == DirUp
	correct := predUp == upWon
	if correct {
		c.Correct++
	} else {
		c.Wrong++
	}
	// Brier: (P(tahmin_yonu) - outcome)^2, outcome=1 dogru yon gerceklestiyse.
	p := conf
	o := 0.0
	if correct {
		o = 1.0
	}
	c.brierSum += (p - o) * (p - o)
	b := confBin(conf)
	c.binHit[b]++
	if correct {
		c.binWin[b]++
	}
}

// Coverage: tahmin yapilan oran (1 - abstain/total).
func (c *CalibTracker) Coverage() float64 {
	if c.Total == 0 {
		return 0
	}
	return float64(c.Total-c.Abstains) / float64(c.Total)
}

// WinRate: dogru/(dogru+yanlis).
func (c *CalibTracker) WinRate() float64 {
	d := c.Correct + c.Wrong
	if d == 0 {
		return 0
	}
	return float64(c.Correct) / float64(d)
}

// Brier: ortalama Brier skoru (dusuk iyi).
func (c *CalibTracker) Brier() float64 {
	d := c.Correct + c.Wrong
	if d == 0 {
		return 0
	}
	return c.brierSum / float64(d)
}

// BinWinRates: guven-kovasi bazinda winrate (kalibrasyon icin).
func (c *CalibTracker) BinWinRates() [6]float64 {
	var out [6]float64
	for i := range out {
		if c.binHit[i] > 0 {
			out[i] = float64(c.binWin[i]) / float64(c.binHit[i])
		}
	}
	return out
}

func confBin(conf float64) int {
	switch {
	case conf < 0.55:
		return 0
	case conf < 0.60:
		return 1
	case conf < 0.65:
		return 2
	case conf < 0.70:
		return 3
	case conf < 0.80:
		return 4
	default:
		return 5
	}
}
