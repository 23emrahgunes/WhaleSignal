package complete

import (
	"math"
	"testing"
)

func approx(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

func TestEstimatedFillSeconds(t *testing.T) {
	// 10 onde + 5 bizim = 15 hisse; net 3/sn -> 5 sn
	got := EstimatedFillSeconds(QueueInput{QueueAhead: 10, OurSize: 5, ArrivalRatePerSec: 3})
	if !approx(got, 5) {
		t.Fatalf("fill sec=%.3f beklenen 5", got)
	}
	// akis yok -> Inf (dolmaz)
	if !math.IsInf(EstimatedFillSeconds(QueueInput{QueueAhead: 5, OurSize: 5}), 1) {
		t.Fatal("akis yokken Inf olmali")
	}
	// iptal de kuyrugu azaltir
	got2 := EstimatedFillSeconds(QueueInput{QueueAhead: 5, OurSize: 5, ArrivalRatePerSec: 1, CancelRatePerSec: 1})
	if !approx(got2, 5) {
		t.Fatalf("fill sec (iptal dahil)=%.3f beklenen 5", got2)
	}
}

func TestQueueSymmetry(t *testing.T) {
	if !approx(QueueSymmetry(10, 10), 1) {
		t.Fatal("esit kuyruk -> 1")
	}
	if QueueSymmetry(5, 120) > 0.05 {
		t.Fatal("cok asimetrik -> ~0")
	}
	if !approx(QueueSymmetry(0, 0), 1) {
		t.Fatal("bos-bos -> 1")
	}
}

func TestFillTimeSymmetry(t *testing.T) {
	if !approx(FillTimeSymmetry(12, 16), 12.0/16.0) {
		t.Fatalf("fill-time sym yanlis")
	}
	if !approx(FillTimeSymmetry(math.Inf(1), math.Inf(1)), 1) {
		t.Fatal("ikisi Inf -> 1")
	}
	if FillTimeSymmetry(7, math.Inf(1)) != 0 {
		t.Fatal("biri Inf -> 0")
	}
}

func TestSecondFillModelLearns(t *testing.T) {
	var m SecondFillModel
	// yuksek coherence -> ikinci bacak dolar; dusuk -> dolmaz
	for i := 0; i < 400; i++ {
		m.Train(map[string]float64{"coherence": 1.0}, true, 0.1)
		m.Train(map[string]float64{"coherence": -1.0}, false, 0.1)
	}
	if m.PSecond(map[string]float64{"coherence": 1.0}) <= 0.6 {
		t.Fatalf("ogrenme sonrasi yuksek coherence P(second) yuksek olmali")
	}
}

func TestCompletionStats(t *testing.T) {
	var s CompletionStats
	s.Observe(OutBoth)
	s.Observe(OutBoth)
	s.Observe(OutUpOnly)
	s.Observe(OutNone)
	// total 4, both 2 -> P(BOTH)=0.5
	if !approx(s.PBoth(), 0.5) {
		t.Fatalf("P(BOTH)=%.3f beklenen 0.5", s.PBoth())
	}
	// firstFills = 3 (2 both + 1 up_only); P(second|first)=2/3
	if !approx(s.PSecondGivenFirst(), 2.0/3.0) {
		t.Fatalf("P(second|first)=%.3f", s.PSecondGivenFirst())
	}
}
