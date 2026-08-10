package engine

import (
	"math"
	"sort"
	"sync"

	"pm-edge/internal/binance"
)

const maxBasisSamples = 120

type basisTracker struct {
	mu     sync.Mutex
	values []float64 // log(Chainlink / Binance), sampled ~1 Hz
}

func (b *basisTracker) Observe(chainlinkPrice, binancePrice float64) (basisBps, basisVolPerSqrtS float64) {
	if chainlinkPrice <= 0 || binancePrice <= 0 {
		return 0, 0
	}
	basis := math.Log(chainlinkPrice / binancePrice)
	b.mu.Lock()
	defer b.mu.Unlock()
	b.values = append(b.values, basis)
	if len(b.values) > maxBasisSamples {
		b.values = b.values[len(b.values)-maxBasisSamples:]
	}
	basisBps = basis * 10000.0
	if len(b.values) < 10 {
		return basisBps, 0
	}

	deltas := make([]float64, 0, len(b.values)-1)
	for i := 1; i < len(b.values); i++ {
		deltas = append(deltas, b.values[i]-b.values[i-1])
	}
	basisVolPerSqrtS = robustStd(deltas)
	return basisBps, basisVolPerSqrtS
}

// estimateMacroVolFloor returns a per-sqrt-second volatility floor from the
// slower 1-minute Binance OHLC history. It uses both close-to-close variance
// and Parkinson high/low range variance and intentionally scales them down
// slightly so they act as a floor rather than replacing the live 1s estimate.
func estimateMacroVolFloor(candles []binance.Candle) float64 {
	if len(candles) < 10 {
		return 0
	}
	if len(candles) > 60 {
		candles = candles[len(candles)-60:]
	}
	returns := make([]float64, 0, len(candles)-1)
	for i := 1; i < len(candles); i++ {
		if candles[i-1].Close > 0 && candles[i].Close > 0 {
			returns = append(returns, math.Log(candles[i].Close/candles[i-1].Close))
		}
	}
	closeSigmaMinute := robustStd(returns)
	closeSigmaPerSqrtS := closeSigmaMinute / math.Sqrt(60.0)

	parkinsonSum := 0.0
	parkinsonN := 0
	for _, c := range candles {
		if c.High > 0 && c.Low > 0 && c.High >= c.Low {
			r := math.Log(c.High / c.Low)
			parkinsonSum += r * r
			parkinsonN++
		}
	}
	parkinsonPerSqrtS := 0.0
	if parkinsonN > 0 {
		minuteVariance := parkinsonSum / (4.0 * math.Ln2 * float64(parkinsonN))
		parkinsonPerSqrtS = math.Sqrt(math.Max(0, minuteVariance) / 60.0)
	}

	// A floor should be conservative but not dominate genuinely calm periods.
	return math.Max(0.75*closeSigmaPerSqrtS, 0.60*parkinsonPerSqrtS)
}

func robustStd(values []float64) float64 {
	if len(values) < 2 {
		return 0
	}
	median := medianFloat(values)
	dev := make([]float64, len(values))
	for i, v := range values {
		dev[i] = math.Abs(v - median)
	}
	mad := medianFloat(dev)
	if mad > 0 {
		return 1.4826 * mad
	}
	mean := 0.0
	for _, v := range values {
		mean += v
	}
	mean /= float64(len(values))
	ss := 0.0
	for _, v := range values {
		d := v - mean
		ss += d * d
	}
	return math.Sqrt(ss / float64(len(values)-1))
}

func medianFloat(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	cp := append([]float64(nil), values...)
	sort.Float64s(cp)
	mid := len(cp) / 2
	if len(cp)%2 == 1 {
		return cp[mid]
	}
	return 0.5 * (cp[mid-1] + cp[mid])
}
