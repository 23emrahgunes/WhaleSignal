package arb

import (
	"math"
	"sort"
	"strings"
)

type CompletionPolicy struct {
	Lookback                int     `json:"lookback"`
	MinSamples              int     `json:"minSamples"`
	MinStrandedSamples      int     `json:"minStrandedSamples"`
	MinPComplete5sLower95   float64 `json:"minPComplete5sLower95"`
	MinCycleEV              float64 `json:"minCycleEv"`
	MaxStrandedLossMultiple float64 `json:"maxStrandedLossMultiple"`
}

type CompletionEstimate struct {
	Ready                      bool    `json:"ready"`
	Scope                      string  `json:"scope"`
	FirstLeg                   string  `json:"firstLeg"`
	FirstFillSamples           int     `json:"firstFillSamples"`
	FirstFillCount             int     `json:"firstFillCount"`
	FirstFullCount             int     `json:"firstFullCount"`
	PFirstFill                 float64 `json:"pFirstFill"`
	PFirstFullGivenFill        float64 `json:"pFirstFullGivenFill"`
	CompletionSamples          int     `json:"completionSamples"`
	Completed250ms             int     `json:"completed250ms"`
	Completed1s                int     `json:"completed1s"`
	Completed2s                int     `json:"completed2s"`
	Completed5s                int     `json:"completed5s"`
	PComplete250ms             float64 `json:"pComplete250ms"`
	PComplete1s                float64 `json:"pComplete1s"`
	PComplete2s                float64 `json:"pComplete2s"`
	PComplete5s                float64 `json:"pComplete5s"`
	PComplete5sLower95         float64 `json:"pComplete5sLower95"`
	AverageCompletionMs        float64 `json:"averageCompletionMs"`
	FullStrandedSamples        int     `json:"fullStrandedSamples"`
	PartialStrandedSamples     int     `json:"partialStrandedSamples"`
	ExpectedPairProfit         float64 `json:"expectedPairProfit"`
	ExpectedFullStrandedPnL    float64 `json:"expectedFullStrandedPnl"`
	ExpectedPartialStrandedPnL float64 `json:"expectedPartialStrandedPnl"`
	FullCycleEV                float64 `json:"fullCycleEv"`
	ConservativeFullCycleEV    float64 `json:"conservativeFullCycleEv"`
	CycleEV                    float64 `json:"cycleEv"`
	ConservativeCycleEV        float64 `json:"conservativeCycleEv"`
	OpportunityEV              float64 `json:"opportunityEv"`
	StrandedLossMultiple       float64 `json:"strandedLossMultiple"`
	ProbabilityPass            bool    `json:"probabilityPass"`
	CycleEVPass                bool    `json:"cycleEvPass"`
	StrandedLossPass           bool    `json:"strandedLossPass"`
}

func DefaultCompletionPolicy() CompletionPolicy {
	return CompletionPolicy{
		Lookback: 500, MinSamples: 30, MinStrandedSamples: 3,
		MinPComplete5sLower95: 0.70, MinCycleEV: 0.01, MaxStrandedLossMultiple: 4,
	}
}

func ApplyCompletionModel(s *Snapshot, cycles []PaperCycle, policy CompletionPolicy) {
	if s == nil {
		return
	}
	policy = normalizeCompletionPolicy(policy)
	s.CompletionMinSamples = policy.MinSamples
	s.CompletionMinStrandedSamples = policy.MinStrandedSamples
	s.MinPComplete5sLower95 = policy.MinPComplete5sLower95
	s.MinCycleEV = policy.MinCycleEV
	s.MaxStrandedLossMultiple = policy.MaxStrandedLossMultiple

	upEst, upOK := estimatePath(cycles, s, "UP", policy)
	downEst, downOK := estimatePath(cycles, s, "DOWN", policy)
	if upOK {
		s.UpPathModelReady = upEst.Ready
		s.UpPathOpportunityEV = upEst.OpportunityEV
	}
	if downOK {
		s.DownPathModelReady = downEst.Ready
		s.DownPathOpportunityEV = downEst.OpportunityEV
	}

	// Fundamental paper gates remain authoritative. Completion-model warmup must
	// never turn an invalid book/PTB/path into a candidate.
	if !s.PTBReady || (!s.UpPathEligible && !s.DownPathEligible) {
		s.PairEdgePass = false
		return
	}

	type pathChoice struct {
		leg string
		est CompletionEstimate
	}
	live := make([]pathChoice, 0, 2)
	if upOK && pathLivePass(s, "UP", upEst, policy) {
		live = append(live, pathChoice{"UP", upEst})
	}
	if downOK && pathLivePass(s, "DOWN", downEst, policy) {
		live = append(live, pathChoice{"DOWN", downEst})
	}
	if len(live) > 0 {
		best := live[0]
		for _, c := range live[1:] {
			if c.est.OpportunityEV > best.est.OpportunityEV {
				best = c
			}
		}
		setSnapshotPath(s, best.leg)
		applyEstimate(s, best.est)
		s.SelectedBy = "COMPLETION_OPPORTUNITY_EV"
		s.Status = StatusCandidate
		s.Reason = "READY_COMPLETION_EV"
		s.PairEdgePass = true
		return
	}

	// Paper research remains on so the empirical model can warm up. If one or
	// both paths have enough data, use the higher conservative opportunity EV;
	// otherwise retain the PTB/stranded-risk safe-first selection from Engine.
	var selected CompletionEstimate
	selectedOK := false
	if upOK && upEst.Ready && s.UpPathEligible {
		selected = upEst
		selectedOK = true
		setSnapshotPath(s, "UP")
	}
	if downOK && downEst.Ready && s.DownPathEligible && (!selectedOK || downEst.OpportunityEV > selected.OpportunityEV) {
		selected = downEst
		selectedOK = true
		setSnapshotPath(s, "DOWN")
	}
	if selectedOK {
		applyEstimate(s, selected)
		s.SelectedBy = "COMPLETION_OPPORTUNITY_EV_PAPER"
	} else {
		if strings.EqualFold(s.FirstLeg, "DOWN") && downOK {
			selected, selectedOK = downEst, true
		} else if upOK {
			selected, selectedOK = upEst, true
		}
		if selectedOK {
			applyEstimate(s, selected)
		}
		s.SelectedBy = "STRANDED_RISK_WARMUP"
	}

	if !s.PaperEdgePass {
		s.Status = StatusBlocked
		s.Reason = "PAIR_EDGE_BELOW_PAPER_MIN"
		s.PairEdgePass = false
		return
	}
	s.Status = StatusPaperCandidate
	s.PairEdgePass = false
	if !selectedOK || !selected.Ready {
		s.Reason = "COMPLETION_MODEL_WARMUP"
		return
	}
	if !s.LiveEdgePass {
		s.Reason = "PAPER_READY_LIVE_EDGE_BELOW_TARGET"
		return
	}
	if !selected.ProbabilityPass {
		s.Reason = "PCOMPLETE_5S_BELOW_THRESHOLD"
		return
	}
	if !selected.CycleEVPass {
		s.Reason = "CYCLE_EV_BELOW_THRESHOLD"
		return
	}
	if !selected.StrandedLossPass {
		s.Reason = "STRANDED_LOSS_MULTIPLE_TOO_HIGH"
		return
	}
	s.Reason = "PAPER_READY_MODEL_PATH_NOT_LIVE_SELECTED"
}

func estimatePath(cycles []PaperCycle, s *Snapshot, leg string, policy CompletionPolicy) (CompletionEstimate, bool) {
	clone := *s
	if !setSnapshotPath(&clone, leg) {
		return CompletionEstimate{}, false
	}
	return EstimateCompletionModel(cycles, &clone, policy), true
}

func EstimateCompletionModel(cycles []PaperCycle, s *Snapshot, policy CompletionPolicy) CompletionEstimate {
	policy = normalizeCompletionPolicy(policy)
	out := CompletionEstimate{FirstLeg: s.FirstLeg, Scope: "NO_DATA"}
	if s == nil || s.FirstLeg == "" {
		return out
	}

	scopes := []struct {
		name string
		band float64
	}{{"LEG_EDGE_0_5PP", .005}, {"LEG_EDGE_1_5PP", .015}, {"LEG_ALL", -1}}
	var rows []PaperCycle
	for _, scope := range scopes {
		candidate := filterTrainingCycles(cycles, s.FirstLeg, s.NetEdge, scope.band)
		rows = candidate
		out.Scope = scope.name
		if countCompletionSamples(candidate) >= policy.MinSamples && len(candidate) >= policy.MinSamples {
			break
		}
	}

	out.FirstFillSamples = len(rows)
	completionMs := make([]float64, 0)
	fullStranded := make([]float64, 0)
	partialStranded := make([]float64, 0)
	for _, c := range rows {
		firstFilled := c.FirstPartialAt != "" || c.ActualFirstLeg != "" || c.FirstFilledShares > 0
		firstFull := c.FirstFullAt != "" || c.FirstFilledShares+1e-9 >= c.OrderSize && c.OrderSize > 0
		if firstFilled {
			out.FirstFillCount++
		}
		if firstFull {
			out.FirstFullCount++
		}
		if firstFull && (c.Status == PaperStatusCompleted || c.Status == PaperStatusStrandedTimeout) {
			out.CompletionSamples++
			if c.Status == PaperStatusCompleted {
				if c.CompletionMs > 0 {
					completionMs = append(completionMs, float64(c.CompletionMs))
				}
				if c.CompletionMs > 0 && c.CompletionMs <= 250 {
					out.Completed250ms++
				}
				if c.CompletionMs > 0 && c.CompletionMs <= 1000 {
					out.Completed1s++
				}
				if c.CompletionMs > 0 && c.CompletionMs <= 2000 {
					out.Completed2s++
				}
				if c.CompletionMs > 0 && c.CompletionMs <= 5000 {
					out.Completed5s++
				}
			} else {
				fullStranded = append(fullStranded, c.PaperPnL)
			}
		} else if firstFilled && !firstFull && c.Status == PaperStatusStrandedTimeout {
			partialStranded = append(partialStranded, c.PaperPnL)
		}
	}

	out.PFirstFill = betaMean(out.FirstFillCount, out.FirstFillSamples)
	out.PFirstFullGivenFill = betaMean(out.FirstFullCount, out.FirstFillCount)
	out.PComplete250ms = betaMean(out.Completed250ms, out.CompletionSamples)
	out.PComplete1s = betaMean(out.Completed1s, out.CompletionSamples)
	out.PComplete2s = betaMean(out.Completed2s, out.CompletionSamples)
	out.PComplete5s = betaMean(out.Completed5s, out.CompletionSamples)
	out.PComplete5sLower95 = wilsonLower95(out.Completed5s, out.CompletionSamples)
	if len(completionMs) > 0 {
		sum := 0.0
		for _, v := range completionMs {
			sum += v
		}
		out.AverageCompletionMs = sum / float64(len(completionMs))
	}
	out.FullStrandedSamples = len(fullStranded)
	out.PartialStrandedSamples = len(partialStranded)

	currentExit := immediateExitPnL(s)
	// Until enough genuine 5-second stranded exits exist, use an 8c/share
	// adverse-move stress floor. This prevents a perfect early completion run
	// from implying that stranded inventory has zero cost. Once the minimum
	// empirical loss sample is available, the measured distribution takes over.
	stressExit := math.Min(currentExit, -0.08*s.OrderSize)
	out.ExpectedFullStrandedPnL = empiricalOrStressPnL(fullStranded, currentExit, stressExit, policy.MinStrandedSamples)
	out.ExpectedPartialStrandedPnL = empiricalOrStressPnL(partialStranded, currentExit, stressExit, policy.MinStrandedSamples)
	out.ExpectedPairProfit = math.Max(0, s.OrderSize*s.NetEdge)
	out.FullCycleEV = out.PComplete5s*out.ExpectedPairProfit + (1-out.PComplete5s)*out.ExpectedFullStrandedPnL
	out.ConservativeFullCycleEV = out.PComplete5sLower95*out.ExpectedPairProfit + (1-out.PComplete5sLower95)*out.ExpectedFullStrandedPnL
	out.CycleEV = out.PFirstFullGivenFill*out.FullCycleEV + (1-out.PFirstFullGivenFill)*out.ExpectedPartialStrandedPnL
	out.ConservativeCycleEV = out.PFirstFullGivenFill*out.ConservativeFullCycleEV + (1-out.PFirstFullGivenFill)*out.ExpectedPartialStrandedPnL
	out.OpportunityEV = out.PFirstFill * out.ConservativeCycleEV

	worstLoss := math.Min(out.ExpectedFullStrandedPnL, out.ExpectedPartialStrandedPnL)
	if out.ExpectedPairProfit > 1e-12 {
		out.StrandedLossMultiple = math.Abs(math.Min(0, worstLoss)) / out.ExpectedPairProfit
	} else {
		out.StrandedLossMultiple = math.Inf(1)
	}
	out.Ready = out.FirstFillSamples >= policy.MinSamples && out.CompletionSamples >= policy.MinSamples
	out.ProbabilityPass = out.Ready && out.PComplete5sLower95+1e-12 >= policy.MinPComplete5sLower95
	out.CycleEVPass = out.Ready && out.ConservativeCycleEV+1e-12 >= policy.MinCycleEV
	out.StrandedLossPass = out.Ready && out.StrandedLossMultiple <= policy.MaxStrandedLossMultiple+1e-12
	return out
}

func pathLivePass(s *Snapshot, leg string, est CompletionEstimate, policy CompletionPolicy) bool {
	net := s.UpPathNetEdge
	eligible := s.UpPathEligible
	if strings.EqualFold(leg, "DOWN") {
		net = s.DownPathNetEdge
		eligible = s.DownPathEligible
	}
	return eligible && net+1e-12 >= s.TargetEdge && est.Ready && est.ProbabilityPass && est.CycleEVPass && est.StrandedLossPass
}

func setSnapshotPath(s *Snapshot, leg string) bool {
	if s == nil {
		return false
	}
	if strings.EqualFold(leg, "DOWN") {
		if !s.DownPathEligible {
			return false
		}
		s.FirstLeg = "DOWN"
		s.DownMakerPrice = s.DownPathFirstPrice
		s.UpMakerPrice = s.DownPathCompletionPrice
		s.NetEdge = s.DownPathNetEdge
		s.FirstLegQueueAhead = s.DownPathQueueAhead
		s.QuoteSkew = "SAFE_FIRST_DOWN_THEN_UP"
	} else {
		if !s.UpPathEligible {
			return false
		}
		s.FirstLeg = "UP"
		s.UpMakerPrice = s.UpPathFirstPrice
		s.DownMakerPrice = s.UpPathCompletionPrice
		s.NetEdge = s.UpPathNetEdge
		s.FirstLegQueueAhead = s.UpPathQueueAhead
		s.QuoteSkew = "SAFE_FIRST_UP_THEN_DOWN"
	}
	s.PairCost = s.UpMakerPrice + s.DownMakerPrice
	s.GrossEdge = 1 - s.PairCost
	s.PaperEdgePass = s.NetEdge+1e-12 >= s.PaperMinEdge
	s.LiveEdgePass = s.NetEdge+1e-12 >= s.TargetEdge
	s.PairEdgePass = false
	if s.PaperEdgePass {
		s.ExpectedLockedProfit = s.OrderSize * s.NetEdge
	} else {
		s.ExpectedLockedProfit = 0
	}
	return true
}

func applyEstimate(s *Snapshot, e CompletionEstimate) {
	s.CompletionModelReady = e.Ready
	s.CompletionModelScope = e.Scope
	s.FirstFillSamples = e.FirstFillSamples
	s.FirstFillCount = e.FirstFillCount
	s.PFirstFill = e.PFirstFill
	s.PFirstFullGivenFill = e.PFirstFullGivenFill
	s.CompletionSamples = e.CompletionSamples
	s.FullStrandedSamples = e.FullStrandedSamples
	s.PComplete250ms = e.PComplete250ms
	s.PComplete1s = e.PComplete1s
	s.PComplete2s = e.PComplete2s
	s.PComplete5s = e.PComplete5s
	s.PComplete5sLower95 = e.PComplete5sLower95
	s.AverageCompletionMs = e.AverageCompletionMs
	s.ExpectedPairProfitEV = e.ExpectedPairProfit
	s.ExpectedFullStrandedPnL = e.ExpectedFullStrandedPnL
	s.ExpectedPartialStrandedPnL = e.ExpectedPartialStrandedPnL
	s.FullCycleEV = e.FullCycleEV
	s.ConservativeCycleEV = e.ConservativeCycleEV
	s.OpportunityEV = e.OpportunityEV
	s.StrandedLossMultiple = e.StrandedLossMultiple
	s.CompletionProbabilityPass = e.ProbabilityPass
	s.CycleEVPass = e.CycleEVPass
	s.StrandedLossPass = e.StrandedLossPass
}

func filterTrainingCycles(cycles []PaperCycle, leg string, edge, band float64) []PaperCycle {
	out := make([]PaperCycle, 0, len(cycles))
	for _, c := range cycles {
		if c.FillModel != "WS_SELL_TRADES_PRICE_TIME_QUEUE_PARTIAL" || c.StrategyMode != "COMPLETION_PROBABILITY_SAFE_FIRST_V2" || !strings.EqualFold(c.PreferredFirstLeg, leg) {
			continue
		}
		switch c.Status {
		case PaperStatusCompleted, PaperStatusStrandedTimeout, PaperStatusExpiredNoFill:
		default:
			continue
		}
		if band >= 0 && math.Abs(c.EntryNetEdge-edge) > band+1e-12 {
			continue
		}
		out = append(out, c)
	}
	return out
}

func countCompletionSamples(rows []PaperCycle) int {
	n := 0
	for _, c := range rows {
		firstFull := c.FirstFullAt != "" || c.FirstFilledShares+1e-9 >= c.OrderSize && c.OrderSize > 0
		if firstFull && (c.Status == PaperStatusCompleted || c.Status == PaperStatusStrandedTimeout) {
			n++
		}
	}
	return n
}

func immediateExitPnL(s *Snapshot) float64 {
	if s == nil || s.OrderSize <= 0 {
		return 0
	}
	if strings.EqualFold(s.FirstLeg, "DOWN") {
		return s.OrderSize * (s.DownBestBid - s.DownMakerPrice)
	}
	return s.OrderSize * (s.UpBestBid - s.UpMakerPrice)
}

func empiricalOrStressPnL(values []float64, currentExit, stressExit float64, minSamples int) float64 {
	if minSamples < 1 {
		minSamples = 1
	}
	if len(values) < minSamples {
		return conservativePnL(values, stressExit)
	}
	return conservativePnL(values, currentExit)
}

func conservativePnL(values []float64, fallback float64) float64 {
	fallback = math.Min(0, fallback)
	if len(values) == 0 {
		return fallback
	}
	cp := append([]float64(nil), values...)
	sort.Float64s(cp)
	sum := 0.0
	for _, v := range cp {
		sum += v
	}
	avg := sum / float64(len(cp))
	q25 := cp[int(math.Floor(.25*float64(len(cp)-1)))]
	return math.Min(0, math.Min(fallback, math.Min(avg, q25)))
}

func betaMean(success, n int) float64 {
	if n <= 0 {
		return 0
	}
	return (float64(success) + .5) / (float64(n) + 1)
}

func wilsonLower95(success, n int) float64 {
	if n <= 0 {
		return 0
	}
	z := 1.959963984540054
	nn := float64(n)
	p := float64(success) / nn
	z2 := z * z
	den := 1 + z2/nn
	center := (p + z2/(2*nn)) / den
	margin := z * math.Sqrt((p*(1-p)+z2/(4*nn))/nn) / den
	return math.Max(0, center-margin)
}

func normalizeCompletionPolicy(p CompletionPolicy) CompletionPolicy {
	d := DefaultCompletionPolicy()
	if p.Lookback <= 0 {
		p.Lookback = d.Lookback
	}
	if p.MinSamples <= 0 {
		p.MinSamples = d.MinSamples
	}
	if p.MinStrandedSamples <= 0 {
		p.MinStrandedSamples = d.MinStrandedSamples
	}
	if p.MinPComplete5sLower95 <= 0 {
		p.MinPComplete5sLower95 = d.MinPComplete5sLower95
	}
	if p.MinCycleEV <= 0 {
		p.MinCycleEV = d.MinCycleEV
	}
	if p.MaxStrandedLossMultiple <= 0 {
		p.MaxStrandedLossMultiple = d.MaxStrandedLossMultiple
	}
	return p
}
