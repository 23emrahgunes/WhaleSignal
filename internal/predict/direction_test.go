package predict

import (
	"math"
	"testing"
)

func TestPredictAbstainWhenNotPredictable(t *testing.T) {
	r := Predict(map[string]float64{"obi": 0.9}, LogisticModel{}, false, 0.55)
	if r.Direction != DirAbstain || !hasReason(r.Reasons, "NOT_PREDICTABLE") {
		t.Fatalf("predictable=false -> ABSTAIN olmali: %+v", r)
	}
}

func TestPredictLowConfidenceAbstain(t *testing.T) {
	// bos model -> P(UP)=0.5 -> guven 0.5 < 0.55 -> ABSTAIN
	r := Predict(map[string]float64{"obi": 0.1}, LogisticModel{}, true, 0.55)
	if r.Direction != DirAbstain || !hasReason(r.Reasons, "LOW_CONFIDENCE") {
		t.Fatalf("dusuk guven -> ABSTAIN olmali: %+v", r)
	}
}

func TestPredictUpDown(t *testing.T) {
	m := LogisticModel{Bias: 0, Weights: map[string]float64{"obi": 5.0}}
	up := Predict(map[string]float64{"obi": 1.0}, m, true, 0.55)
	if up.Direction != DirUp || up.PUp <= 0.5 {
		t.Fatalf("guclu +obi -> UP olmali: %+v", up)
	}
	dn := Predict(map[string]float64{"obi": -1.0}, m, true, 0.55)
	if dn.Direction != DirDown || dn.PDown <= 0.5 {
		t.Fatalf("guclu -obi -> DOWN olmali: %+v", dn)
	}
}

func TestOnlineUpdateLearnsDirection(t *testing.T) {
	m := LogisticModel{}
	// obi>0 -> UP(1), obi<0 -> DOWN(0) ornekleriyle egit
	for i := 0; i < 500; i++ {
		m.Update(map[string]float64{"obi": 1.0}, 1.0, 0.1)
		m.Update(map[string]float64{"obi": -1.0}, 0.0, 0.1)
	}
	if m.PUp(map[string]float64{"obi": 1.0}) <= 0.6 {
		t.Fatalf("ogrenme sonrasi +obi P(UP) yuksek olmali: %.3f", m.PUp(map[string]float64{"obi": 1.0}))
	}
	if m.PUp(map[string]float64{"obi": -1.0}) >= 0.4 {
		t.Fatalf("ogrenme sonrasi -obi P(UP) dusuk olmali: %.3f", m.PUp(map[string]float64{"obi": -1.0}))
	}
}

func TestCalibTracker(t *testing.T) {
	var c CalibTracker
	c.Observe(DirAbstain, 0, true)  // abstain
	c.Observe(DirUp, 0.75, true)    // dogru
	c.Observe(DirUp, 0.75, false)   // yanlis
	c.Observe(DirDown, 0.90, false) // dogru (down, down oldu)
	if c.Total != 4 || c.Abstains != 1 {
		t.Fatalf("total/abstain yanlis: %+v", c)
	}
	if !approxf(c.Coverage(), 0.75) {
		t.Fatalf("coverage=%.3f beklenen 0.75", c.Coverage())
	}
	if c.Correct != 2 || c.Wrong != 1 {
		t.Fatalf("correct/wrong yanlis: %d/%d", c.Correct, c.Wrong)
	}
	if !approxf(c.WinRate(), 2.0/3.0) {
		t.Fatalf("winrate=%.3f", c.WinRate())
	}
	if c.Brier() <= 0 {
		t.Fatal("brier hesaplanmadi")
	}
}

func approxf(a, b float64) bool { return math.Abs(a-b) < 1e-9 }
