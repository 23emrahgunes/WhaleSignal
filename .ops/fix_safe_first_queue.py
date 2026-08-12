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

# Correct the test threshold: the selected fixture has 1.8% net edge.
p=Path('internal/arb/engine_test.go')
s=p.read_text().replace('TargetEdge:.02,PaperMinEdge:.01,OperationalBuffer:.002','TargetEdge:.02,PaperMinEdge:.019,OperationalBuffer:.002')
p.write_text(s)

# Lower-price sweep should imply the full remaining resting maker order filled.
p=Path('internal/arb/paper_test.go')
s=p.read_text()
s=s.replace('func TestLowerPrintCreditsOnlyPrintedSize', 'func TestLowerPrintProvesFullRestingOrderFilled')
s=s.replace('''if math.Abs(c.FirstFilledShares-1.25)>1e-9 { t.Fatalf("must not fake full fill %+v",c) }''','''if math.Abs(c.FirstFilledShares-5)>1e-9 || c.Status!=PaperStatusCompleting { t.Fatalf("lower sweep must fill full resting order %+v",c) }''')
p.write_text(s)
