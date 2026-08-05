package engine

import (
	"math"

	"pm-edge/internal/binance"
)

// Indicator calculations returning -1 / 0 / +1
// EMA, SMA, RSI, MACD, Stochastic, CCI, Williams %R, Momentum, ADX, VWMA, HMA

func CalculateEMA(candles []binance.Candle, period int) []float64 {
	if len(candles) == 0 {
		return nil
	}
	ema := make([]float64, len(candles))
	multiplier := 2.0 / float64(period+1)
	ema[0] = candles[0].Close

	for i := 1; i < len(candles); i++ {
		ema[i] = (candles[i].Close-ema[i-1])*multiplier + ema[i-1]
	}
	return ema
}

func CalculateSMA(candles []binance.Candle, period int) []float64 {
	if len(candles) < period {
		return nil
	}
	sma := make([]float64, len(candles))
	sum := 0.0
	for i := 0; i < period; i++ {
		sum += candles[i].Close
	}
	sma[period-1] = sum / float64(period)

	for i := period; i < len(candles); i++ {
		sum = sum - candles[i-period].Close + candles[i].Close
		sma[i] = sum / float64(period)
	}
	return sma
}

func CalculateRSI(candles []binance.Candle, period int) []float64 {
	if len(candles) < period+1 {
		return nil
	}
	rsi := make([]float64, len(candles))

	gains := 0.0
	losses := 0.0

	for i := 1; i <= period; i++ {
		diff := candles[i].Close - candles[i-1].Close
		if diff > 0 {
			gains += diff
		} else {
			losses -= diff
		}
	}

	avgGain := gains / float64(period)
	avgLoss := losses / float64(period)

	if avgLoss == 0 {
		rsi[period] = 100
	} else {
		rs := avgGain / avgLoss
		rsi[period] = 100 - (100 / (1 + rs))
	}

	for i := period + 1; i < len(candles); i++ {
		diff := candles[i].Close - candles[i-1].Close
		gain := 0.0
		loss := 0.0
		if diff > 0 {
			gain = diff
		} else {
			loss = -diff
		}

		avgGain = (avgGain*float64(period-1) + gain) / float64(period)
		avgLoss = (avgLoss*float64(period-1) + loss) / float64(period)

		if avgLoss == 0 {
			rsi[i] = 100
		} else {
			rs := avgGain / avgLoss
			rsi[i] = 100 - (100 / (1 + rs))
		}
	}
	return rsi
}

func CalculateMACD(candles []binance.Candle, fastPeriod, slowPeriod, signalPeriod int) ([]float64, []float64, []float64) {
	emaFast := CalculateEMA(candles, fastPeriod)
	emaSlow := CalculateEMA(candles, slowPeriod)
	if len(emaFast) == 0 || len(emaSlow) == 0 {
		return nil, nil, nil
	}

	macdLine := make([]float64, len(candles))
	for i := 0; i < len(candles); i++ {
		macdLine[i] = emaFast[i] - emaSlow[i]
	}

	// Calculate Signal Line (EMA of MACD Line)
	signalLine := make([]float64, len(candles))
	multiplier := 2.0 / float64(signalPeriod+1)
	signalLine[0] = macdLine[0]
	for i := 1; i < len(candles); i++ {
		signalLine[i] = (macdLine[i]-signalLine[i-1])*multiplier + signalLine[i-1]
	}

	histogram := make([]float64, len(candles))
	for i := 0; i < len(candles); i++ {
		histogram[i] = macdLine[i] - signalLine[i]
	}

	return macdLine, signalLine, histogram
}

func CalculateStochastic(candles []binance.Candle, periodK, periodD int) ([]float64, []float64) {
	if len(candles) < periodK {
		return nil, nil
	}
	fastK := make([]float64, len(candles))

	for i := periodK - 1; i < len(candles); i++ {
		highestHigh := candles[i].High
		lowestLow := candles[i].Low
		for j := i - periodK + 1; j <= i; j++ {
			if candles[j].High > highestHigh {
				highestHigh = candles[j].High
			}
			if candles[j].Low < lowestLow {
				lowestLow = candles[j].Low
			}
		}
		diff := highestHigh - lowestLow
		if diff == 0 {
			fastK[i] = 50.0
		} else {
			fastK[i] = ((candles[i].Close - lowestLow) / diff) * 100.0
		}
	}

	// Smooth %K to get %D (3-period SMA of %K)
	fastD := make([]float64, len(candles))
	for i := periodK + periodD - 2; i < len(candles); i++ {
		sum := 0.0
		for j := i - periodD + 1; j <= i; j++ {
			sum += fastK[j]
		}
		fastD[i] = sum / float64(periodD)
	}

	return fastK, fastD
}

func CalculateCCI(candles []binance.Candle, period int) []float64 {
	if len(candles) < period {
		return nil
	}
	cci := make([]float64, len(candles))
	tp := make([]float64, len(candles))
	for i := 0; i < len(candles); i++ {
		tp[i] = (candles[i].High + candles[i].Low + candles[i].Close) / 3.0
	}

	for i := period - 1; i < len(candles); i++ {
		// SMA of TP
		sum := 0.0
		for j := i - period + 1; j <= i; j++ {
			sum += tp[j]
		}
		smaTP := sum / float64(period)

		// Mean Deviation
		meanDev := 0.0
		for j := i - period + 1; j <= i; j++ {
			meanDev += math.Abs(tp[j] - smaTP)
		}
		meanDev /= float64(period)

		if meanDev == 0 {
			cci[i] = 0.0
		} else {
			cci[i] = (tp[i] - smaTP) / (0.015 * meanDev)
		}
	}
	return cci
}

func CalculateWilliamsR(candles []binance.Candle, period int) []float64 {
	if len(candles) < period {
		return nil
	}
	williamsR := make([]float64, len(candles))

	for i := period - 1; i < len(candles); i++ {
		highestHigh := candles[i].High
		lowestLow := candles[i].Low
		for j := i - period + 1; j <= i; j++ {
			if candles[j].High > highestHigh {
				highestHigh = candles[j].High
			}
			if candles[j].Low < lowestLow {
				lowestLow = candles[j].Low
			}
		}
		diff := highestHigh - lowestLow
		if diff == 0 {
			williamsR[i] = -50.0
		} else {
			williamsR[i] = ((highestHigh - candles[i].Close) / diff) * -100.0
		}
	}
	return williamsR
}

func CalculateMomentum(candles []binance.Candle, period int) []float64 {
	if len(candles) < period {
		return nil
	}
	mom := make([]float64, len(candles))
	for i := period; i < len(candles); i++ {
		mom[i] = candles[i].Close - candles[i-period].Close
	}
	return mom
}

func CalculateADX(candles []binance.Candle, period int) []float64 {
	if len(candles) < period*2 {
		return nil
	}
	adx := make([]float64, len(candles))
	tr := make([]float64, len(candles))
	pDM := make([]float64, len(candles))
	mDM := make([]float64, len(candles))

	for i := 1; i < len(candles); i++ {
		h1 := candles[i].High
		l1 := candles[i].Low
		yClose := candles[i-1].Close

		trVal := math.Max(h1-l1, math.Max(math.Abs(h1-yClose), math.Abs(l1-yClose)))
		tr[i] = trVal

		upMove := h1 - candles[i-1].High
		downMove := candles[i-1].Low - l1

		if upMove > downMove && upMove > 0 {
			pDM[i] = upMove
		} else {
			pDM[i] = 0
		}

		if downMove > upMove && downMove > 0 {
			mDM[i] = downMove
		} else {
			mDM[i] = 0
		}
	}

	// Smooth using Wilder's technique
	smoothedTR := make([]float64, len(candles))
	smoothedPDM := make([]float64, len(candles))
	smoothedMDM := make([]float64, len(candles))

	trSum := 0.0
	pdmSum := 0.0
	mdmSum := 0.0
	for i := 1; i <= period; i++ {
		trSum += tr[i]
		pdmSum += pDM[i]
		mdmSum += mDM[i]
	}
	smoothedTR[period] = trSum
	smoothedPDM[period] = pdmSum
	smoothedMDM[period] = mdmSum

	for i := period + 1; i < len(candles); i++ {
		smoothedTR[i] = smoothedTR[i-1] - (smoothedTR[i-1] / float64(period)) + tr[i]
		smoothedPDM[i] = smoothedPDM[i-1] - (smoothedPDM[i-1] / float64(period)) + pDM[i]
		smoothedMDM[i] = smoothedMDM[i-1] - (smoothedMDM[i-1] / float64(period)) + mDM[i]
	}

	dx := make([]float64, len(candles))
	for i := period; i < len(candles); i++ {
		pDI := (smoothedPDM[i] / smoothedTR[i]) * 100
		mDI := (smoothedMDM[i] / smoothedTR[i]) * 100
		sum := pDI + mDI
		diff := math.Abs(pDI - mDI)
		if sum == 0 {
			dx[i] = 0
		} else {
			dx[i] = (diff / sum) * 100
		}
	}

	// Smooth DX to get ADX
	dxSum := 0.0
	for i := period; i < period*2; i++ {
		dxSum += dx[i]
	}
	adx[period*2-1] = dxSum / float64(period)

	for i := period * 2; i < len(candles); i++ {
		adx[i] = (adx[i-1]*float64(period-1) + dx[i]) / float64(period)
	}

	return adx
}

func CalculateVWMA(candles []binance.Candle, period int) []float64 {
	if len(candles) < period {
		return nil
	}
	vwma := make([]float64, len(candles))
	for i := period - 1; i < len(candles); i++ {
		pvSum := 0.0
		vSum := 0.0
		for j := i - period + 1; j <= i; j++ {
			pvSum += candles[j].Close * candles[j].Volume
			vSum += candles[j].Volume
		}
		if vSum == 0 {
			vwma[i] = candles[i].Close
		} else {
			vwma[i] = pvSum / vSum
		}
	}
	return vwma
}

// CalculateHMA calculates Hull Moving Average.
func CalculateHMA(candles []binance.Candle, period int) []float64 {
	halfPeriod := period / 2
	sqrtPeriod := int(math.Sqrt(float64(period)))

	wmaHalf := CalculateWMA(candles, halfPeriod)
	wmaFull := CalculateWMA(candles, period)

	if len(wmaHalf) == 0 || len(wmaFull) == 0 {
		return nil
	}

	rawHMA := make([]binance.Candle, len(candles))
	for i := 0; i < len(candles); i++ {
		val := 0.0
		if i < len(wmaHalf) && i < len(wmaFull) {
			val = 2.0*wmaHalf[i] - wmaFull[i]
		}
		rawHMA[i] = binance.Candle{Close: val}
	}

	return CalculateWMA(rawHMA, sqrtPeriod)
}

func CalculateWMA(candles []binance.Candle, period int) []float64 {
	if len(candles) < period {
		return nil
	}
	wma := make([]float64, len(candles))
	weightSum := float64(period * (period + 1) / 2)

	for i := period - 1; i < len(candles); i++ {
		sum := 0.0
		weight := 1.0
		for j := i - period + 1; j <= i; j++ {
			sum += candles[j].Close * weight
			weight++
		}
		wma[i] = sum / weightSum
	}
	return wma
}

// GetIndicatorScores compiles all indicator outputs into individual direction signals (-1, 0, or 1)
func GetIndicatorScores(candles1m []binance.Candle, candles5m []binance.Candle) map[string]int {
	scores := make(map[string]int)

	if len(candles1m) < 50 || len(candles5m) < 50 {
		return scores // not enough warmup data
	}

	// 1. EMA (9, 21, 50)
	ema9 := CalculateEMA(candles1m, 9)
	ema21 := CalculateEMA(candles1m, 21)
	ema50 := CalculateEMA(candles1m, 50)
	if len(ema9) > 0 && len(ema21) > 0 && len(ema50) > 0 {
		lastIdx := len(candles1m) - 1
		if ema9[lastIdx] > ema21[lastIdx] && ema21[lastIdx] > ema50[lastIdx] {
			scores["EMA"] = 1
		} else if ema9[lastIdx] < ema21[lastIdx] && ema21[lastIdx] < ema50[lastIdx] {
			scores["EMA"] = -1
		} else {
			scores["EMA"] = 0
		}
	}

	// 2. SMA (20)
	sma20 := CalculateSMA(candles1m, 20)
	if len(sma20) > 0 {
		lastIdx := len(candles1m) - 1
		if candles1m[lastIdx].Close > sma20[lastIdx] {
			scores["SMA"] = 1
		} else {
			scores["SMA"] = -1
		}
	}

	// 3. RSI (14)
	rsi := CalculateRSI(candles1m, 14)
	if len(rsi) > 0 {
		lastVal := rsi[len(rsi)-1]
		if lastVal > 70 {
			scores["RSI"] = -1 // overbought, bearish reversal expectation
		} else if lastVal < 30 {
			scores["RSI"] = 1 // oversold, bullish reversal expectation
		} else {
			scores["RSI"] = 0
		}
	}

	// 4. MACD (12, 26, 9)
	_, _, hist := CalculateMACD(candles1m, 12, 26, 9)
	if len(hist) > 0 {
		lastVal := hist[len(hist)-1]
		if lastVal > 0 {
			scores["MACD"] = 1
		} else {
			scores["MACD"] = -1
		}
	}

	// 5. Stochastic
	fastK, fastD := CalculateStochastic(candles1m, 14, 3)
	if len(fastK) > 0 && len(fastD) > 0 {
		lastK := fastK[len(fastK)-1]
		lastD := fastD[len(fastD)-1]
		if lastK > 80 && lastD > 80 {
			scores["Stochastic"] = -1
		} else if lastK < 20 && lastD < 20 {
			scores["Stochastic"] = 1
		} else {
			scores["Stochastic"] = 0
		}
	}

	// 6. CCI (20)
	cci := CalculateCCI(candles1m, 20)
	if len(cci) > 0 {
		lastCCI := cci[len(cci)-1]
		if lastCCI > 100 {
			scores["CCI"] = 1
		} else if lastCCI < -100 {
			scores["CCI"] = -1
		} else {
			scores["CCI"] = 0
		}
	}

	// 7. Williams %R (14)
	wR := CalculateWilliamsR(candles1m, 14)
	if len(wR) > 0 {
		lastWR := wR[len(wR)-1]
		if lastWR > -20 {
			scores["WilliamsR"] = -1
		} else if lastWR < -80 {
			scores["WilliamsR"] = 1
		} else {
			scores["WilliamsR"] = 0
		}
	}

	// 8. Momentum (10)
	mom := CalculateMomentum(candles1m, 10)
	if len(mom) > 0 {
		lastMom := mom[len(mom)-1]
		if lastMom > 0 {
			scores["Momentum"] = 1
		} else if lastMom < 0 {
			scores["Momentum"] = -1
		} else {
			scores["Momentum"] = 0
		}
	}

	// 9. ADX (14) (use 5m for larger timeframes)
	adx := CalculateADX(candles5m, 14)
	if len(adx) > 0 {
		lastADX := adx[len(adx)-1]
		if lastADX > 25 {
			// strong trend. Let's align with general candles close vs SMA trend
			sma5m := CalculateSMA(candles5m, 20)
			if len(sma5m) > 0 && candles5m[len(candles5m)-1].Close > sma5m[len(sma5m)-1] {
				scores["ADX"] = 1
			} else {
				scores["ADX"] = -1
			}
		} else {
			scores["ADX"] = 0
		}
	}

	// 10. VWMA (20)
	vwma := CalculateVWMA(candles1m, 20)
	if len(vwma) > 0 {
		lastIdx := len(candles1m) - 1
		if candles1m[lastIdx].Close > vwma[lastIdx] {
			scores["VWMA"] = 1
		} else {
			scores["VWMA"] = -1
		}
	}

	// 11. HMA (20)
	hma := CalculateHMA(candles1m, 20)
	if len(hma) > 0 {
		lastIdx := len(candles1m) - 1
		if candles1m[lastIdx].Close > hma[lastIdx] {
			scores["HMA"] = 1
		} else {
			scores["HMA"] = -1
		}
	}

	return scores
}
