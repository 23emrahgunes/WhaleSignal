from pathlib import Path

p=Path('internal/arb/paper.go')
s=p.read_text()
s=s.replace('\t\tbook := bookForSide(c.FirstOrderSide, upBook, downBook)\n','')
s=s.replace('\t\tc.FirstQueueAhead = math.Max(c.FirstQueueAhead, buyQueueAhead(book, c.FirstOrderPrice))\n','')
s=s.replace('\t\tc.SecondQueueAhead = math.Max(c.SecondQueueAhead, buyQueueAhead(secondBook, c.SecondOrderPrice))\n','')
# A public SELL print strictly below a still-resting BUY limit means matching
# necessarily swept through our price; the entire hypothetical remaining order
# must have been filled before a lower bid could execute.
s=s.replace('''\t\t} else if tr.Price < orderPrice-1e-9 {\n\t\t\t// A print below our resting BUY proves the real book swept through our\n\t\t\t// price. Only the volume that actually printed beyond our level is\n\t\t\t// credited, never an automatic full fill.\n\t\t\tq = 0\n\t\t}\n\t\tif available > 0 && q <= 1e-9 {\n''','''\t\t} else if tr.Price < orderPrice-1e-9 {\n\t\t\t// A lower SELL print cannot occur while our higher resting BUY is\n\t\t\t// still unfilled. The sweep necessarily consumed our full remainder.\n\t\t\tq = 0\n\t\t\tfilled += remaining\n\t\t\tremaining = 0\n\t\t\tcontinue\n\t\t}\n\t\tif available > 0 && q <= 1e-9 {\n''')
p.write_text(s)

# Entry scanning must not manufacture edge by backing the planned completion
# quote below the current competitive maker price. If competitive completion
# does not fit the economic ceiling, the path is not an arb candidate.
p=Path('internal/arb/engine.go')
s=p.read_text()
s=s.replace('''\tif price > ceiling {\n\t\tprice = floorToTick(ceiling, opposite.TickSize)\n\t}\n\tpostOnlyCeiling := floorToTick(opposite.BestAsk-opposite.TickSize, opposite.TickSize)\n''','''\tif price > ceiling+1e-12 {\n\t\treturn 0, false\n\t}\n\tpostOnlyCeiling := floorToTick(opposite.BestAsk-opposite.TickSize, opposite.TickSize)\n''')
# Give the no-path case an explicit reason instead of presenting a synthetic
# negative edge from a zero completion price.
s=s.replace('''\tfirst := "UP"\n\tif (!upEligible && downEligible) || (upEligible == downEligible && downRisk < upRisk) {\n''','''\tif !upEligible && !downEligible {\n\t\tsnap.NetEdge = -1\n\t\tsnap.Reason = "NO_COMPETITIVE_COMPLETION_WITHIN_EDGE"\n\t\treturn snap\n\t}\n\n\tfirst := "UP"\n\tif (!upEligible && downEligible) || (upEligible == downEligible && downRisk < upRisk) {\n''')
p.write_text(s)

# The fixture is deliberately impossible at the configured paper edge once
# competitive completion is enforced.
p=Path('internal/arb/engine_test.go')
s=p.read_text().replace('TargetEdge:.02,PaperMinEdge:.01,OperationalBuffer:.002','TargetEdge:.02,PaperMinEdge:.019,OperationalBuffer:.002')
s=s.replace('''if s.PaperEdgePass || s.Status!=StatusBlocked || s.Reason!="PAIR_EDGE_BELOW_PAPER_MIN" {t.Fatalf("%+v",s)}''','''if s.PaperEdgePass || s.Status!=StatusBlocked || s.Reason!="NO_COMPETITIVE_COMPLETION_WITHIN_EDGE" {t.Fatalf("%+v",s)}''')
p.write_text(s)

# Lower-price sweep should imply the full remaining resting maker order filled.
p=Path('internal/arb/paper_test.go')
s=p.read_text()
s=s.replace('func TestLowerPrintCreditsOnlyPrintedSize', 'func TestLowerPrintProvesFullRestingOrderFilled')
s=s.replace('''if math.Abs(c.FirstFilledShares-1.25)>1e-9 { t.Fatalf("must not fake full fill %+v",c) }''','''if math.Abs(c.FirstFilledShares-5)>1e-9 || c.Status!=PaperStatusCompleting { t.Fatalf("lower sweep must fill full resting order %+v",c) }''')
p.write_text(s)

# Dashboard reason.
p=Path('web/static/index.html')
s=p.read_text().replace("'PAIR_EDGE_BELOW_PAPER_MIN':'Net maker avantajı paper araştırma eşiğinin altında'", "'PAIR_EDGE_BELOW_PAPER_MIN':'Net maker avantajı paper araştırma eşiğinin altında','NO_COMPETITIVE_COMPLETION_WITHIN_EDGE':'Karşı bacağın rekabetçi maker fiyatı ekonomik tavana sığmıyor'")
p.write_text(s)
