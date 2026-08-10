package probability

import (
	"math"
	"testing"
)

func TestCalculateProbability(t *testing.T) {
	S := 100000.0
	K := 101000.0
	T := 5.0 / (365.0 * 24.0 * 12.0) // 5 minutes in years
	sigma := 0.15                    // 15% annualised vol
	mu := 0.05                       // 5% annualised drift

	pUp, pDown := CalculateProbability(S, K, T, sigma, mu)

	if pUp < 0.0 || pUp > 1.0 {
		t.Errorf("Expected pUp to be between 0 and 1, got %f", pUp)
	}
	if math.Abs((pUp+pDown)-1.0) > 1e-9 {
		t.Errorf("Expected pUp + pDown to be 1.0, got %f", pUp+pDown)
	}

	// S < K, so pUp should be less than 0.5 usually unless drift is extremely high
	if pUp >= 0.5 {
		t.Errorf("Expected pUp to be < 0.5, got %f", pUp)
	}
}
