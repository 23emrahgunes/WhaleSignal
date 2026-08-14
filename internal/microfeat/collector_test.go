package microfeat

import (
	"testing"
	"time"
)

func TestCollectorRingBufferCap(t *testing.T) {
	c := NewCollector(3, nil)
	base := time.Now()
	for i := 0; i < 5; i++ {
		c.Add(Snapshot{T: base.Add(time.Duration(i) * time.Second), OBI: float64(i)})
	}
	if c.Len() != 3 {
		t.Fatalf("ring cap 3 olmali, %d", c.Len())
	}
}

func TestCollectorComputeCleanCoherentDown(t *testing.T) {
	c := NewCollector(240, []float64{1, 1, 1})
	base := time.Now()
	// 10 ornek, hepsi negatif OBI (kalici DOWN), uyumlu bantlar
	for i := 0; i < 10; i++ {
		c.Add(Snapshot{
			T:        base.Add(time.Duration(i*250) * time.Millisecond),
			BandOBIs: []float64{-0.5, -0.4, -0.3},
			OBI:      -0.4,
			Flow:     -0.3,
		})
	}
	now := base.Add(10 * 250 * time.Millisecond)
	f := c.Compute(now)
	if f.HistorySamples != 10 {
		t.Fatalf("history 10 olmali, %d", f.HistorySamples)
	}
	if f.BandCoherence < 0.99 {
		t.Fatalf("uyumlu bantlar -> coherence ~1, %.3f", f.BandCoherence)
	}
	if f.OBIPersistence5s < 0.99 {
		t.Fatalf("hepsi negatif -> persistence ~1, %.3f", f.OBIPersistence5s)
	}
	if f.OBIFlipRate != 0 {
		t.Fatalf("tek yon -> flip 0, %.3f", f.OBIFlipRate)
	}
	if f.DirectionConsistency >= 0 {
		t.Fatalf("negatif akis -> dirConsistency negatif, %.3f", f.DirectionConsistency)
	}
}

func TestCollectorChaoticFlips(t *testing.T) {
	c := NewCollector(240, nil)
	base := time.Now()
	// alternatif isaret -> yuksek flip, dusuk persistence
	for i := 0; i < 10; i++ {
		v := 0.4
		if i%2 == 1 {
			v = -0.4
		}
		c.Add(Snapshot{T: base.Add(time.Duration(i*250) * time.Millisecond),
			BandOBIs: []float64{0.5, -0.5}, OBI: v, Flow: v})
	}
	f := c.Compute(base.Add(3 * time.Second))
	if f.OBIFlipRate < 0.8 {
		t.Fatalf("alternatif -> yuksek flip, %.3f", f.OBIFlipRate)
	}
	if f.BandCoherence > 0.1 {
		t.Fatalf("celiskili bantlar -> dusuk coherence, %.3f", f.BandCoherence)
	}
}

func TestCollectorEmpty(t *testing.T) {
	c := NewCollector(10, nil)
	f := c.Compute(time.Now())
	if f.HistorySamples != 0 || f.BandCoherence != 0 {
		t.Fatalf("bos collector sifir olmali: %+v", f)
	}
}
