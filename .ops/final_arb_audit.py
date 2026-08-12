from pathlib import Path


def replace_once(path, old, new):
    p = Path(path)
    s = p.read_text()
    if old not in s:
        raise SystemExit(f'marker not found in {path}: {old[:160]!r}')
    p.write_text(s.replace(old, new, 1))

# Queue-ahead is FIFO only at OUR exact price. Better-priced bids that appear
# later have price priority, but a future print at our price proves they were
# already cleared; they must never be mixed into our same-price FIFO counter.
replace_once('internal/arb/engine.go', '''func buyQueueAhead(book polymarket.BookSnapshot, price float64) float64 {
	if price <= 0 {
		return 0
	}
	q := 0.0
	for _, level := range book.Bids {
		if level.Price+1e-12 >= price {
			q += level.Size
		}
	}
	return q
}
''', '''func buyQueueAhead(book polymarket.BookSnapshot, price float64) float64 {
	if price <= 0 {
		return 0
	}
	q := 0.0
	for _, level := range book.Bids {
		if math.Abs(level.Price-price) <= 1e-9 {
			q += level.Size
		}
	}
	return q
}
''')

# Higher-price SELL prints consume better-priced bids, not the FIFO queue at our
# lower price. They are evidence that our order has NOT been reached yet.
replace_once('internal/arb/paper.go', '''		if tr.Price > orderPrice+1e-9 {
			// Better-priced bids execute before us. Their executed volume can only
			// reduce queue ahead; it cannot fill our lower bid.
			q = math.Max(0, q-tr.Size)
			continue
		}
''', '''		if tr.Price > orderPrice+1e-9 {
			// This execution occurred at a better bid. It says nothing about the
			// FIFO volume already ahead of us at our own price.
			continue
		}
''')

# Activate the completion leg from the CURRENT book when the first leg becomes
# full. The old planned price is only an entry-time feasibility estimate; using
# it after the market moved can violate post-only or fabricate queue position.
replace_once('internal/arb/paper.go', '''			c.Status = PaperStatusCompleting
			c.Reason = "FIRST_LEG_FULL_COMPLETION_POSTED"
			c.CompletionPostedAt = c.FirstFullAt
			secondBook := bookForSide(c.SecondOrderSide, upBook, downBook)
			c.SecondQueueAhead = buyQueueAhead(secondBook, c.SecondOrderPrice)
			c.LastTradeSeq = latestSeq
''', '''			c.Status = PaperStatusCompleting
			c.CompletionPostedAt = c.FirstFullAt
			secondBook := bookForSide(c.SecondOrderSide, upBook, downBook)
			c.SecondOrderPrice = 0
			if activateCompletion(c, secondBook) {
				c.Reason = "FIRST_LEG_FULL_COMPLETION_POSTED"
			} else {
				c.Reason = "COMPLETION_WAITING_POST_ONLY_PRICE"
			}
			c.LastTradeSeq = latestSeq
''')

# If activation was temporarily impossible, retry from each fresh book before
# consuming trade events. Never infer a fill for an order that was not posted.
replace_once('internal/arb/paper.go', '''	if c.Status == PaperStatusCompleting || c.Status == PaperStatusCompletionPartial {
		secondBook := bookForSide(c.SecondOrderSide, upBook, downBook)
		delta, q := makerBuyFillFromTrades(tokenForSide(c.SecondOrderSide, c), c.SecondOrderPrice, c.SecondFilledShares, c.OrderSize, c.SecondQueueAhead, trades)
''', '''	if c.Status == PaperStatusCompleting || c.Status == PaperStatusCompletionPartial {
		secondBook := bookForSide(c.SecondOrderSide, upBook, downBook)
		if c.SecondOrderPrice <= 0 {
			if !activateCompletion(c, secondBook) {
				if strandedExpired(c, now, marketEnd, cfg) {
					timeoutStranded(c, upBook, downBook, now)
					return true
				}
				c.Reason = "COMPLETION_WAITING_POST_ONLY_PRICE"
				c.LastTradeSeq = latestSeq
				c.UpdatedAt = now.Format(time.RFC3339Nano)
				return true
			}
			c.Reason = "COMPLETION_POSTED_FROM_CURRENT_BOOK"
			c.LastTradeSeq = latestSeq
			c.UpdatedAt = now.Format(time.RFC3339Nano)
			// It was not resting during this batch; start fill accounting next batch.
			return true
		}
		delta, q := makerBuyFillFromTrades(tokenForSide(c.SecondOrderSide, c), c.SecondOrderPrice, c.SecondFilledShares, c.OrderSize, c.SecondQueueAhead, trades)
''')

# Helper: choose a CURRENT post-only completion price without violating the
# economic ceiling. Competitive maker when feasible, otherwise rest at the
# ceiling/post-only ceiling and let the timeout measure the leg risk.
insert_marker = '''func completionReprice(current, economicCeiling float64, book polymarket.BookSnapshot) (float64, bool) {'''
helper = '''func activateCompletion(c *PaperCycle, book polymarket.BookSnapshot) bool {
	if c == nil || !validBook(book) {
		return false
	}
	ceiling := c.DownCompletionMax
	if strings.EqualFold(c.SecondOrderSide, "UP") {
		ceiling = c.UpCompletionMax
	}
	if ceiling <= 0 {
		return false
	}
	postOnlyCeiling := floorToTick(book.BestAsk-book.TickSize, book.TickSize)
	if postOnlyCeiling <= 0 {
		return false
	}
	price, ok := MakerBuyPrice(book, true)
	if !ok {
		price, ok = MakerBuyPrice(book, false)
	}
	if !ok || price > ceiling+1e-12 {
		price = floorToTick(math.Min(ceiling, postOnlyCeiling), book.TickSize)
	}
	if price <= 0 || price >= book.BestAsk-1e-12 || price > ceiling+1e-12 {
		return false
	}
	c.SecondOrderPrice = price
	if strings.EqualFold(c.SecondOrderSide, "UP") {
		c.UpOrderPrice = price
	} else {
		c.DownOrderPrice = price
	}
	c.SecondQueueAhead = buyQueueAhead(book, price)
	return true
}

'''
replace_once('internal/arb/paper.go', insert_marker, helper + insert_marker)

# Engine queue test: only same-price displayed liquidity existed before us.
p = Path('internal/arb/engine_test.go')
s = p.read_text()
old_test = '''func TestQueueAheadCountsDisplayedPriority(t *testing.T) {
	b := book("u", .40, .44)
	b.Bids = []polymarket.CLOBLevel{{Price: .41, Size: 3}, {Price: .40, Size: 7}}
	if q := buyQueueAhead(b, .40); q != 10 {
		t.Fatalf("q %.2f", q)
	}
	if q := buyQueueAhead(b, .42); q != 0 {
		t.Fatalf("improved q %.2f", q)
	}
}
'''
new_test = '''func TestQueueAheadCountsOnlySamePriceFIFO(t *testing.T) {
	b := book("u", .40, .44)
	b.Bids = []polymarket.CLOBLevel{{Price: .41, Size: 3}, {Price: .40, Size: 7}}
	if q := buyQueueAhead(b, .40); q != 7 {
		t.Fatalf("same-price q %.2f", q)
	}
	if q := buyQueueAhead(b, .42); q != 0 {
		t.Fatalf("improved q %.2f", q)
	}
}
'''
if old_test not in s:
    raise SystemExit('queue regression test marker not found')
s = s.replace(old_test, new_test, 1)
p.write_text(s)

# Paper regressions: higher-price executions do not eat same-price FIFO; and
# completion activation uses the current book rather than a stale planned price.
p = Path('internal/arb/paper_test.go')
s = p.read_text()
append = r'''

func TestBetterPriceTradeDoesNotConsumeOurSamePriceQueue(t *testing.T) {
    now:=time.Date(2026,8,12,0,0,0,0,time.UTC)
    s:=paperSnap(); s.UpMakerPrice=.40
    up:=paperBook("up",.40,.44,7); down:=paperBook("down",.53,.58,100)
    c:=NewPaperCycle(s,up,down,now,0,0)
    if c.FirstQueueAhead!=7 { t.Fatalf("queue %.2f",c.FirstQueueAhead) }
    AdvancePaperCycle(c,up,down,[]polymarket.MarketTrade{sellTrade(1,"up",.41,50)},1,now.Add(time.Second),now.Add(time.Minute),DefaultPaperConfig())
    if c.FirstQueueAhead!=7 || c.FirstFilledShares!=0 { t.Fatalf("better-price print changed FIFO %+v",c) }
    AdvancePaperCycle(c,up,down,[]polymarket.MarketTrade{sellTrade(2,"up",.40,8)},2,now.Add(2*time.Second),now.Add(time.Minute),DefaultPaperConfig())
    if math.Abs(c.FirstFilledShares-1)>1e-9 { t.Fatalf("expected 1 share after 7 ahead %+v",c) }
}

func TestCompletionActivationRepricesFromCurrentBookPostOnly(t *testing.T) {
    now:=time.Date(2026,8,12,0,0,0,0,time.UTC)
    up:=paperBook("up",.40,.44,100); down:=paperBook("down",.53,.58,100)
    c:=NewPaperCycle(paperSnap(),up,down,now,0,0)
    // The entry-time planned DOWN price is .54. Before UP fills, DOWN moves to
    // .50/.53. Reusing .54 would cross the ask and a real post-only order would
    // be rejected. Activation must recompute .51 from the current book.
    downNow:=paperBook("down",.50,.53,100)
    AdvancePaperCycle(c,up,downNow,[]polymarket.MarketTrade{sellTrade(1,"up",.41,5)},1,now.Add(time.Second),now.Add(time.Minute),DefaultPaperConfig())
    if c.Status!=PaperStatusCompleting || math.Abs(c.SecondOrderPrice-.51)>1e-9 { t.Fatalf("stale completion price %+v",c) }
    if c.SecondOrderPrice>=downNow.BestAsk { t.Fatalf("completion must remain post-only %+v",c) }
}

func TestCompletionActivationCanRestAtEconomicCeilingBehindBestBid(t *testing.T) {
    now:=time.Date(2026,8,12,0,0,0,0,time.UTC)
    up:=paperBook("up",.40,.44,100); down:=paperBook("down",.53,.58,100)
    c:=NewPaperCycle(paperSnap(),up,down,now,0,0)
    // Competitive maker is now .58 but our paper economic ceiling is .56.
    // We may rest at .56; we must not manufacture a competitive .58 completion.
    downNow:=paperBook("down",.57,.60,100)
    AdvancePaperCycle(c,up,downNow,[]polymarket.MarketTrade{sellTrade(1,"up",.41,5)},1,now.Add(time.Second),now.Add(time.Minute),DefaultPaperConfig())
    if math.Abs(c.SecondOrderPrice-.56)>1e-9 { t.Fatalf("expected economic-ceiling order %+v",c) }
    if c.SecondOrderPrice>=downNow.BestAsk { t.Fatalf("not post-only %+v",c) }
}
'''
if 'TestBetterPriceTradeDoesNotConsumeOurSamePriceQueue' not in s:
    s += append
p.write_text(s)

# The preferred-first-match statistic is tautological in a safe-first strategy:
# only that side is posted. Display the useful first-leg fill rate instead.
p = Path('web/static/index.html')
s = p.read_text()
s = s.replace('Tercih Edilen İlk Bacak İsabeti', 'İlk Bacak Dolum Oranı')
s = s.replace("document.getElementById('arbPaperFirstMatch').textContent=`${pct(s.preferredFirstMatchRate||0,1)} · ${s.preferredFirstMatches||0}/${s.firstLegFilledCycles||0}`;", "document.getElementById('arbPaperFirstMatch').textContent=`${pct((s.totalCycles||0)?(s.firstLegFilledCycles||0)/(s.totalCycles||1):0,1)} · ${s.firstLegFilledCycles||0}/${s.totalCycles||0}`;")
p.write_text(s)

p = Path('docs/maker-arb-shadow.md')
s = p.read_text()
if 'same-price FIFO' not in s:
    s += '\n\n### Final queue/activation audit\n`queueAhead` tracks only FIFO liquidity already resting at the exact order price. Higher-price executions never reduce that same-price queue. When the first leg becomes fully filled, the completion order is priced again from the current CLOB and constrained by both post-only and the original economic ceiling; the entry-time planned completion price is never blindly reused.\n'
p.write_text(s)
