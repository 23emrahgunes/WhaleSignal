from pathlib import Path

def replace_once(path, old, new):
    p=Path(path); s=p.read_text()
    if old not in s: raise SystemExit(f'marker missing {path}: {old[:120]!r}')
    p.write_text(s.replace(old,new,1))

replace_once('internal/arb/completion_model.go',
'''\tcurrentExit := immediateExitPnL(s)\n\tout.ExpectedFullStrandedPnL = conservativePnL(fullStranded, currentExit)\n\tout.ExpectedPartialStrandedPnL = conservativePnL(partialStranded, currentExit)\n''',
'''\tcurrentExit := immediateExitPnL(s)\n\t// Until enough genuine 5-second stranded exits exist, use an 8c/share\n\t// adverse-move stress floor. This prevents a perfect early completion run\n\t// from implying that stranded inventory has zero cost. Once the minimum\n\t// empirical loss sample is available, the measured distribution takes over.\n\tstressExit := math.Min(currentExit, -0.08*s.OrderSize)\n\tout.ExpectedFullStrandedPnL = empiricalOrStressPnL(fullStranded, currentExit, stressExit, policy.MinStrandedSamples)\n\tout.ExpectedPartialStrandedPnL = empiricalOrStressPnL(partialStranded, currentExit, stressExit, policy.MinStrandedSamples)\n''')
replace_once('internal/arb/completion_model.go',
'''\tout.Ready = out.FirstFillSamples >= policy.MinSamples && out.CompletionSamples >= policy.MinSamples && out.FullStrandedSamples >= policy.MinStrandedSamples\n''',
'''\tout.Ready = out.FirstFillSamples >= policy.MinSamples && out.CompletionSamples >= policy.MinSamples\n''')
replace_once('internal/arb/completion_model.go',
'''\t\tif c.FillModel != "WS_SELL_TRADES_PRICE_TIME_QUEUE_PARTIAL" || !strings.EqualFold(c.PreferredFirstLeg, leg) {\n''',
'''\t\tif c.FillModel != "WS_SELL_TRADES_PRICE_TIME_QUEUE_PARTIAL" || c.StrategyMode != "COMPLETION_PROBABILITY_SAFE_FIRST_V2" || !strings.EqualFold(c.PreferredFirstLeg, leg) {\n''')
replace_once('internal/arb/completion_model.go',
'''func conservativePnL(values []float64, fallback float64) float64 {\n''',
'''func empiricalOrStressPnL(values []float64, currentExit, stressExit float64, minSamples int) float64 {\n\tif minSamples < 1 { minSamples = 1 }\n\tif len(values) < minSamples {\n\t\treturn conservativePnL(values, stressExit)\n\t}\n\treturn conservativePnL(values, currentExit)\n}\n\nfunc conservativePnL(values []float64, fallback float64) float64 {\n''')

# Training fixtures must represent the new policy; legacy strategy rows are intentionally excluded.
replace_once('internal/arb/completion_model_test.go',
'''StrategyMode: "SAFE_FIRST_SEQUENTIAL_MAKER"''',
'''StrategyMode: "COMPLETION_PROBABILITY_SAFE_FIRST_V2"''')

p=Path('internal/arb/completion_model_test.go')
p.write_text(p.read_text()+r'''

func TestLegacyTwentySecondPolicyCyclesAreExcluded(t *testing.T) {
    s:=modelSnap()
    rows:=make([]PaperCycle,0,40)
    for i:=0;i<40;i++ {
        c:=trainingCycle(i,"UP",PaperStatusCompleted,900,.14,true)
        c.StrategyMode="SAFE_FIRST_SEQUENTIAL_MAKER"
        rows=append(rows,c)
    }
    e:=EstimateCompletionModel(rows,s,DefaultCompletionPolicy())
    if e.FirstFillSamples!=0 || e.CompletionSamples!=0 || e.Ready { t.Fatalf("legacy leaked %+v",e) }
}

func TestPerfectEarlyRunUsesStressLossInsteadOfZeroStrandedRisk(t *testing.T) {
    s:=modelSnap(); rows:=make([]PaperCycle,0,40)
    for i:=0;i<40;i++ { rows=append(rows,trainingCycle(i,"UP",PaperStatusCompleted,800,.14,true)) }
    p:=DefaultCompletionPolicy(); p.MinSamples=30; p.MinStrandedSamples=3
    e:=EstimateCompletionModel(rows,s,p)
    if !e.Ready { t.Fatalf("should be statistically ready %+v",e) }
    want:=-0.08*s.OrderSize
    if math.Abs(e.ExpectedFullStrandedPnL-want)>1e-9 { t.Fatalf("stress %.4f want %.4f",e.ExpectedFullStrandedPnL,want) }
    if e.StrandedLossMultiple<=0 { t.Fatalf("zero stranded risk %+v",e) }
}
''')