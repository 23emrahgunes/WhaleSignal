import { cfg } from "./config.js";
import { PriceFeed } from "./priceFeed.js";
import { Polymarket, Book } from "./polymarket.js";
import { autoMarket, manualMarket, fetchOpenPrice } from "./marketResolver.js";
import type { MarketRef } from "./strategy.js";
import { log } from "./logger.js";

interface Leg {
  id?: string;
  price: number;
  shares: number;
  filled: number;
}

/**
 * Web panelinin arkasindaki durum + emir mantigi.
 * Manuel "box yerlestir" (UP+DOWN limit) dugmesini yonetir, fill'leri takip
 * eder, kilitlenen box'un garanti karini net PnL'e ekler.
 */
export class WebController {
  feed: PriceFeed;
  pm: Polymarket;
  market: MarketRef | null = null;
  pinned = false; // box yerlestirilince market sabitlenir (rollover durur)

  upBook: Book | null = null;
  downBook: Book | null = null;

  up: Leg = { price: 0, shares: 0, filled: 0 };
  down: Leg = { price: 0, shares: 0, filled: 0 };
  upCost = 0;
  downCost = 0;
  settled = false;

  locked: { combined: number; shares: number; profit: number; slug: string; ts: number }[] = [];
  // Tum sonuclar (kar VE zarar): box kilit, abort satis, naked resolution
  history: { slug: string; kind: string; result: string; shares: number; pnl: number; ts: number }[] =
    [];
  realizedPnl = 0;
  status = "başlatılıyor";
  lastError = "";
  strikeSrc = "gamma/fallback"; // strike kaynagi
  // Polymarket event sayfasindan cekilen resmi openPrice (priceToBeat)
  private opWin = 0;
  private opPrice = 0;
  private opLastFetch = 0;

  // --- Otomatik mod ---
  auto = false;
  targetMode: "fixed" | "adaptive" = "fixed";
  autoPrice = cfg.targetLegPrice; // 0.40 (fixed mod)
  autoShares = cfg.pairShares; // 5
  autoProxUsd = 2; // |spot - strike| <= bu (USD) olunca (fixed mod)
  // Emir penceresi: secLeft bu araliktayken box acar [minSec, maxSec]
  autoMinSec = 20; // en gec: bundan az kalinca ACMAZ (fill zamani kalmaz)
  autoMaxSec = 45; // en erken: bundan fazla kalinca bekler (son saniyeleri kolla)
  autoMaxCombined = cfg.maxPairCost; // adaptif modda combined tavani (0.97)
  autoReason = ""; // neden acilmadi (UI icin)

  constructor() {
    this.feed = new PriceFeed(cfg.priceSource);
    this.pm = new Polymarket();
  }

  async start() {
    this.feed.connect();
    await this.pm.init();
    this.loop();
  }

  private async loop() {
    for (;;) {
      try {
        await this.tick();
      } catch (e) {
        this.lastError = (e as Error).message;
        log.err("web tick:", this.lastError);
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  private async tick() {
    // Market: pinned degilse guncel marketi al
    if (!this.pinned || !this.market) {
      const m = cfg.marketMode === "auto" ? await autoMarket() : manualMarket();
      if (m && (!this.market || m.id !== this.market.id)) {
        this.market = m;
        // Yeni market -> openPrice cache'ini sifirla, hemen yeniden cek
        this.opWin = 0;
        this.opPrice = 0;
        this.opLastFetch = 0;
        log.info(`web: yeni market ${m.id}`);
      }
    }
    if (!this.market) {
      this.status = "market bekleniyor";
      return;
    }

    // priceToBeat kaynak onceligi (yalnizca polymarket kaynaginda):
    //  1) Polymarket event sayfasi openPrice (RESMI, birebir) — throttled fetch
    //  2) chainlink tick rekonstruksiyonu (feed) — yedek
    //  3) resolver strike (Gamma) — son yedek
    if (cfg.priceSource === "polymarket") {
      const mm = /(\d{6,})$/.exec(this.market.id);
      const start = mm ? Number(mm[1]) : Math.round(this.market.resolveTs - 300);

      const haveOp = this.opWin === start && this.opPrice > 0;
      const throttle = haveOp ? 5000 : 1200;
      if (Date.now() - this.opLastFetch > throttle) {
        this.opLastFetch = Date.now();
        fetchOpenPrice(start)
          .then((op) => {
            if (op > 0) {
              if (this.opWin !== start) log.ok(`web: openPrice=${op} (win ${start}, Polymarket)`);
              this.opWin = start;
              this.opPrice = op;
            }
          })
          .catch(() => {});
      }

      if (this.opWin === start && this.opPrice > 0) {
        this.market.strike = this.opPrice;
        this.strikeSrc = "polymarket-openPrice";
      } else {
        const p2b = this.feed.priceToBeatFor(start);
        if (p2b > 0) {
          this.market.strike = p2b;
          this.strikeSrc = "chainlink-tick";
        } else {
          this.strikeSrc = "gamma/fallback";
        }
      }
    }

    const secLeft = this.market.resolveTs - Date.now() / 1000;

    const [a, b] = await Promise.all([
      this.pm.getBook(this.market.yesTokenId),
      this.pm.getBook(this.market.noTokenId),
    ]);
    this.upBook = a;
    this.downBook = b;

    if (this.pinned) {
      await this.refresh("UP", a);
      await this.refresh("DOWN", b);
      this.tryLock();

      // Leg-risk: tek bacak doldu, digeri gelmedi -> flatten'da tamamla/iptal
      const matched = Math.min(this.up.filled, this.down.filled);
      const naked = this.up.filled + this.down.filled - 2 * matched;
      if (!this.settled && naked > 0 && secLeft <= cfg.flattenSec) {
        await this.completeOrAbort();
      }

      if (secLeft < -2) {
        this.resolveHeldNaked();
        this.unpinAndReset();
      } else {
        this.status = `box aktif (kalan ${secLeft.toFixed(0)}s)`;
      }
      return;
    }

    // --- OTOMATIK MOD ---
    if (this.auto) {
      if (!this.feed.ready || this.feed.isStale()) {
        this.autoReason = "fiyat bekleniyor";
      } else if (secLeft < this.autoMinSec) {
        this.autoReason = `çok geç (${secLeft.toFixed(0)}s < ${this.autoMinSec}s, açmaz)`;
      } else if (secLeft > this.autoMaxSec) {
        this.autoReason = `henüz erken (${secLeft.toFixed(0)}s > ${this.autoMaxSec}s, ${this.autoMaxSec}s kala girer)`;
      } else if (this.market.strike <= 0) {
        this.autoReason = "priceToBeat bekleniyor (chainlink pencere açılışı)";
      } else if (this.targetMode === "fixed") {
        // FIXED: spot strike'a yakin VE iki bacak da hedefin (0.40) ustunde
        // (yoksa hedef limit ucuz bacagi capraz alir -> naked). Iki ask de
        // 0.40 ustundeyse market ~0.50/0.50 demektir; 0.40 limitler BEKLER.
        const dist = Math.abs(this.feed.price - this.market.strike);
        if (dist > this.autoProxUsd) {
          this.autoReason = `uzak (${dist.toFixed(2)}$ > ${this.autoProxUsd}$)`;
        } else if (!a || !b) {
          this.autoReason = "order book yok";
        } else if (a.bestAsk <= this.autoPrice || b.bestAsk <= this.autoPrice) {
          const lo = a.bestAsk <= this.autoPrice ? "UP" : "DOWN";
          this.autoReason = `${lo} zaten ≤ ${this.autoPrice} (çapraz almam, ikisi de 0.40 üstü olmalı)`;
        } else {
          this.autoReason = "";
          const r = await this.placeBox(this.autoPrice, this.autoShares);
          this.status = "🤖 OTO(fixed) box: " + r.msg;
          return;
        }
      } else {
        // ADAPTIVE: her bacagi best bid'ine oturt, combined <= autoMaxCombined
        if (!a || !b) {
          this.autoReason = "order book yok";
        } else {
          let upPx = a.bestBid;
          let downPx = b.bestBid;
          // Kuyrukta one gecmek icin +1 tick, tavani asmiyorsa
          if (upPx + downPx + 0.02 <= this.autoMaxCombined) {
            upPx += 0.01;
            downPx += 0.01;
          }
          const combined = upPx + downPx;
          if (combined > this.autoMaxCombined) {
            this.autoReason = `bid toplamı yüksek (${combined.toFixed(3)} > ${this.autoMaxCombined})`;
          } else {
            this.autoReason = "";
            const r = await this.placeBoxLegs(upPx, downPx, this.autoShares);
            this.status = "🤖 OTO(adaptif) box: " + r.msg;
            return;
          }
        }
      }
      this.status = `🤖 oto(${this.targetMode}) izliyor — ${this.autoReason} (kalan ${secLeft.toFixed(0)}s)`;
    } else {
      this.status = `izleniyor (kalan ${secLeft.toFixed(0)}s)`;
    }
  }

  /** Tek bacak tamamlanamadi: karli ise taker ile tamamla, degilse cıplak bacagi sat. */
  private async completeOrAbort() {
    if (!this.market) return;
    const matched = Math.min(this.up.filled, this.down.filled);
    const haveUp = this.up.filled - matched > 0; // naked taraf UP mi
    const p1 = haveUp ? this.upAvg : this.downAvg;
    const need = (haveUp ? this.up.filled : this.down.filled) - matched;
    const otherToken = haveUp ? this.market.noTokenId : this.market.yesTokenId;
    const otherBook = haveUp ? this.downBook : this.upBook;
    const otherSide: "UP" | "DOWN" = haveUp ? "DOWN" : "UP";

    // Diger tarafin bekleyen resting emrini iptal et (taker'a gec)
    await this.cancelSide(otherSide);

    const breakeven = 1 - p1;
    if (otherBook && otherBook.bestAsk < breakeven - 0.005) {
      // Hala net kar -> taker ile tamamla
      const px = Math.min(0.99, otherBook.bestAsk + cfg.slippage);
      const res = await this.pm.placeMarketable(otherToken, "BUY", need, px);
      if (res.ok) {
        const filled = res.filled ?? need;
        if (otherSide === "UP") {
          this.up.filled += filled;
          this.upCost += filled * px;
        } else {
          this.down.filled += filled;
          this.downCost += filled * px;
        }
        log.trade(`WEB tamamla ${otherSide} ${filled}@${px.toFixed(3)}`);
        this.tryLock();
      }
    } else {
      // Iptal: cıplak bacagi sat (riski kapat)
      const nakedToken = haveUp ? this.market.yesTokenId : this.market.noTokenId;
      const nakedBook = haveUp ? this.upBook : this.downBook;
      const cost = haveUp ? this.upAvg : this.downAvg;
      if (cfg.abortMode === "sell" && nakedBook) {
        const px = Math.max(0.01, nakedBook.bestBid - cfg.slippage);
        const res = await this.pm.placeMarketable(nakedToken, "SELL", need, px);
        if (res.ok) {
          const sold = res.filled ?? need;
          const realized = (px - cost) * sold;
          this.realizedPnl += realized;
          this.pushHistory(
            `ABORT ${haveUp ? "UP" : "DOWN"}`,
            realized >= 0 ? "kâr ✓" : "zarar ✗",
            sold,
            realized
          );
          // Satilan naked'i pozisyondan dus (resolution'da tekrar sayilmasin)
          if (haveUp) {
            this.up.filled -= sold;
            this.upCost -= cost * sold;
          } else {
            this.down.filled -= sold;
            this.downCost -= cost * sold;
          }
          log.trade(
            `WEB ABORT sat ${haveUp ? "UP" : "DOWN"} ${sold}@${px.toFixed(3)} pnl=${realized.toFixed(3)} (net=${this.realizedPnl.toFixed(3)})`
          );
        }
      } else {
        log.warn("WEB ABORT=hold: cıplak bacak resolution'a tutuluyor (sonuç resolution'da işlenecek)");
      }
      this.settled = true; // bu markette isimiz bitti
    }
  }

  /**
   * Resolution aninda hala tutulan (satilmamis) naked bacak varsa kazanc/kaybi
   * net PnL'e isle. Kazanan taraf: spot > strike ise UP, degilse DOWN (Binance
   * proxy; gercek oracle biraz farkli olabilir).
   */
  private resolveHeldNaked() {
    if (!this.market) {
      this.status = "market çözüldü";
      return;
    }
    const matched = Math.min(this.up.filled, this.down.filled);
    const upNaked = this.up.filled - matched;
    const downNaked = this.down.filled - matched;
    if (upNaked <= 0 && downNaked <= 0) {
      this.status = "market çözüldü — temiz" + (matched > 0 ? " (box kilitliydi)" : "");
      return;
    }
    const upWins = this.feed.price > this.market.strike;
    let msg = "";
    if (upNaked > 0) {
      const won = upWins;
      const realized = ((won ? 1 : 0) - this.upAvg) * upNaked;
      this.realizedPnl += realized;
      this.pushHistory("NAKED UP", won ? "kazandı ✓" : "kaybetti ✗", upNaked, realized);
      msg = `naked UP ${upNaked} ${won ? "KAZANDI" : "kaybetti"} pnl=${realized.toFixed(3)}`;
    }
    if (downNaked > 0) {
      const won = !upWins;
      const realized = ((won ? 1 : 0) - this.downAvg) * downNaked;
      this.realizedPnl += realized;
      this.pushHistory("NAKED DOWN", won ? "kazandı ✓" : "kaybetti ✗", downNaked, realized);
      msg = `naked DOWN ${downNaked} ${won ? "KAZANDI" : "kaybetti"} pnl=${realized.toFixed(3)}`;
    }
    log.warn(`WEB resolution: ${msg} | net=${this.realizedPnl.toFixed(3)}`);
    this.status = `market çözüldü — ${msg}`;
  }

  private pushHistory(kind: string, result: string, shares: number, pnl: number) {
    this.history.push({
      slug: this.market?.id ?? "",
      kind,
      result,
      shares,
      pnl,
      ts: Date.now(),
    });
    if (this.history.length > 100) this.history.shift();
  }

  private async cancelSide(side: "UP" | "DOWN") {
    const leg = side === "UP" ? this.up : this.down;
    if (leg.id) {
      await this.pm.cancel(leg.id);
      leg.id = undefined;
    }
  }

  setAuto(
    on: boolean,
    opts: {
      mode?: "fixed" | "adaptive";
      price?: number;
      shares?: number;
      proxUsd?: number;
      minSec?: number;
      maxSec?: number;
      maxCombined?: number;
    }
  ) {
    this.auto = on;
    if (opts.mode) this.targetMode = opts.mode;
    if (opts.price != null) this.autoPrice = Math.min(0.99, Math.max(0.01, opts.price));
    if (opts.shares != null) this.autoShares = Math.max(1, opts.shares);
    if (opts.proxUsd != null) this.autoProxUsd = Math.max(0.1, opts.proxUsd);
    if (opts.minSec != null) this.autoMinSec = Math.max(cfg.flattenSec + 4, opts.minSec);
    if (opts.maxSec != null) this.autoMaxSec = opts.maxSec;
    if (opts.maxCombined != null)
      this.autoMaxCombined = Math.min(0.999, Math.max(0.5, opts.maxCombined));
    // maxSec her zaman minSec'ten buyuk olsun
    if (this.autoMaxSec < this.autoMinSec + 3) this.autoMaxSec = this.autoMinSec + 3;
  }

  private async refresh(side: "UP" | "DOWN", book: Book | null) {
    const leg = side === "UP" ? this.up : this.down;
    if (!leg.id || !book) return;
    let filledTotal: number;
    if (cfg.dryRun) {
      filledTotal = book.bestAsk <= leg.price ? leg.shares : leg.filled;
    } else {
      filledTotal = await this.pm.getFilledShares(leg.id);
    }
    const delta = filledTotal - leg.filled;
    if (delta > 0) {
      leg.filled = filledTotal;
      // Resting maker limit alis => fill LIMIT fiyatindan olur (0.40).
      // (Ask sonradan daha da dusse bile sen bid'ine 0.40'tan dolarsin.)
      // Limitten daha kotu asla dolmaz => tutucu ve dogru.
      const fillPx = leg.price;
      if (side === "UP") this.upCost += delta * fillPx;
      else this.downCost += delta * fillPx;
      log.trade(`WEB FILL ${side} +${delta}@${fillPx.toFixed(3)}`);
    }
  }

  private get upAvg() {
    return this.up.filled > 0 ? this.upCost / this.up.filled : 0;
  }
  private get downAvg() {
    return this.down.filled > 0 ? this.downCost / this.down.filled : 0;
  }

  private tryLock() {
    if (this.settled) return;
    if (this.up.filled < this.up.shares || this.down.filled < this.down.shares) return;
    const matched = Math.min(this.up.filled, this.down.filled);
    if (matched <= 0) return;
    const combined = this.upAvg + this.downAvg;
    this.settled = true;
    if (combined < 1) {
      const profit = matched * (1 - combined);
      this.realizedPnl += profit;
      this.locked.push({
        combined,
        shares: matched,
        profit,
        slug: this.market?.id ?? "",
        ts: Date.now(),
      });
      this.pushHistory("BOX", "kâr ✓", matched, profit);
      log.ok(`WEB BOX KILIT: combined=${combined.toFixed(3)} kar=${profit.toFixed(3)}`);
    }
  }

  /** UP ve DOWN'a AYNI fiyattan limit koy (manuel / fixed mod). */
  async placeBox(price: number, shares: number): Promise<{ ok: boolean; msg: string }> {
    return this.placeBoxLegs(price, price, shares);
  }

  /** UP ve DOWN'a AYRI fiyatlardan limit koy (adaptif mod best-bid'e oturur). */
  async placeBoxLegs(
    upPrice: number,
    downPrice: number,
    shares: number
  ): Promise<{ ok: boolean; msg: string }> {
    if (!this.market) return { ok: false, msg: "market yok" };
    if (this.pinned) return { ok: false, msg: "zaten aktif box var (önce Reset)" };
    const upPx = Math.min(0.99, Math.max(0.01, Number(upPrice.toFixed(3))));
    const downPx = Math.min(0.99, Math.max(0.01, Number(downPrice.toFixed(3))));
    const sz = Math.max(1, shares);
    if (upPx + downPx >= 1) {
      return { ok: false, msg: `combined ${(upPx + downPx).toFixed(3)} >= 1.00, açılmadı` };
    }

    this.pinned = true;
    this.settled = false;
    this.up = { price: upPx, shares: sz, filled: 0 };
    this.down = { price: downPx, shares: sz, filled: 0 };
    this.upCost = 0;
    this.downCost = 0;

    const [ru, rd] = await Promise.all([
      this.pm.placeLimit(this.market.yesTokenId, "BUY", sz, upPx),
      this.pm.placeLimit(this.market.noTokenId, "BUY", sz, downPx),
    ]);
    this.up.id = ru.id;
    this.down.id = rd.id;

    if (!ru.ok || !rd.ok) {
      return { ok: false, msg: `emir hatası UP:${ru.ok} DOWN:${rd.ok}` };
    }
    return {
      ok: true,
      msg: `UP@${upPx} + DOWN@${downPx} ${sz} share (combined ${(upPx + downPx).toFixed(3)}, ${cfg.dryRun ? "DRY" : "CANLI"})`,
    };
  }

  async cancelAll(): Promise<{ ok: boolean; msg: string }> {
    if (this.up.id) await this.pm.cancel(this.up.id);
    if (this.down.id) await this.pm.cancel(this.down.id);
    return { ok: true, msg: "açık emirler iptal edildi" };
  }

  private unpinAndReset() {
    this.pinned = false;
    this.up = { price: 0, shares: 0, filled: 0 };
    this.down = { price: 0, shares: 0, filled: 0 };
    this.upCost = this.downCost = 0;
    this.settled = false;
  }

  async reset(): Promise<{ ok: boolean; msg: string }> {
    await this.cancelAll();
    this.unpinAndReset();
    return { ok: true, msg: "oturum sıfırlandı" };
  }

  snapshot() {
    const secLeft = this.market ? this.market.resolveTs - Date.now() / 1000 : 0;
    const combined = this.upAvg + this.downAvg;

    // Acik risk: bir bacak dolu digeri degil -> naked mark-to-market
    const matched = Math.min(this.up.filled, this.down.filled);
    const upNaked = this.up.filled - matched;
    const downNaked = this.down.filled - matched;
    let openRisk: any = null;
    if (upNaked > 0) {
      const bid = this.upBook?.bestBid ?? 0;
      openRisk = {
        side: "UP",
        shares: upNaked,
        avg: this.upAvg,
        mark: bid,
        unrealized: (bid - this.upAvg) * upNaked, // simdi satsan
        worst: (0 - this.upAvg) * upNaked, // ters resolve
        best: (1 - this.upAvg) * upNaked, // lehte resolve
      };
    } else if (downNaked > 0) {
      const bid = this.downBook?.bestBid ?? 0;
      openRisk = {
        side: "DOWN",
        shares: downNaked,
        avg: this.downAvg,
        mark: bid,
        unrealized: (bid - this.downAvg) * downNaked,
        worst: (0 - this.downAvg) * downNaked,
        best: (1 - this.downAvg) * downNaked,
      };
    }

    return {
      dryRun: cfg.dryRun,
      legMode: cfg.legMode,
      priceSource: cfg.priceSource,
      status: this.status,
      lastError: this.lastError,
      auto: this.auto,
      targetMode: this.targetMode,
      autoPrice: this.autoPrice,
      autoShares: this.autoShares,
      autoProxUsd: this.autoProxUsd,
      autoMinSec: this.autoMinSec,
      autoMaxSec: this.autoMaxSec,
      autoMaxCombined: this.autoMaxCombined,
      market: this.market
        ? {
            slug: this.market.id,
            strike: this.market.strike,
            strikeSrc: this.strikeSrc,
            resolveTs: this.market.resolveTs,
            secLeft: Math.round(secLeft),
          }
        : null,
      spot: this.feed.price,
      dist:
        this.market && this.feed.price && this.market.strike > 0
          ? this.feed.price - this.market.strike
          : null,
      up: {
        bestBid: this.upBook?.bestBid ?? null,
        bestAsk: this.upBook?.bestAsk ?? null,
        limit: this.up.price || null,
        shares: this.up.shares || null,
        filled: this.up.filled,
      },
      down: {
        bestBid: this.downBook?.bestBid ?? null,
        bestAsk: this.downBook?.bestAsk ?? null,
        limit: this.down.price || null,
        shares: this.down.shares || null,
        filled: this.down.filled,
      },
      pinned: this.pinned,
      combined: this.pinned && this.up.filled && this.down.filled ? combined : null,
      netPnl: this.realizedPnl,
      openRisk,
      totalPnl: this.realizedPnl + (openRisk ? openRisk.unrealized : 0),
      lockedCount: this.locked.length,
      wins: this.history.filter((h) => h.pnl >= 0).length,
      losses: this.history.filter((h) => h.pnl < 0).length,
      history: this.history.slice(-15).reverse(),
    };
  }
}
