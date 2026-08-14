package microfeat

import (
	"math"
	"testing"
)

func approx(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

func TestOBI(t *testing.T) {
	if got := OBI(70, 30); !approx(got, 0.4) {
		t.Fatalf("OBI(70,30)=%.4f, beklenen 0.4", got)
	}
	if got := OBI(30, 70); !approx(got, -0.4) {
		t.Fatalf("OBI(30,70)=%.4f, beklenen -0.4", got)
	}
	if got := OBI(0, 0); got != 0 {
		t.Fatalf("bos band 0 olmali, %v", got)
	}
}

func TestSignPersistence(t *testing.T) {
	// 17 negatif, 3 pozitif -> 0.85
	s := make([]float64, 0, 20)
	for i := 0; i < 17; i++ {
		s = append(s, -0.2)
	}
	for i := 0; i < 3; i++ {
		s = append(s, 0.2)
	}
	if got := SignPersistence(s); !approx(got, 0.85) {
		t.Fatalf("persistence=%.4f, beklenen 0.85", got)
	}
	// denge 11/9 -> 0.55
	s2 := make([]float64, 0, 20)
	for i := 0; i < 11; i++ {
		s2 = append(s2, -1)
	}
	for i := 0; i < 9; i++ {
		s2 = append(s2, 1)
	}
	if got := SignPersistence(s2); !approx(got, 0.55) {
		t.Fatalf("persistence=%.4f, beklenen 0.55", got)
	}
	if got := SignPersistence(nil); got != 0 {
		t.Fatalf("bos seri 0 olmali, %v", got)
	}
}

func TestFlipRate(t *testing.T) {
	// +,-,+,- -> 3 flip / 3 = 1.0 (tam kaotik)
	if got := FlipRate([]float64{1, -1, 1, -1}); !approx(got, 1.0) {
		t.Fatalf("flipRate=%.4f, beklenen 1.0", got)
	}
	// +,+,+,+ -> 0 flip
	if got := FlipRate([]float64{1, 1, 1, 1}); got != 0 {
		t.Fatalf("flipRate=%.4f, beklenen 0", got)
	}
	if got := FlipCount([]float64{1, 0, 1, -1}); got != 1 {
		t.Fatalf("flipCount=%d, beklenen 1 (sifir atlanir)", got)
	}
}

func TestBandCoherence(t *testing.T) {
	// hepsi ayni yon -> 1
	if got := BandCoherence([]float64{0.5, 0.3, 0.2}, nil); !approx(got, 1.0) {
		t.Fatalf("coherence(ayni yon)=%.4f, beklenen 1.0", got)
	}
	// tam celiskili +0.5/-0.5 -> 0
	if got := BandCoherence([]float64{0.5, -0.5}, nil); !approx(got, 0.0) {
		t.Fatalf("coherence(celiskili)=%.4f, beklenen 0.0", got)
	}
	// kismi: +0.5,-0.3 -> |0.2|/0.8 = 0.25
	if got := BandCoherence([]float64{0.5, -0.3}, nil); !approx(got, 0.25) {
		t.Fatalf("coherence=%.4f, beklenen 0.25", got)
	}
}

func TestStatsAndFlow(t *testing.T) {
	xs := []float64{1, 2, 3, 4}
	if !approx(Mean(xs), 2.5) {
		t.Fatalf("mean %.4f", Mean(xs))
	}
	if !approx(Median(xs), 2.5) {
		t.Fatalf("median %.4f", Median(xs))
	}
	if !approx(Median([]float64{3, 1, 2}), 2) {
		t.Fatalf("median tek %.4f", Median([]float64{3, 1, 2}))
	}
	if !approx(FlowAcceleration(0.8, 0.2), 0.6) {
		t.Fatalf("flow accel %.4f, beklenen 0.6", FlowAcceleration(0.8, 0.2))
	}
	// yon tutarliligi: tumu negatif, ort buyukluk 0.5, persistence 1 -> -0.5
	if got := DirectionConsistency([]float64{-0.5, -0.5, -0.5}); !approx(got, -0.5) {
		t.Fatalf("dirConsistency=%.4f, beklenen -0.5", got)
	}
}
