package probability

import (
	"math"
	"sort"
)

const (
	secondsPerYear          = 365.0 * 24.0 * 60.0 * 60.0
	defaultAnnualVolFloor   = 0.20 // conservative research prior; dynamic floor may be higher
	defaultModelUncertainty = 1.15 // variance inflation for uncalibrated short-horizon model risk
)

// ForecastContext carries uncertainty estimates that are not visible in the
// one-second Binance return sample itself. This prevents a quiet/duplicated
// 60-second sample from producing absurdly narrow terminal bands.
type ForecastContext struct {
	VolatilityFloorPerSqrtS float64
	BasisVolatilityPerSqrtS float64
	ModelUncertainty        float64
}

// TerminalForecast is a short-horizon, research-only terminal price distribution.
// LogReturns are expected to be approximately one-second log returns.
type TerminalForecast struct {
	Ready                    bool
	Samples                  int
	HorizonSeconds           float64
	DriftPerSecond           float64
	VolatilityPerSqrtS       float64
	MicroVolatilityAnnual    float64
	VolatilityFloorAnnual    float64
	BasisVolatilityAnnual    float64
	DriftAnnual              float64
	VolatilityAnnual         float64
	ForecastMedian           float64
	ForecastMean             float64
	Lower68                  float64
	Upper68                  float64
	Lower95                  float64
	Upper95                  float64
	RequiredMoveBps          float64
	ExpectedMoveBps          float64
	SigmaAtExpiryBps         float64
	TargetZ                  float64
	PAbove                   float64
	PBelow                   float64
	Confidence               float64
}

func NormalCDF(x float64) float64 {
	return 0.5 * (1.0 + math.Erf(x/math.Sqrt(2.0)))
}

// EstimateTerminalForecast keeps the historical API but now uses a conservative
// volatility prior. Production evaluation should prefer EstimateTerminalForecastWithContext.
func EstimateTerminalForecast(currentPrice, targetPrice, horizonSeconds float64, logReturns []float64) TerminalForecast {
	return EstimateTerminalForecastWithContext(currentPrice, targetPrice, horizonSeconds, logReturns, ForecastContext{})
}

// EstimateTerminalForecastWithContext combines four uncertainty sources:
//  1. robust EWMA one-second realized variance,
//  2. a slower volatility floor supplied by 1m OHLC data,
//  3. Chainlink/Binance basis-change variance,
//  4. an explicit model-risk inflation factor while the model is uncalibrated.
//
// Drift is shrunk and horizon-capped. This is deliberate: with only ~60 seconds
// of observations, estimating variance is much easier than estimating drift.
func EstimateTerminalForecastWithContext(currentPrice, targetPrice, horizonSeconds float64, logReturns []float64, ctx ForecastContext) TerminalForecast {
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

	effectiveN := sumW * sumW / math.Max(sumW2, 1e-12)
	shrink := effectiveN / (effectiveN + 30.0)
	driftPerSecond := mean * shrink
	microVol := math.Sqrt(variance)
	if robustSigma > 0 {
		microVol = math.Max(microVol, 0.75*robustSigma)
	}

	defaultFloor := defaultAnnualVolFloor / math.Sqrt(secondsPerYear)
	volFloor := math.Max(defaultFloor, ctx.VolatilityFloorPerSqrtS)
	basisVol := math.Max(0, ctx.BasisVolatilityPerSqrtS)
	baseVol := math.Max(microVol, volFloor)
	combinedVol := math.Sqrt(baseVol*baseVol + basisVol*basisVol)
	uncertainty := ctx.ModelUncertainty
	if uncertainty <= 0 {
		uncertainty = defaultModelUncertainty
	}
	if uncertainty < 1.0 {
		uncertainty = 1.0
	}
	volPerSqrtSecond := combinedVol * uncertainty

	sigmaH := volPerSqrtSecond * math.Sqrt(horizonSeconds)
	if sigmaH <= 0 {
		return f
	}
	muH := driftPerSecond * horizonSeconds
	// Do not allow an extremely noisy 60-second drift estimate to shift the
	// terminal median by more than 0.75 sigma. Direction should come from price
	// distance + persistent evidence, not an unstable sample mean.
	maxDriftH := 0.75 * sigmaH
	if muH > maxDriftH {
		muH = maxDriftH
	} else if muH < -maxDriftH {
		muH = -maxDriftH
	}
	driftPerSecond = muH / horizonSeconds

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
	f.SigmaAtExpiryBps = sigmaH * 10000.0
	f.DriftPerSecond = driftPerSecond
	f.VolatilityPerSqrtS = volPerSqrtSecond
	f.MicroVolatilityAnnual = microVol * math.Sqrt(secondsPerYear)
	f.VolatilityFloorAnnual = volFloor * math.Sqrt(secondsPerYear)
	f.BasisVolatilityAnnual = basisVol * math.Sqrt(secondsPerYear)
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

// CalculateProbability is retained for compatibility with historical tools.
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
