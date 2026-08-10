package probability

import (
	"math"
	"testing"
)

func TestCalculateProbability(t *testing.T) {
	S := 100000.0
	K := 101000.0
	T := 5.0 / (365.0 * 24.0 * 12.0)
	sigma := 0.15
	mu := 0.05
	pUp, pDown := CalculateProbability(S, K, T, sigma, mu)
	if pUp < 0.0 || pUp > 1.0 {
		t.Errorf("Expected pUp to be between 0 and 1, got %f", pUp)
	}
	if math.Abs((pUp+pDown)-1.0) > 1e-9 {
		t.Errorf("Expected pUp + pDown to be 1.0, got %f", pUp+pDown)
	}
	if pUp >= 0.5 {
		t.Errorf("Expected pUp to be < 0.5, got %f", pUp)
	}
}

func TestTerminalForecastRequiresWarmup(t *testing.T) {
	f := EstimateTerminalForecast(100000, 100010, 120, []float64{0.0001, -0.0001})
	if f.Ready {
		t.Fatal("forecast must not be ready with fewer than 10 one-second returns")
	}
}

func TestTerminalForecastProbabilityAndIntervals(t *testing.T) {
	returns := make([]float64, 60)
	for i := range returns {
		if i%2 == 0 {
			returns[i] = 0.00002
		} else {
			returns[i] = -0.00001
		}
	}
	f := EstimateTerminalForecast(100000, 100000, 120, returns)
	if !f.Ready {
		t.Fatal("expected forecast to be ready")
	}
	if f.PAbove <= 0 || f.PAbove >= 1 || math.Abs(f.PAbove+f.PBelow-1) > 1e-12 {
		t.Fatalf("invalid probabilities: %+v", f)
	}
	if !(f.Lower95 < f.Lower68 && f.Lower68 < f.ForecastMedian && f.ForecastMedian < f.Upper68 && f.Upper68 < f.Upper95) {
		t.Fatalf("forecast intervals are not ordered: %+v", f)
	}
	if f.Samples != 60 {
		t.Fatalf("samples=%d want 60", f.Samples)
	}
}

func TestTerminalForecastTargetMonotonicity(t *testing.T) {
	returns := make([]float64, 60)
	for i := range returns {
		returns[i] = 0.000005 * math.Sin(float64(i))
	}
	lowTarget := EstimateTerminalForecast(100000, 99900, 90, returns)
	highTarget := EstimateTerminalForecast(100000, 101000, 90, returns)
	if !lowTarget.Ready || !highTarget.Ready {
		t.Fatal("expected forecasts to be ready")
	}
	if lowTarget.PAbove <= highTarget.PAbove {
		t.Fatalf("P(above) must fall as PTB rises: low=%f high=%f", lowTarget.PAbove, highTarget.PAbove)
	}
	if lowTarget.RequiredMoveBps >= highTarget.RequiredMoveBps {
		t.Fatalf("required move must increase with target: low=%f high=%f", lowTarget.RequiredMoveBps, highTarget.RequiredMoveBps)
	}
}

func TestTerminalForecastOutlierRobustness(t *testing.T) {
	base := make([]float64, 60)
	for i := range base {
		base[i] = 0.00001 * math.Sin(float64(i))
	}
	withOutlier := append([]float64(nil), base...)
	withOutlier[30] = 0.20
	clean := EstimateTerminalForecast(100000, 100000, 60, base)
	robust := EstimateTerminalForecast(100000, 100000, 60, withOutlier)
	if !clean.Ready || !robust.Ready {
		t.Fatal("expected forecasts to be ready")
	}
	if math.Abs(robust.ExpectedMoveBps-clean.ExpectedMoveBps) > 5.0 {
		t.Fatalf("single outlier moved forecast drift too much: clean=%f robust=%f", clean.ExpectedMoveBps, robust.ExpectedMoveBps)
	}
}

func TestTerminalForecastDoesNotCollapseOnFlatReturns(t *testing.T) {
	returns := make([]float64, 60)
	f := EstimateTerminalForecastWithContext(63921.09, 63952.50, 98, returns, ForecastContext{})
	if !f.Ready {
		t.Fatal("expected forecast to be ready")
	}
	// The previous model could produce a ~12-cent 68% band and |z| near 500.
	// With an explicit volatility prior, a 98-second BTC forecast must retain
	// meaningful uncertainty even if the last 60 observed returns are flat.
	band68 := f.Upper68 - f.Lower68
	if band68 < 10.0 {
		t.Fatalf("forecast variance collapsed: 68%% band width=%f", band68)
	}
	if math.Abs(f.TargetZ) > 10.0 {
		t.Fatalf("implausible target z-score after variance floor: %f", f.TargetZ)
	}
	if f.PAbove <= 0 || f.PAbove >= 1 {
		t.Fatalf("probability should not numerically pin at 0/1: %f", f.PAbove)
	}
	if f.VolatilityFloorAnnual < 0.199 {
		t.Fatalf("missing conservative annual volatility floor: %f", f.VolatilityFloorAnnual)
	}
}

func TestTerminalForecastUsesMacroAndBasisUncertainty(t *testing.T) {
	returns := make([]float64, 60)
	base := EstimateTerminalForecastWithContext(100000, 100020, 90, returns, ForecastContext{})
	wider := EstimateTerminalForecastWithContext(100000, 100020, 90, returns, ForecastContext{
		VolatilityFloorPerSqrtS: 0.00008,
		BasisVolatilityPerSqrtS: 0.00004,
		ModelUncertainty:        1.20,
	})
	if !base.Ready || !wider.Ready {
		t.Fatal("expected forecasts to be ready")
	}
	if wider.SigmaAtExpiryBps <= base.SigmaAtExpiryBps {
		t.Fatalf("context uncertainty should widen terminal sigma: base=%f wider=%f", base.SigmaAtExpiryBps, wider.SigmaAtExpiryBps)
	}
}

func TestTerminalForecastDriftIsHorizonCapped(t *testing.T) {
	returns := make([]float64, 60)
	for i := range returns {
		returns[i] = 0.01 // deliberately absurd persistent one-second drift
	}
	f := EstimateTerminalForecastWithContext(100000, 100000, 120, returns, ForecastContext{})
	if !f.Ready {
		t.Fatal("expected forecast to be ready")
	}
	if math.Abs(f.ExpectedMoveBps) > 0.751*f.SigmaAtExpiryBps {
		t.Fatalf("drift exceeded horizon cap: move=%f sigma=%f", f.ExpectedMoveBps, f.SigmaAtExpiryBps)
	}
}
