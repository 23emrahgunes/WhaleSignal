package main

import (
	"context"
	"fmt"
	"math"
	"sync"
	"sync/atomic"
	"time"

	"go.uber.org/zap"
	"pm-edge/internal/binance"
	"pm-edge/internal/chainlink"
	"pm-edge/internal/clob"
	"pm-edge/internal/dual40"
	"pm-edge/internal/engine"
	"pm-edge/internal/microfeat"
	"pm-edge/internal/polymarket"
	"pm-edge/internal/predict"
	"pm-edge/internal/storage"
	"pm-edge/internal/util"
)

type dual40Runtime struct {
	db                *storage.Database
	pmClient          *polymarket.Client
	clClient          *chainlink.Client
	tradeStream       *polymarket.MarketTradeStream
	enabled           bool
	cfg               dual40.Config
	feeRate           float64
	latencyBuffer     float64
	tradeStreamMaxAge time.Duration
	busy              atomic.Bool

	execMode    string                 // shadow|dry|live (baslangic)
	exec        *clob.Client           // shadow'da nil; dry/live'de imzalayici (live bayragi runtime)
	killReq     atomic.Bool            // buton -> tum acik emirleri iptal + DRY'ye dus
	lastExecErr atomic.Pointer[string] // son kopru/emir hatasi (panelde gosterilir)

	// Model B (yeni mikroyapi beyni, SHADOW): her tick feature toplar + regime/
	// direction motorlarini calistirir. Karar VERMEZ; dashboard'a gozlem sunar.
	mfCollector  *microfeat.Collector
	dirModel     predict.LogisticModel
	regimeThresh predict.Thresholds
	dirConfMin   float64
	lastModelB   atomic.Pointer[ModelBResult]

	marketSlug    string
	market        *polymarket.Market // aktif market (roll aninda ESKI market settle icin)
	priceToBeat   float64            // aktif marketin event openPrice'i (Chainlink strike)
	samples       []dual40.Sample
	evaluated     map[int]bool
	active        map[int]*dual40.Trial
	marketEntered bool // bu markette bir kutu acildi mi (tek giris/market kurali)
}

// SetLive: butondan DRY<->CANLI. exec yoksa (shadow) etkisiz.
func (r *dual40Runtime) SetLive(v bool) error {
	if r == nil || r.exec == nil {
		return fmt.Errorf("executor yok (shadow modda baslatildi)")
	}
	r.exec.SetLive(v)
	return nil
}

// RequestKill: tum acik emirleri iptal + DRY'ye dus (observer bir sonraki tick'te uygular).
func (r *dual40Runtime) RequestKill() {
	if r == nil {
		return
	}
	if r.exec != nil {
		r.exec.SetLive(false)
	}
	r.killReq.Store(true)
}

// Status: gosterim icin mevcut mod.
func (r *dual40Runtime) Status() string {
	if r == nil || r.exec == nil {
		return "shadow"
	}
	if r.exec.IsLive() {
		return "live"
	}
	return "dry"
}

// setExecErr / ExecErr: son kopru/emir hatasini tutar (dashboard panelinde gosterilir).
func (r *dual40Runtime) setExecErr(err error) {
	if err == nil {
		return
	}
	s := time.Now().UTC().Format("15:04:05") + " · " + err.Error()
	r.lastExecErr.Store(&s)
}

func (r *dual40Runtime) ExecErr() string {
	if p := r.lastExecErr.Load(); p != nil {
		return *p
	}
	return ""
}

// placeBoxLegs: iki bacagi da post-only GTC olarak executor'a verir; order id'leri
// trial'a yazar. DRY'de imzalanip loglanir (POST yok), live'de gercek gonderilir.
func (r *dual40Runtime) placeBoxLegs(t *dual40.Trial, upID, downID string) {
	if up, err := r.exec.PlaceLimit(upID, clob.Buy, t.Shares, t.EntryPrice); err != nil {
		util.Logger.Warn("DUAL40 UP bacak emri basarisiz", zap.Int64("id", t.ID), zap.Error(err))
		r.setExecErr(err)
	} else {
		t.UpOrderID = up
	}
	if dn, err := r.exec.PlaceLimit(downID, clob.Buy, t.Shares, t.EntryPrice); err != nil {
		util.Logger.Warn("DUAL40 DOWN bacak emri basarisiz", zap.Int64("id", t.ID), zap.Error(err))
		r.setExecErr(err)
	} else {
		t.DownOrderID = dn
	}
}

// cancelResting: trial'in acik resting emirlerini iptal eder (executor). Dolmus/
// yok emirde CLOB no-op; DRY'de no-op.
func (r *dual40Runtime) cancelResting(t *dual40.Trial) {
	if r.exec == nil {
		return
	}
	for _, id := range []string{t.UpOrderID, t.DownOrderID} {
		if id != "" {
			_ = r.exec.Cancel(id)
		}
	}
}

// killAll: kill-switch — tum acik emirleri iptal, acik trial'lari KILL_SWITCH ile
// kapat, takibi birak. exec zaten RequestKill'de DRY'ye alindi.
func (r *dual40Runtime) killAll(now time.Time) {
	for sec, t := range r.active {
		r.cancelResting(t)
		if dual40.InvalidateDataGap(t, now, "KILL_SWITCH") {
			_ = r.db.UpdateDual40Trial(t)
		}
		delete(r.active, sec)
	}
	util.Logger.Warn("DUAL40 KILL-SWITCH: tum acik emirler iptal, DRY moda dusuldu")
}

// hedgeLive: hedge edilen tarafin resting emrini iptal edip o tarafi marketable
// (FAK) verir; order id'yi trial'a yazar.
func (r *dual40Runtime) hedgeLive(t *dual40.Trial, side, tokenID string, shares, price float64) {
	restingID := t.UpOrderID
	if side == "DOWN" {
		restingID = t.DownOrderID
	}
	if restingID != "" {
		_ = r.exec.Cancel(restingID)
	}
	if id, err := r.exec.PlaceMarketable(tokenID, clob.Buy, shares, price); err != nil {
		util.Logger.Warn("DUAL40 hedge emri basarisiz", zap.Int64("id", t.ID), zap.String("side", side), zap.Error(err))
		r.setExecErr(err)
	} else {
		t.HedgeOrderID = id
	}
}

func newDual40Runtime(tf string, cfg dual40.Config, db *storage.Database, pmClient *polymarket.Client, enabled bool, feeRate, latencyBuffer float64, tradeStreamMaxAge time.Duration) *dual40Runtime {
	cfg = dual40.NormalizeConfig(cfg)
	if tradeStreamMaxAge <= 0 {
		tradeStreamMaxAge = 20 * time.Second
	}
	r := &dual40Runtime{
		db: db, pmClient: pmClient, tradeStream: polymarket.NewMarketTradeStream(), enabled: enabled,
		cfg: cfg, feeRate: feeRate, latencyBuffer: latencyBuffer, tradeStreamMaxAge: tradeStreamMaxAge,
		evaluated: make(map[int]bool), active: make(map[int]*dual40.Trial),
	}
	if err := db.EnsureDual40Schema(); err != nil {
		util.Logger.Error("Dual40 schema setup failed; shadow engine disabled", zap.Error(err))
		r.enabled = false
		return r
	}
	if enabled {
		r.tradeStream.Start()
		if open, err := db.GetOpenDual40TrialsByTimeframe(tf); err != nil {
			util.Logger.Warn("Dual40 open-trial restore read failed", zap.Error(err))
		} else {
			now := time.Now().UTC()
			for i := range open {
				t := &open[i]
				if dual40.InvalidateDataGap(t, now, "PROCESS_RESTART_DATA_GAP") {
					if err := db.UpdateDual40Trial(t); err != nil {
						util.Logger.Warn("Dual40 stale open trial invalidation failed", zap.Int64("id", t.ID), zap.Error(err))
					}
				}
			}
		}
	}
	return r
}

// StartObserver is deliberately independent from the directional/PTB evaluator.
// Dual40 needs the true first 5/10/20 seconds of each market. The old implementation
// sampled only after the full evaluator became ready, which could be 10-20 seconds
// into the market and made every opening window fail closed as INSUFFICIENT.
func (r *dual40Runtime) StartObserver(ctx context.Context, clClient *chainlink.Client, bClient *binance.Client, microClient *binance.MicrostructureClient, execMode string, exec *clob.Client) {
	if r == nil || !r.enabled || ctx == nil || bClient == nil || microClient == nil {
		return
	}
	r.clClient = clClient
	r.execMode = execMode
	r.exec = exec
	// Model B shadow beyni init (240 ornek ~ 4dk/1s; esikler/tohum model kalibrasyona acik).
	r.mfCollector = microfeat.NewCollector(240, nil)
	r.regimeThresh = defaultRegimeThresholds()
	r.dirModel = defaultDirModel()
	r.dirConfMin = 0.60
	util.Logger.Info("DUAL40 OPENING OBSERVER STARTED", zap.String("execMode", execMode), zap.Bool("executor", exec != nil), zap.Duration("cadence", time.Second))
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()

		observe := func(now time.Time) {
			if !bClient.IsPriceFresh(3 * time.Second) {
				return
			}
			spot := bClient.GetPrice()
			if spot <= 0 {
				return
			}
			market, err := r.pmClient.FetchActiveBTC5mMarket()
			if err != nil || market == nil {
				return
			}
			deep := microClient.Snapshot(spot, spot, now)
			if !deep.TradeFlowAvailable || len(deep.Trades) == 0 {
				return
			}
			// FIYAT KAYNAGI = CHAINLINK (settle oracle'i). Binance yalniz akis/flow.
			var clPrice float64
			if r.clClient != nil {
				clPrice = r.clClient.Snapshot(market.StartTime, now).CurrentPrice
			}
			res := &engine.EvaluationResult{
				Timestamp:          now.UTC().Format(time.RFC3339Nano),
				CurrentPrice:       clPrice,
				BinancePrice:       spot,
				OrderFlowScore:     deep.Trades[0].Imbalance,
				DeepMicrostructure: deep,
			}
			if err := r.tick(res, market); err != nil {
				util.Logger.Debug("Dual40 opening observer tick skipped", zap.Error(err), zap.String("market", market.Slug))
			}
		}

		observe(time.Now().UTC())
		for {
			select {
			case <-ctx.Done():
				return
			case now := <-ticker.C:
				observe(now.UTC())
			}
		}
	}()
}

func (r *dual40Runtime) Submit(res *engine.EvaluationResult, market *polymarket.Market) {
	if r == nil || !r.enabled || res == nil || market == nil || !r.busy.CompareAndSwap(false, true) {
		return
	}
	rc := *res
	mc := *market
	mc.Tokens = append([]polymarket.Token(nil), market.Tokens...)
	go func() {
		defer r.busy.Store(false)
		if err := r.tick(&rc, &mc); err != nil {
			util.Logger.Warn("Dual40 shadow tick skipped", zap.Error(err), zap.String("market", mc.Slug))
		}
	}()
}

func (r *dual40Runtime) tick(res *engine.EvaluationResult, market *polymarket.Market) error {
	now := time.Now().UTC()
	if parsed, err := time.Parse(time.RFC3339Nano, res.Timestamp); err == nil {
		now = parsed.UTC()
	}
	if r.killReq.Swap(false) {
		r.killAll(now)
	}
	upID, ok := polymarket.TokenIDForOutcome(market, "UP")
	if !ok {
		return fmt.Errorf("missing UP token")
	}
	downID, ok := polymarket.TokenIDForOutcome(market, "DOWN")
	if !ok {
		return fmt.Errorf("missing DOWN token")
	}
	if r.marketSlug != market.Slug {
		r.rollMarket(market.Slug, []string{upID, downID}, now)
	}
	r.market = market // roll'dan SONRA guncelle (roll eski market'i settle icin kullandi)
	// priceToBeat = event openPrice (Chainlink strike). Market obj'de varsa onu al,
	// yoksa event sayfasindan cek. Gelene kadar her tick tekrar dene.
	if r.priceToBeat <= 0 {
		if market.PriceToBeat > 0 {
			r.priceToBeat = market.PriceToBeat
		} else if ptb, ferr := r.pmClient.FetchOpenPriceFromEvent(market); ferr == nil && ptb > 0 {
			r.priceToBeat = ptb
		}
	}

	upBook, downBook, err := r.fetchPairBooks(upID, downID)
	if err != nil {
		return err
	}
	elapsed := now.Sub(market.StartTime.UTC()).Seconds()
	if elapsed < -1 {
		return nil
	}
	// Ornek fiyati = Chainlink current (yoksa Binance yedek).
	price := res.CurrentPrice
	if price <= 0 {
		price = res.BinancePrice
	}
	if price > 0 {
		r.addSample(dual40.Sample{
			ElapsedSec:    elapsed,
			Price:         price,
			FlowImbalance: dual40FlowImbalance(res),
			UpMid:         0.5 * (upBook.BestBid + upBook.BestAsk),
			DownMid:       0.5 * (downBook.BestBid + downBook.BestAsk),
		})
	}

	currentMetrics := dual40.Classify(recentDual40Samples(r.samples, 15), r.cfg)
	if r.mfCollector != nil {
		r.runModelB(res, currentMetrics, upBook, downBook, now) // SHADOW gozlem
	}
	r.advanceTrials(upBook, downBook, currentMetrics, market, now)
	r.evaluateOpeningWindows(upID, downID, upBook, downBook, market, now, elapsed, price)
	return nil
}

func (r *dual40Runtime) rollMarket(slug string, assets []string, now time.Time) {
	// ESKI marketin acilis/kapanis Chainlink boundary fiyatlari (kazanan icin,
	// ikisi de ayni oracle => basissiz). Dolan bacagi olan trial'lar VOID yerine
	// GERCEK sonuca gore settle edilir; boylece tek-bacak riski durustce nete girer.
	var openP, closeP float64
	if r.clClient != nil && r.market != nil {
		if p, ok := r.clClient.BoundaryPrice(r.market.StartTime); ok {
			openP = p
		}
		if p, ok := r.clClient.BoundaryPrice(r.market.EndTime); ok {
			closeP = p
		}
	}
	// FALLBACK: boundary anchor'i roll aninda yakalanmamis olabilir (zamanlama
	// yarisi). Bu durumda ELIMIZDEKI Chainlink ornekleri ile settle et (acilis =
	// ilk ornek, kapanis = son ornek) — ikisi de ayni oracle, basissiz. Boylece
	// tek-bacak trial VOID olmaz, gercek sonuc (kayip/kazanc) nete girer.
	if openP <= 0 && len(r.samples) > 0 {
		openP = r.samples[0].Price
	}
	if openP <= 0 {
		openP = r.priceToBeat // son care: event openPrice
	}
	if closeP <= 0 && len(r.samples) > 0 {
		closeP = r.samples[len(r.samples)-1].Price
	}
	for sec, t := range r.active {
		r.cancelResting(t) // eski marketin acik resting emirlerini iptal et (executor)
		filled := t.UpMakerFilled > 1e-9 || t.DownMakerFilled > 1e-9
		changed := false
		if filled && openP > 0 && closeP > 0 {
			changed = dual40.SettleAtOutcome(t, openP, closeP, now)
		}
		if !changed {
			changed = dual40.CloseForMarketChange(t, now) // pozisyonsuz veya settle fiyati yok
		}
		if changed {
			if err := r.db.UpdateDual40Trial(t); err != nil {
				util.Logger.Warn("Dual40 market-change close failed", zap.Int64("id", t.ID), zap.Error(err))
			}
			util.Logger.Info("DUAL40 ROLL CLOSE", zap.Int64("id", t.ID), zap.String("market", t.MarketSlug), zap.String("state", t.State), zap.Float64("pnl", t.PaperPnL), zap.String("reason", t.Reason))
		}
		delete(r.active, sec)
	}
	r.marketSlug = slug
	r.priceToBeat = 0
	r.samples = nil
	r.evaluated = make(map[int]bool)
	r.marketEntered = false
	r.tradeStream.SetAssets(assets)
	util.Logger.Info("DUAL40 NEW MARKET", zap.String("market", slug))
}

func (r *dual40Runtime) addSample(s dual40.Sample) {
	if len(r.samples) > 0 {
		last := r.samples[len(r.samples)-1]
		if s.ElapsedSec-last.ElapsedSec < 0.50 {
			return
		}
	}
	r.samples = append(r.samples, s)
	if len(r.samples) > 360 {
		r.samples = append([]dual40.Sample(nil), r.samples[len(r.samples)-360:]...)
	}
}

func (r *dual40Runtime) advanceTrials(upBook, downBook polymarket.BookSnapshot, metrics dual40.Metrics, market *polymarket.Market, now time.Time) {
	for sec, t := range r.active {
		changed := false
		gapChanged := r.tradeStream.GapCount() != t.StreamGapCount
		filled := t.UpMakerFilled > 1e-9 || t.DownMakerFilled > 1e-9
		if gapChanged && !filled {
			// Pozisyonsuz trial + stream boslugu -> void (riski gizlemez).
			changed = dual40.InvalidateDataGap(t, now, "TRADE_STREAM_DATA_GAP")
		} else {
			// Stream saglikliysa fill'leri ilerlet. DOLU trial'i gap'te VOID ETME;
			// deadline hedge (kitaptan, stream'den bagimsiz) veya roll-settle ile
			// gercek sonuca gitsin.
			if !gapChanged && r.tradeStream.Healthy(r.tradeStreamMaxAge) {
				trades, latest := r.tradeStream.TradesAfter(t.LastTradeSeq)
				hadFirstFill := t.FirstFillAt != ""
				changed = dual40.Advance(t, upBook, downBook, trades, latest, now, market.EndTime, r.cfg)
				if !hadFirstFill && t.FirstFillAt != "" {
					fillElapsed := now.Sub(market.StartTime.UTC()).Seconds()
					dual40.RecordFirstFillContext(t, metrics, fillElapsed)
					changed = true
				}
			}
			// HEDGE stream saglik/gap durumundan BAGIMSIZ: kitap + REST quote yeter.
			if t.IsOpen() {
				req := dual40.HedgeNeeded(t, metrics, upBook, downBook, now, market.EndTime, r.cfg)
				if req.Needed {
					tokenID := t.UpTokenID
					if req.Side == "DOWN" {
						tokenID = t.DownTokenID
					}
					quote, err := r.pmClient.FetchBuyQuoteForShares(tokenID, req.Shares, r.feeRate, r.latencyBuffer)
					if err != nil {
						util.Logger.Warn("Dual40 hedge quote unavailable", zap.Int64("id", t.ID), zap.String("side", req.Side), zap.Float64("shares", req.Shares), zap.Error(err))
					} else if err := dual40.ApplyHedge(t, req.Side, quote, req.TriggerPrice, req.Reason, now); err != nil {
						util.Logger.Warn("Dual40 hedge apply failed", zap.Int64("id", t.ID), zap.Error(err))
					} else {
						changed = true
						// SEAM 3: dry/live -> hedge edilen tarafi executor'a ver (resting iptal + FAK).
						if r.exec != nil {
							r.hedgeLive(t, req.Side, tokenID, req.Shares, quote.AveragePrice)
						}
						util.Logger.Info("DUAL40 DEADLINE HEDGE", zap.String("mode", r.Status()), zap.Int64("id", t.ID), zap.String("market", t.MarketSlug), zap.String("side", t.HedgeSide), zap.String("hedgeOrderId", t.HedgeOrderID), zap.Float64("avgPrice", t.HedgeAvgPrice), zap.Float64("pnl", t.PaperPnL), zap.String("reason", t.Reason))
					}
				}
			}
		}
		if changed {
			if err := r.db.UpdateDual40Trial(t); err != nil {
				util.Logger.Warn("Dual40 trial update failed", zap.Int64("id", t.ID), zap.Error(err))
			}
		}
		if t.IsTerminal() {
			// SEAM 2: terminal -> acik resting emir(ler)i iptal et (executor).
			r.cancelResting(t)
			util.Logger.Info("DUAL40 TRIAL CLOSED", zap.Int64("id", t.ID), zap.String("market", t.MarketSlug), zap.Int("entrySec", t.EntrySecond), zap.String("state", t.State), zap.Float64("pnl", t.PaperPnL), zap.String("reason", t.Reason))
			delete(r.active, sec)
		}
	}
}

func (r *dual40Runtime) evaluateOpeningWindows(upID, downID string, upBook, downBook polymarket.BookSnapshot, market *polymarket.Market, now time.Time, elapsed, price float64) {
	for _, sec := range r.cfg.EntrySeconds {
		if r.evaluated[sec] || elapsed+0.25 < float64(sec) {
			continue
		}
		r.evaluated[sec] = true
		// TEK GIRIS/MARKET: bu markette zaten bir kutu acildiysa kalan pencereleri
		// atla. Coklu pencere artik "tek kutu icin birden cok SANS" demektir; ayni
		// markete birden fazla kutu yiginlamaz.
		if r.marketEntered {
			continue
		}
		window := dual40.SamplesThrough(r.samples, float64(sec))
		metrics := dual40.Classify(window, r.cfg)
		distUsd := math.Abs(price - r.priceToBeat)

		// Giris kapilari. Veri-saglik onkosullari (pencere/akis/PTB) her modda ayni.
		// Sonra: SimpleEntry => TEK FILTRE (acilis volatil degilse gir); aksi halde
		// eski cok-kapili zincir (mesafe/momentum/ModelB/chop).
		skipReason := ""
		switch {
		case !dual40.OpeningWindowCovered(window, sec):
			skipReason = "YETERSIZ_ACILIS_PENCERESI"
		case !r.tradeStream.Healthy(r.tradeStreamMaxAge):
			skipReason = "AKIS_SAGLIKSIZ"
		case r.priceToBeat <= 0 || price <= 0:
			skipReason = "PTB_BEKLENIYOR" // Chainlink bekleniyor
		case r.cfg.SimpleEntry:
			// TEK FILTRE: acilis penceresinde volatil hareket yoksa gir. Volatilite
			// olcusu = RangeBps (tepe-dip salinim; trend de burada buyur). Ustu = gir­me.
			if metrics.RangeBps > r.cfg.SimpleMaxRangeBps {
				skipReason = fmt.Sprintf("VOLATIL_HAREKET(%.1fbps>%.0fbps)", metrics.RangeBps, r.cfg.SimpleMaxRangeBps)
			}
		default:
			// Eski cok-kapili mod (SimpleEntry=false).
			switch {
			case r.cfg.MaxEntryDistanceUsd > 0 && distUsd > r.cfg.MaxEntryDistanceUsd:
				skipReason = fmt.Sprintf("PTB_UZAK($%.1f>$%.0f)", distUsd, r.cfg.MaxEntryDistanceUsd)
			case r.cfg.MaxEntryMomentumBps > 0 && math.Abs(metrics.DriftBps) > r.cfg.MaxEntryMomentumBps:
				skipReason = fmt.Sprintf("MOMENTUM_VAR(%.1fbps)", math.Abs(metrics.DriftBps))
			case r.modelBGateBlocks() != "":
				skipReason = r.modelBGateBlocks()
			case r.cfg.GateMode == "hard" && !metrics.Eligible:
				skipReason = metrics.Reason
			}
		}

		var trial *dual40.Trial
		if skipReason != "" {
			trial = dual40.NewSkippedTrial("5m", market.Slug, sec, metrics, skipReason, now)
		} else {
			created, err := dual40.NewRestingTrial("5m", market.Slug, sec, metrics, upID, downID, upBook, downBook, r.cfg, now, r.tradeStream.LastSeq(), r.tradeStream.GapCount())
			if err != nil {
				trial = dual40.NewSkippedTrial("5m", market.Slug, sec, metrics, "BOOK_GATE: "+err.Error(), now)
			} else {
				trial = created
				trial.ExecMode = r.execMode
				// SEAM 1: dry/live -> iki bacagi da executor'a ver (post-only GTC).
				if r.exec != nil {
					r.placeBoxLegs(trial, upID, downID)
				}
			}
		}
		if err := r.db.InsertDual40Trial(trial); err != nil {
			util.Logger.Warn("Dual40 trial insert failed", zap.Int("entrySec", sec), zap.Error(err))
			continue
		}
		if trial.IsOpen() {
			r.active[sec] = trial
			r.marketEntered = true // tek giris/market: bundan sonra bu markette yeni kutu yok
			util.Logger.Info("DUAL40 40C/40C POSTED", zap.String("mode", r.Status()), zap.Int64("id", trial.ID), zap.String("market", trial.MarketSlug), zap.Int("entrySec", sec), zap.String("upOrderId", trial.UpOrderID), zap.String("downOrderId", trial.DownOrderID), zap.Float64("driftBps", trial.Metrics.DriftBps), zap.Float64("upQueue", trial.UpQueueAhead), zap.Float64("downQueue", trial.DownQueueAhead))
		} else {
			util.Logger.Info("DUAL40 ENTRY SKIPPED", zap.String("market", trial.MarketSlug), zap.Int("entrySec", sec), zap.String("regime", trial.Regime), zap.Float64("chopScore", trial.Metrics.ChopScore), zap.String("reason", trial.Reason))
		}
	}
}

func (r *dual40Runtime) fetchPairBooks(upID, downID string) (polymarket.BookSnapshot, polymarket.BookSnapshot, error) {
	type result struct {
		book polymarket.BookSnapshot
		err  error
	}
	var wg sync.WaitGroup
	wg.Add(2)
	upCh, downCh := make(chan result, 1), make(chan result, 1)
	go func() { defer wg.Done(); b, e := r.pmClient.FetchBookSnapshot(upID); upCh <- result{b, e} }()
	go func() { defer wg.Done(); b, e := r.pmClient.FetchBookSnapshot(downID); downCh <- result{b, e} }()
	wg.Wait()
	close(upCh)
	close(downCh)
	u, d := <-upCh, <-downCh
	if u.err != nil {
		return polymarket.BookSnapshot{}, polymarket.BookSnapshot{}, u.err
	}
	if d.err != nil {
		return polymarket.BookSnapshot{}, polymarket.BookSnapshot{}, d.err
	}
	return u.book, d.book, nil
}

func dual40FlowImbalance(res *engine.EvaluationResult) float64 {
	if res == nil {
		return 0
	}
	bestSec := math.MaxInt
	flow := res.OrderFlowScore
	for _, w := range res.DeepMicrostructure.Trades {
		if w.Seconds > 0 && w.Seconds < bestSec {
			bestSec = w.Seconds
			flow = w.Imbalance
		}
	}
	if flow > 1 {
		return 1
	}
	if flow < -1 {
		return -1
	}
	return flow
}

func recentDual40Samples(samples []dual40.Sample, n int) []dual40.Sample {
	if n <= 0 || len(samples) <= n {
		return samples
	}
	return samples[len(samples)-n:]
}
