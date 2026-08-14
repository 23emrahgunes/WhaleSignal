// Package microfeat: mikroyapi ozelliklerinin SAF (yan-etkisiz) hesaplayicilari.
// PERSISTENCE + COHERENCE + FLOW cekirdek matematigi. Calisan sisteme dokunmaz;
// ust katmanlar (predict/regime, dual40/completion) bunlari besler.
//
// Faz 4 — SHADOW-first. Esikler burada YOK; yalniz olcumler. Karar/esik ust katmanda.
package microfeat

import (
	"math"
	"sort"
)

const eps = 1e-12

// OBI: order-book imbalance bir band icin. (bid-ask)/(bid+ask), [-1,1].
// +1 tamamen bid (alis baskin), -1 tamamen ask. Bos band -> 0.
func OBI(bidVol, askVol float64) float64 {
	s := bidVol + askVol
	if s <= eps {
		return 0
	}
	return clamp((bidVol-askVol)/s, -1, 1)
}

// SignPersistence: bir OBI (veya flow) serisinin BASKIN yonunun oranini dondurur,
// [0,1]. Isaret + isaret uyumu ne kadar yuksekse o kadar "kalici". 17/20 negatif
// -> 0.85. Denge (11/9) -> 0.55 gibi zayif. Sifirlar notr sayilir (ne + ne -).
func SignPersistence(series []float64) float64 {
	pos, neg := 0, 0
	for _, v := range series {
		if v > eps {
			pos++
		} else if v < -eps {
			neg++
		}
	}
	total := pos + neg
	if total == 0 {
		return 0
	}
	dom := pos
	if neg > dom {
		dom = neg
	}
	return float64(dom) / float64(total)
}

// DominantSign: serinin baskin isareti (+1/-1/0).
func DominantSign(series []float64) int {
	pos, neg := 0, 0
	for _, v := range series {
		if v > eps {
			pos++
		} else if v < -eps {
			neg++
		}
	}
	switch {
	case pos > neg:
		return 1
	case neg > pos:
		return -1
	default:
		return 0
	}
}

// FlipCount: ardisik isaret degisimi sayisi (sifirlar atlanir). Yuksek -> kaotik.
func FlipCount(series []float64) int {
	flips := 0
	last := 0
	for _, v := range series {
		s := 0
		if v > eps {
			s = 1
		} else if v < -eps {
			s = -1
		}
		if s == 0 {
			continue
		}
		if last != 0 && s != last {
			flips++
		}
		last = s
	}
	return flips
}

// FlipRate: flip / (isaretli-eleman-1), [0,1]. Sifir/tek eleman -> 0.
func FlipRate(series []float64) float64 {
	signed := 0
	for _, v := range series {
		if v > eps || v < -eps {
			signed++
		}
	}
	if signed < 2 {
		return 0
	}
	return float64(FlipCount(series)) / float64(signed-1)
}

// Mean: aritmetik ortalama (bos -> 0).
func Mean(xs []float64) float64 {
	if len(xs) == 0 {
		return 0
	}
	var s float64
	for _, x := range xs {
		s += x
	}
	return s / float64(len(xs))
}

// Median: (bos -> 0). Kopya alir, girdi bozulmaz.
func Median(xs []float64) float64 {
	n := len(xs)
	if n == 0 {
		return 0
	}
	c := append([]float64(nil), xs...)
	sort.Float64s(c)
	if n%2 == 1 {
		return c[n/2]
	}
	return 0.5 * (c[n/2-1] + c[n/2])
}

// Stddev: ornek std sapmasi (n<2 -> 0).
func Stddev(xs []float64) float64 {
	n := len(xs)
	if n < 2 {
		return 0
	}
	m := Mean(xs)
	var ss float64
	for _, x := range xs {
		d := x - m
		ss += d * d
	}
	return math.Sqrt(ss / float64(n-1))
}

// BandCoherence: farkli derinlik bantlarindaki OBI'lerin yon-uyumu, [0,1].
// |Σ w_i·OBI_i| / (Σ w_i·|OBI_i| + ε). 1 = hepsi ayni yon (tutarli),
// 0 = birbirini goturen celiskili bantlar (kaotik book). weights nil -> esit agirlik.
func BandCoherence(obis, weights []float64) float64 {
	if len(obis) == 0 {
		return 0
	}
	var num, den float64
	for i, o := range obis {
		w := 1.0
		if i < len(weights) {
			w = weights[i]
		}
		num += w * o
		den += w * math.Abs(o)
	}
	if den <= eps {
		return 0
	}
	return clamp(math.Abs(num)/(den+eps), 0, 1)
}

// FlowAcceleration: kisa-vadeli OFI eksi uzun-vadeli OFI. + ise akis hizlaniyor
// (kisa vade uzun vadeden guclu), - ise yavasliyor/donuyor.
func FlowAcceleration(shortOFI, longOFI float64) float64 {
	return shortOFI - longOFI
}

// DirectionConsistency: bir dizi horizon flow degerinin ortalamasinin isaret-tutarliligi
// ile buyuklugunu birlestirir. |Mean| yonunde persistence agirlikli, [0,1] isaretli:
// isaret = baskin yon; buyukluk = persistence * min(1,|mean|). Notr -> 0.
func DirectionConsistency(series []float64) float64 {
	if len(series) == 0 {
		return 0
	}
	p := SignPersistence(series)
	mag := math.Min(1, math.Abs(Mean(series)))
	v := p * mag
	if DominantSign(series) < 0 {
		return -v
	}
	return v
}

func clamp(x, lo, hi float64) float64 {
	if x < lo {
		return lo
	}
	if x > hi {
		return hi
	}
	return x
}
