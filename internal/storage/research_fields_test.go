package storage

import (
	"math"
	"path/filepath"
	"testing"
)

func TestSignalResearchDiagnosticsPersistForCalibration(t *testing.T) {
	db, err := NewDatabase(filepath.Join(t.TempDir(), "research.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	r := validSignal()
	r.BinancePrice = 64063.25
	r.ChainlinkBinanceBasisBps = -6.42
	r.ForecastSamples = 60
	r.ForecastPrice = 64012.50
	r.ForecastMeanPrice = 64012.75
	r.ForecastLow68 = 63985.10
	r.ForecastHigh68 = 64039.90
	r.ForecastLow95 = 63958.20
	r.ForecastHigh95 = 64066.80
	r.PTBZ = 0.73
	r.RequiredMoveBps = -1.56
	r.ExpectedMoveBps = 0.45
	r.ForecastSigmaExpiryBps = 3.10
	r.ForecastConfidence = 53.4
	r.MicroVolatilityAnnual = 0.31
	r.VolatilityFloorAnnual = 0.24
	r.BasisVolatilityAnnual = 0.07

	if err := db.InsertSignal(r); err != nil {
		t.Fatalf("InsertSignal: %v", err)
	}
	history, err := db.GetHistory(1)
	if err != nil {
		t.Fatal(err)
	}
	if len(history) != 1 {
		t.Fatalf("history len=%d", len(history))
	}
	got := history[0]
	checks := []struct {
		name string
		got  float64
		want float64
	}{
		{"binancePrice", got.BinancePrice, r.BinancePrice},
		{"basisBps", got.ChainlinkBinanceBasisBps, r.ChainlinkBinanceBasisBps},
		{"forecastPrice", got.ForecastPrice, r.ForecastPrice},
		{"forecastMean", got.ForecastMeanPrice, r.ForecastMeanPrice},
		{"low68", got.ForecastLow68, r.ForecastLow68},
		{"high68", got.ForecastHigh68, r.ForecastHigh68},
		{"low95", got.ForecastLow95, r.ForecastLow95},
		{"high95", got.ForecastHigh95, r.ForecastHigh95},
		{"ptbZ", got.PTBZ, r.PTBZ},
		{"requiredMove", got.RequiredMoveBps, r.RequiredMoveBps},
		{"expectedMove", got.ExpectedMoveBps, r.ExpectedMoveBps},
		{"sigmaExpiry", got.ForecastSigmaExpiryBps, r.ForecastSigmaExpiryBps},
		{"forecastConfidence", got.ForecastConfidence, r.ForecastConfidence},
		{"microVol", got.MicroVolatilityAnnual, r.MicroVolatilityAnnual},
		{"floorVol", got.VolatilityFloorAnnual, r.VolatilityFloorAnnual},
		{"basisVol", got.BasisVolatilityAnnual, r.BasisVolatilityAnnual},
	}
	for _, c := range checks {
		if math.Abs(c.got-c.want) > 1e-9 {
			t.Fatalf("%s got=%f want=%f", c.name, c.got, c.want)
		}
	}
	if got.ForecastSamples != 60 {
		t.Fatalf("forecastSamples=%d", got.ForecastSamples)
	}
}

func TestSignalResearchColumnsExistAfterMigration(t *testing.T) {
	db, err := NewDatabase(filepath.Join(t.TempDir(), "columns.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	rows, err := db.db.Query("PRAGMA table_info(signals)")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	seen := map[string]bool{}
	for rows.Next() {
		var cid, notnull, pk int
		var name, typ string
		var defaultValue interface{}
		if err := rows.Scan(&cid, &name, &typ, &notnull, &defaultValue, &pk); err != nil {
			t.Fatal(err)
		}
		seen[name] = true
	}
	for _, name := range []string{"ptb_z", "forecast_sigma_expiry_bps", "chainlink_binance_basis_bps", "micro_volatility_annual", "volatility_floor_annual", "basis_volatility_annual"} {
		if !seen[name] {
			t.Fatalf("missing research column %s", name)
		}
	}
}
