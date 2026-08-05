package probability

import (
	"math"
)

// NormalCDF computes the cumulative distribution function of the standard normal distribution.
func NormalCDF(x float64) float64 {
	return 0.5 * (1.0 + math.Erf(x/math.Sqrt(2.0)))
}

// CalculateProbability computes mathematically the win probability for an up/down outcome
// S = current price
// K = priceToBeat
// T = remaining time in years (e.g. secondsRemaining / 31536000)
// sigmaAnnual = annualized realized volatility (clamped if needed)
// muAnnual = annualized drift (clamped to [-5.0, 5.0])
func CalculateProbability(S, K, T, sigmaAnnual, muAnnual float64) (float64, float64) {
	if T <= 0 {
		if S > K {
			return 1.0, 0.0
		} else if S < K {
			return 0.0, 1.0
		}
		return 0.5, 0.5
	}

	// Clamp volatility to avoid division by zero or negative vols
	if sigmaAnnual < 0.01 {
		sigmaAnnual = 0.01
	}

	// Clamp drift to ±500%
	if muAnnual > 5.0 {
		muAnnual = 5.0
	} else if muAnnual < -5.0 {
		muAnnual = -5.0
	}

	x := math.Log(S / K)
	numerator := x + (muAnnual * T)
	denominator := sigmaAnnual * math.Sqrt(T)

	z := numerator / denominator
	pUp := NormalCDF(z)
	pDown := 1.0 - pUp

	return pUp, pDown
}
