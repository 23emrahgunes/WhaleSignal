import { cfg } from "./config.js";
import { PriceFeed } from "./priceFeed.js";
import { Polymarket, Book } from "./polymarket.js";
import { autoMarket, manualMarket } from "./marketResolver.js";
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
  realizedPnl = 0;
  status = "başlatılıyor";
  lastError = "";

  // --- Otomatik mod ---
  auto = false;
  autoPrice = cfg.targetLegPrice; // 0.40
  autoShares = cfg.pairShares; // 5
  autoProxUsd = 2; // |spot - strike| <= bu (USD) olunca otomatik box
  autoMinSec = 45; // en az bu kadar sure kalmisken box ac
  autoReason = ""; // neden acilmadi (UI icin)

  constructor() {
    this.feed = new PriceFeed(cfg.binanceWs);
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
      if (m && (!this.market || m.id !== this.market.id)) this.market = m;
    }
    if (!this.market) {
      this.status = "market bekleniyor";
      return;
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
        const m2 = Math.min(this.up.filled, this.down.filled);
        const nk = this.up.filled + this.down.filled - 2 * m2;
        this.status =
          nk > 0
            ? "⚠ market çözüldü — hedge edilmemiş bacak vardı (manuel kontrol)"
            : "market çözüldü — temiz";
        this.unpinAndReset();
      } else {
        this.status = `box aktif (kalan ${secLeft.toFixed(0)}s)`;
      }
      return;
    }

    // --- OTOMATIK MOD: pinned degilken proximity izle, uygunsa box ac ---
    if (this.auto) {
      const dist = Math.abs(this.feed.price - this.market.strike);
      if (!this.feed.ready || this.feed.isStale()) {
        this.autoReason = "fiyat bekleniyor";
      } else if (secLeft < this.autoMinSec) {
        this.autoReason = `kalan süre az (${secLeft.toFixed(0)}s < ${this.autoMinSec}s)`;
      } else if (dist > this.autoProxUsd) {
        this.autoReason = `uzak (${dist.toFixed(2)}$ > ${this.autoProxUsd}$)`;
      } else {
        this.autoReason = "";
        const r = await this.placeBox(this.autoPrice, this.autoShares);
        this.status = "🤖 OTO box açıldı: " + r.msg;
        return;
      }
      this.status = `🤖 oto izliyor — ${this.autoReason} (kalan ${secLeft.toFixed(0)}s)`;
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
          const realized = (px - cost) * need;
          this.realizedPnl += realized;
          log.trade(`WEB ABORT sat ${haveUp ? "UP" : "DOWN"} ${need}@${px.toFixed(3)} pnl=${realized.toFixed(3)}`);
        }
      } else {
        log.warn("WEB ABORT=hold: cıplak bacak resolution'a tutuluyor");
      }
      this.settled = true; // bu markette isimiz bitti
    }
  }

  private async cancelSide(side: "UP" | "DOWN") {
    const leg = side === "UP" ? this.up : this.down;
    if (leg.id) {
      await this.pm.cancel(leg.id);
      leg.id = undefined;
    }
  }

  setAuto(on: boolean, opts: { price?: number; shares?: number; proxUsd?: number; minSec?: number }) {
    this.auto = on;
    if (opts.price != null) this.autoPrice = Math.min(0.99, Math.max(0.01, opts.price));
    if (opts.shares != null) this.autoShares = Math.max(1, opts.shares);
    if (opts.proxUsd != null) this.autoProxUsd = Math.max(0.1, opts.proxUsd);
    if (opts.minSec != null) this.autoMinSec = Math.max(10, opts.minSec);
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
      if (side === "UP") this.upCost += delta * leg.price;
      else this.downCost += delta * leg.price;
      log.trade(`WEB FILL ${side} +${delta}@${leg.price.toFixed(3)}`);
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
      log.ok(`WEB BOX KILIT: combined=${combined.toFixed(3)} kar=${profit.toFixed(3)}`);
    }
  }

  /** UP ve DOWN'a ayni anda limit emir koy (tek seferde). */
  async placeBox(price: number, shares: number): Promise<{ ok: boolean; msg: string }> {
    if (!this.market) return { ok: false, msg: "market yok" };
    if (this.pinned) return { ok: false, msg: "zaten aktif box var (önce Reset)" };
    const px = Math.min(0.99, Math.max(0.01, price));
    const sz = Math.max(1, shares);

    this.pinned = true;
    this.settled = false;
    this.up = { price: px, shares: sz, filled: 0 };
    this.down = { price: px, shares: sz, filled: 0 };
    this.upCost = 0;
    this.downCost = 0;

    const [ru, rd] = await Promise.all([
      this.pm.placeLimit(this.market.yesTokenId, "BUY", sz, px),
      this.pm.placeLimit(this.market.noTokenId, "BUY", sz, px),
    ]);
    this.up.id = ru.id;
    this.down.id = rd.id;

    if (!ru.ok || !rd.ok) {
      return { ok: false, msg: `emir hatası UP:${ru.ok} DOWN:${rd.ok}` };
    }
    return {
      ok: true,
      msg: `Box yerleştirildi: UP+DOWN ${sz} share @ ${px} (${cfg.dryRun ? "DRY" : "CANLI"})`,
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
    return {
      dryRun: cfg.dryRun,
      legMode: cfg.legMode,
      status: this.status,
      lastError: this.lastError,
      auto: this.auto,
      autoPrice: this.autoPrice,
      autoShares: this.autoShares,
      autoProxUsd: this.autoProxUsd,
      autoMinSec: this.autoMinSec,
      market: this.market
        ? {
            slug: this.market.id,
            strike: this.market.strike,
            resolveTs: this.market.resolveTs,
            secLeft: Math.round(secLeft),
          }
        : null,
      spot: this.feed.price,
      dist: this.market && this.feed.price ? this.feed.price - this.market.strike : null,
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
      lockedCount: this.locked.length,
      locked: this.locked.slice(-10).reverse(),
    };
  }
}
