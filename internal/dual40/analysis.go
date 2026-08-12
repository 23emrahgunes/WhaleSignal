package dual40

import (
	"math"
	"sort"
)

// BucketStat: bir first-fill feature bucket'inda koşullu completion + PnL.
type BucketStat struct {
	Key               string  `json:"key"`
	FirstFillN        int     `json:"firstFillN"`
	Completed         int     `json:"completed"`
	Hedged            int     `json:"hedged"`
	PSecondGivenFirst float64 `json:"pSecondGivenFirst"`
	MeanPnL           float64 `json:"meanPnl"`
	NetPnL            float64 `json:"netPnl"`
}

// Analysis: dual40 box motorunun ISTATISTIKSEL KANITI.
//   - ResolvedN/NetPnL/MeanPnL/SEPnL/TStat: posted+resolved trial'lar uzerinde
//     net EV ve anlamlilik (|t|>2 & n>=30 => Significant).
//   - DualFillRate = P(second fill | first fill) = Completed/(Completed+Hedged).
//   - ByRegime/ByDrift: first-fill ANI mikroyapisina gore kosullu tablo — edge
//     hangi bucket'ta (varsa) yasiyor.
type Analysis struct {
	ResolvedN    int          `json:"resolvedN"`
	NetPnL       float64      `json:"netPnl"`
	MeanPnL      float64      `json:"meanPnl"`
	SEPnL        float64      `json:"sePnl"`
	TStat        float64      `json:"tStat"`
	Significant  bool         `json:"significant"`
	FirstFillN   int          `json:"firstFillN"`
	Completed    int          `json:"completed"`
	Hedged       int          `json:"hedged"`
	DualFillRate float64      `json:"dualFillRate"`
	ByRegime     []BucketStat `json:"byRegime"`
	ByDrift      []BucketStat `json:"byDrift"`
}

// isResolvedTerminal: ekonomik sonucu tanimli terminal durum. DataGapInvalid
// (bozuk veri) ve Skipped (hic girilmedi) HARIC.
func isResolvedTerminal(state string) bool {
	switch state {
	case StateCompleted, StateHedged, StatePartialPair, StateExpiredNoFill:
		return true
	}
	return false
}

type bucketAcc struct {
	firstFillN int
	completed  int
	hedged     int
	sumPnL     float64
}

func addBucket(m map[string]*bucketAcc, key string, t Trial) {
	b := m[key]
	if b == nil {
		b = &bucketAcc{}
		m[key] = b
	}
	b.firstFillN++
	b.sumPnL += t.PaperPnL
	switch t.State {
	case StateCompleted:
		b.completed++
	case StateHedged:
		b.hedged++
	}
}

func finalizeBuckets(m map[string]*bucketAcc) []BucketStat {
	out := make([]BucketStat, 0, len(m))
	for k, b := range m {
		bs := BucketStat{Key: k, FirstFillN: b.firstFillN, Completed: b.completed, Hedged: b.hedged, NetPnL: b.sumPnL}
		if b.completed+b.hedged > 0 {
			bs.PSecondGivenFirst = float64(b.completed) / float64(b.completed+b.hedged)
		}
		if b.firstFillN > 0 {
			bs.MeanPnL = b.sumPnL / float64(b.firstFillN)
		}
		out = append(out, bs)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	return out
}

func regimeBucketKey(r string) string {
	if r == "" {
		return "UNKNOWN"
	}
	return r
}

func driftBucketKey(driftBps float64) string {
	a := math.Abs(driftBps)
	switch {
	case a < 2:
		return "drift<2bps"
	case a < 4:
		return "drift2-4bps"
	case a < 8:
		return "drift4-8bps"
	default:
		return "drift>8bps"
	}
}

// AnalyzeTrials: trial listesinden istatistiksel kaniti hesaplar (saf fonksiyon).
func AnalyzeTrials(trials []Trial) Analysis {
	var a Analysis
	var sum, sumSq float64
	regime := map[string]*bucketAcc{}
	drift := map[string]*bucketAcc{}

	for i := range trials {
		t := trials[i]
		if !isResolvedTerminal(t.State) {
			continue
		}
		a.ResolvedN++
		sum += t.PaperPnL
		sumSq += t.PaperPnL * t.PaperPnL
		switch t.State {
		case StateCompleted:
			a.Completed++
		case StateHedged:
			a.Hedged++
		}
		if t.FirstFillAt != "" {
			a.FirstFillN++
			addBucket(regime, regimeBucketKey(t.FirstFillRegime), t)
			addBucket(drift, driftBucketKey(t.FirstFillDriftBps), t)
		}
	}

	a.NetPnL = sum
	if a.ResolvedN > 0 {
		a.MeanPnL = sum / float64(a.ResolvedN)
	}
	if a.ResolvedN > 1 {
		n := float64(a.ResolvedN)
		varr := math.Max((sumSq-sum*sum/n)/(n-1), 0)
		std := math.Sqrt(varr)
		a.SEPnL = std / math.Sqrt(n)
		if a.SEPnL > 0 {
			a.TStat = a.MeanPnL / a.SEPnL
		}
	}
	a.Significant = math.Abs(a.TStat) > 2 && a.ResolvedN >= 30
	if a.Completed+a.Hedged > 0 {
		a.DualFillRate = float64(a.Completed) / float64(a.Completed+a.Hedged)
	}
	a.ByRegime = finalizeBuckets(regime)
	a.ByDrift = finalizeBuckets(drift)
	return a
}
