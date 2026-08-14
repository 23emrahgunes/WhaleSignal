package microfeat

import "time"

// Snapshot: bir anlik mikroyapi orneklemesi (~250ms'de bir eklenir).
type Snapshot struct {
	T              time.Time
	BandOBIs       []float64 // her derinlik bandi icin OBI (coherence icin)
	OBI            float64   // temsili (yakin band) OBI, persistence/flip icin
	Flow           float64   // anlik agresif akis dengesizligi [-1,1]
	RealizedVolBps float64
}

// FeatureSet: Collector'in urettigi zaman-tabanli ozellikler. predict.Features'a
// (wiring katmaninda) eslenir. Paketler-arasi bagimlilik yaratmaz.
type FeatureSet struct {
	BandCoherence        float64
	OBIPersistence1s     float64
	OBIPersistence2s     float64
	OBIPersistence5s     float64
	OBIPersistence10s    float64
	OBIFlipRate          float64
	OBIMean              float64
	OBIStdDev            float64
	FlowPersistence      float64
	FlowAcceleration     float64
	DirectionConsistency float64
	RealizedVolBps       float64
	HistorySamples       int
	DataAgeMs            float64
}

// Collector: sabit kapasiteli ring-buffer + band agirliklari. Add ile beslenir,
// Compute ile o anki FeatureSet uretilir. Yan-etkisiz hesaplama (test-dostu).
type Collector struct {
	buf     []Snapshot
	max     int
	weights []float64
}

// NewCollector: maxSamples ring-buffer boyutu (orn. 60s/250ms ~ 240), weights
// coherence icin band agirliklari (nil -> esit).
func NewCollector(maxSamples int, weights []float64) *Collector {
	if maxSamples < 1 {
		maxSamples = 1
	}
	return &Collector{buf: make([]Snapshot, 0, maxSamples), max: maxSamples, weights: weights}
}

// Add: yeni ornek ekle (en eskiyi dusurur).
func (c *Collector) Add(s Snapshot) {
	c.buf = append(c.buf, s)
	if len(c.buf) > c.max {
		c.buf = append([]Snapshot(nil), c.buf[len(c.buf)-c.max:]...)
	}
}

// Len: buffer'daki ornek sayisi.
func (c *Collector) Len() int { return len(c.buf) }

// obiWithin: son 'within' suresi icindeki OBI serisi.
func (c *Collector) obiWithin(now time.Time, within time.Duration) []float64 {
	out := make([]float64, 0, len(c.buf))
	for _, s := range c.buf {
		if now.Sub(s.T) <= within {
			out = append(out, s.OBI)
		}
	}
	return out
}

// Compute: o anki FeatureSet. Bos buffer -> sifir + HistorySamples 0.
func (c *Collector) Compute(now time.Time) FeatureSet {
	var f FeatureSet
	n := len(c.buf)
	f.HistorySamples = n
	if n == 0 {
		return f
	}
	last := c.buf[n-1]
	f.DataAgeMs = float64(now.Sub(last.T).Milliseconds())
	f.RealizedVolBps = last.RealizedVolBps
	f.BandCoherence = BandCoherence(last.BandOBIs, c.weights)

	// OBI tam seri (persistence/flip/mean/std)
	allOBI := make([]float64, n)
	for i, s := range c.buf {
		allOBI[i] = s.OBI
	}
	f.OBIFlipRate = FlipRate(allOBI)
	f.OBIMean = Mean(allOBI)
	f.OBIStdDev = Stddev(allOBI)
	f.OBIPersistence1s = SignPersistence(c.obiWithin(now, 1*time.Second))
	f.OBIPersistence2s = SignPersistence(c.obiWithin(now, 2*time.Second))
	f.OBIPersistence5s = SignPersistence(c.obiWithin(now, 5*time.Second))
	f.OBIPersistence10s = SignPersistence(c.obiWithin(now, 10*time.Second))

	// Flow serisi
	flow := make([]float64, n)
	for i, s := range c.buf {
		flow[i] = s.Flow
	}
	f.FlowPersistence = SignPersistence(flow)
	f.DirectionConsistency = DirectionConsistency(flow)
	// hizlanma: son ~2s ort. eksi tum-pencere ort.
	shortFlow := make([]float64, 0, n)
	for _, s := range c.buf {
		if now.Sub(s.T) <= 2*time.Second {
			shortFlow = append(shortFlow, s.Flow)
		}
	}
	f.FlowAcceleration = FlowAcceleration(Mean(shortFlow), Mean(flow))
	return f
}
