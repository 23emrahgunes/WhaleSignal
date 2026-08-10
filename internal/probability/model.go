package probability

import (
	"math"
	"sort"
)

const secondsPerYear = 365.0 * 24.0 * 60.0 * 60.0

// TerminalForecast is a short-horizon, research-only terminal price distribution.
// LogReturns are expected to be approximately one-second log returns.
type TerminalForecast struct {
	Ready              bool
	Samples            int
	HorizonSeconds     float64
	DriftPerSecond     float64
	VolatilityPerSqrtS float64
	DriftAnnual        float64
	VolatilityAnnual   float64
	ForecastMedian     float64
	ForecastMean       float64
	Lower68            float64
	Upper68            float64
	Lower95            float64
	Upper95            float64
	RequiredMoveBps    float64
	ExpectedMoveBps    float64
	TargetZ            float64
	PAbove             float64
	PBelow             float64
	Confidence         float64
}

// NormalCDF computes the cumulative distribution function of the standard normal distribution.
func NormalCDF(x float64) float64 {
	return 0.5 * (1.0 + math.Erf(x/math.Sqrt(2.0)))
}

// EstimateTerminalForecast estimates the distribution of the BTC reference price
// at the market end boundary. It intentionally keeps the model compact and
// calibratable: robustly winsorized one-second log returns, exponentially
// weighted moments, and drift shrinkage toward zero to reduce short-sample
// trend overfitting.
func EstimateTerminalForecast(currentPrice, targetPrice, horizonSeconds float64, logReturns []float64) TerminalForecast {
	f := TerminalForecast{HorizonSeconds: horizonSeconds}
	if currentPrice <= 0 || targetPrice <= 0 || horizonSeconds <= 0 || len(logReturns) < 10 {
		return f
	}

	returns := append([]float64(nil), logReturns...)
	if len(returns) > 60 {
		returns = returns[len(returns)-60:]
	}
	f.Samples = len(returns)

	median := sampleMedian(returns)
	absDev := make([]float64, len(returns))
	for i, r := range returns {
		absDev[i] = math.Abs(r - median)
	}
	mad := sampleMedian(absDev)
	robustSigma := 1.4826 * mad
	clip := 5.0 * robustSigma
	if clip > 0 {
		lo := median - clip
		hi := median + clip
		for i, r := range returns {
			if r < lo {
				returns[i] = lo
			} else if r > hi {
				returns[i] = hi
			}
		}
	}

	// 15-second half-life: newest observations matter more without letting a
	// single tick dominate the forecast.
	lambda := math.Exp(-math.Ln2 / 15.0)
	sumW := 0.0
	sumW2 := 0.0
	mean := 0.0
	for i, r := range returns {
		age := float64(len(returns) - 1 - i)
		w := math.Pow(lambda, age)
		sumW += w
		sumW2 += w * w
		mean += w * r
	}
	if sumW <= 0 {
		return f
	}
	mean /= sumW

	variance := 0.0
	for i, r := range returns {
		age := float64(len(returns) - 1 - i)
		w := math.Pow(lambda, age)
		d := r - mean
		variance += w * d * d
	}
	variance /= sumW
	if variance < 0 {
		variance = 0
	}

	// Effective sample size drives empirical-Bayes style shrinkage of drift.
	// Volatility is allowed to react quickly; drift is deliberately conservative.
	effectiveN := sumW * sumW / math.Max(sumW2, 1e-12)
	shrink := effectiveN / (effectiveN + 30.0)
	driftPerSecond := mean * shrink
	volPerSqrtSecond := math.Sqrt(variance)
	if robustSigma > 0 {
		volPerSqrtSecond = math.Max(volPerSqrtSecond, 0.75*robustSigma)
	}
	volPerSqrtSecond = math.Max(volPerSqrtSecond, 1e-7)

	sigmaH := volPerSqrtSecond * math.Sqrt(horizonSeconds)
	muH := driftPerSecond * horizonSeconds
	terminalLogMedian := math.Log(currentPrice) + muH
	f.ForecastMedian = math.Exp(terminalLogMedian)
	f.ForecastMean = math.Exp(terminalLogMedian + 0.5*sigmaH*sigmaH)
	f.Lower68 = math.Exp(terminalLogMedian - sigmaH)
	f.Upper68 = math.Exp(terminalLogMedian + sigmaH)
	f.Lower95 = math.Exp(terminalLogMedian - 1.959963984540054*sigmaH)
	f.Upper95 = math.Exp(terminalLogMedian + 1.959963984540054*sigmaH)

	logDistanceToTarget := math.Log(currentPrice / targetPrice)
	f.TargetZ = (logDistanceToTarget + muH) / sigmaH
	f.PAbove = NormalCDF(f.TargetZ)
	f.PBelow = 1.0 - f.PAbove
	f.Confidence = math.Abs(f.PAbove-0.5) * 200.0
	f.RequiredMoveBps = math.Log(targetPrice/currentPrice) * 10000.0
	f.ExpectedMoveBps = muH * 10000.0
	f.DriftPerSecond = driftPerSecond
	f.VolatilityPerSqrtS = volPerSqrtSecond
	f.DriftAnnual = driftPerSecond * secondsPerYear
	f.VolatilityAnnual = volPerSqrtSecond * math.Sqrt(secondsPerYear)
	f.Ready = true
	return f
}

func sampleMedian(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	copyValues := append([]float64(nil), values...)
	sort.Float64s(copyValues)
	mid := len(copyValues) / 2
	if len(copyValues)%2 == 1 {
		return copyValues[mid]
	}
	return 0.5 * (copyValues[mid-1] + copyValues[mid])
}

// CalculateProbability is retained for compatibility with historical tests and
// tools that use annualized inputs directly.
func CalculateProbability(S, K, T, sigmaAnnual, muAnnual float64) (float64, float64) {
	if T <= 0 {
		if S > K {
			return 1.0, 0.0
		} else if S < K {
			return 0.0, 1.0
		}
		return 0.5, 0.5
	}
	if sigmaAnnual < 0.01 {
		sigmaAnnual = 0.01
	}
	if muAnnual > 5.0 {
		muAnnual = 5.0
	} else if muAnnual < -5.0 {
		muAnnual = -5.0
	}
	x := math.Log(S / K)
	z := (x + muAnnual*T) / (sigmaAnnual * math.Sqrt(T))
	pUp := NormalCDF(z)
	return pUp, 1.0 - pUp
}
