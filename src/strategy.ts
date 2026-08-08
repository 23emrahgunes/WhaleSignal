import { cfg } from "./config.js";
import { log } from "./logger.js";
import { PriceFeed } from "./priceFeed.js";
import { Polymarket } from "./polymarket.js";
import { RiskManager } from "./risk.js";

export interface MarketRef {
  id: string;
  yesTokenId: string; // "Up" / "Yes" tarafi (A)
  noTokenId: string; // "Down" / "No" tarafi (B)
  strike: number;
  resolveTs: number; // saniye (UNIX)
}

/**
 * BOX / cift-bacak yakalama stratejisi.
 *
 * Fikir: Up ve Down bacaklarinin HER IKISINI de ucuza al; toplam maliyet
 * < $1.00 olsun. Resolution'da mutlaka bir taraf $1 oder => yonden BAGIMSIZ
 * garanti kar. Volatilite, her bacagi ayri ayri (~0.40) doldurmak icindir.
 * En iyi bolge: fiyatin priceToBeat'e yakin aktigi an (iki taraf da ~0.50,
 * oynaklik her ikisini sirayla hedefe dusurur).
 *
 * Ana risk = LEG RISK: bir bacagi doldurdun, digeri gelmezse cıplak (naked)
 * yonlu pozisyonda kalirsin. Bunu `completeDeadline`/`flatten` + abort mantigi
 * yonetir. Iki bacak kilitlendiginde resolution'a TUTMAK dogrudur (garanti $1).
 */
export class Strategy {
  private upShares = 0;
  private upCost = 0; // USD
  private downShares = 0;
  private downCost = 0; // USD
  private locked = false;
  private done = false; // bu markette isimiz bitti (kilitlendi veya iptal)
  private marketId = "";
  // maker modu: taraf basina aktif resting emir
  private orders: {
    UP?: { id: string; price: number; shares: number; filled: number };
    DOWN?: { id: string; price: number; shares: number; filled: number };
  } = {};

  constructor(
    private feed: PriceFeed,
    private pm: Polymarket,
    private risk: RiskManager
  ) {}

  private reset(id: string) {
    this.marketId = id;
    this.upShares = this.upCost = this.downShares = this.downCost = 0;
    this.locked = false;
    this.done = false;
    this.orders = {};
  }

  private get upAvg() {
    return this.upShares > 0 ? this.upCost / this.upShares : 0;
  }
  private get downAvg() {
    return this.downShares > 0 ? this.downCost / this.downShares : 0;
  }

  /** Hedge edilmemis (naked) bacak var mi? Rollover guvenligi icin. */
  hasOpenRisk() {
    const matched = Math.min(this.upShares, this.downShares);
    return this.upShares - matched > 0 || this.downShares - matched > 0;
  }

  async tick(m: MarketRef) {
    if (m.id !== this.marketId) this.reset(m.id);
    this.risk.newMarket(m.id);
    if (this.done || this.locked) return;
    if (!this.feed.ready || this.feed.isStale()) return;
    if (cfg.legMode === "maker") return this.makerTick(m);
    return this.takerTick(m);
  }

  // ======================= TAKER MODU =======================
  private async takerTick(m: MarketRef) {
    const secLeft = m.resolveTs - Date.now() / 1000;
    const prox = Math.abs(this.feed.price - m.strike);

    const matched = Math.min(this.upShares, this.downShares);
    const upNaked = this.upShares - matched;
    const downNaked = this.downShares - matched;
    const oneLeg = upNaked > 0 || downNaked > 0;

    // Order book'lari oku
    const [bookA, bookB] = await Promise.all([
      this.pm.getBook(m.yesTokenId),
      this.pm.getBook(m.noTokenId),
    ]);
    if (!bookA || !bookB) return;

    // ---------------- FAZ: ILK BACAK (flat) ----------------
    if (!oneLeg) {
      if (this.risk.halted) return;
      if (secLeft <= cfg.completeDeadlineSec) return; // gec kaldik, yeni bacak acma
      if (prox > cfg.proxMaxUsd) return; // strike'a yeterince yakin degil

      // Her iki tarafi da AYNI ANDA hedefte bulduysak -> aninda box
      if (bookA.bestAsk + bookB.bestAsk <= cfg.maxPairCost) {
        await this.buy("UP", m.yesTokenId, bookA);
        await this.buy("DOWN", m.noTokenId, bookB);
        this.tryLock();
        return;
      }
      // Yoksa: ucuz olan tek bacagi hedeften yakala
      if (bookA.bestAsk <= cfg.targetLegPrice && bookA.bestAsk <= bookB.bestAsk) {
        await this.buy("UP", m.yesTokenId, bookA);
      } else if (bookB.bestAsk <= cfg.targetLegPrice) {
        await this.buy("DOWN", m.noTokenId, bookB);
      }
      return;
    }

    // ---------------- FAZ: TEK BACAK (tamamla / iptal) ----------------
    const haveUp = upNaked > 0;
    const p1 = haveUp ? this.upAvg : this.downAvg;
    const otherBook = haveUp ? bookB : bookA;
    const otherToken = haveUp ? m.noTokenId : m.yesTokenId;
    const otherSide: "UP" | "DOWN" = haveUp ? "DOWN" : "UP";
    const need = haveUp ? upNaked : downNaked;

    const ceilingLock = cfg.maxPairCost - p1; // marjli tamamla fiyati
    const breakeven = 1 - p1; // bunun altinda her fiyat = net kar

    if (otherBook.bestAsk <= ceilingLock) {
      // Karli tamamla -> kilit
      await this.buy(otherSide, otherToken, otherBook, need);
      this.tryLock();
      return;
    }

    if (secLeft <= cfg.flattenSec) {
      if (otherBook.bestAsk < breakeven - 0.005) {
        // Marj yok ama hala net kar -> yine de tamamla
        log.warn(
          `marj daraltti, breakeven-alti tamamlaniyor: ask=${otherBook.bestAsk.toFixed(3)} < ${breakeven.toFixed(3)}`
        );
        await this.buy(otherSide, otherToken, otherBook, need);
        this.tryLock();
      } else {
        await this.abort(m, haveUp, bookA, bookB);
      }
    }
    // aksi halde: bekle, diger bacagin dusmesini umut et
  }

  // ======================= MAKER MODU =======================
  // Iki tarafa da resting bid koy (hedef ~0.40). Volatilite doldursun.
  // Biri dolunca digeri zaten hedefte beklerken kilit otomatik olur.
  private async makerTick(m: MarketRef) {
    const secLeft = m.resolveTs - Date.now() / 1000;
    const prox = Math.abs(this.feed.price - m.strike);

    const [bookA, bookB] = await Promise.all([
      this.pm.getBook(m.yesTokenId),
      this.pm.getBook(m.noTokenId),
    ]);
    if (!bookA || !bookB) return;

    // 1) Resting emirlerin fill'lerini guncelle
    await this.refreshFill("UP", bookA);
    await this.refreshFill("DOWN", bookB);

    // 2) Iki taraf da hedefe ulastiysa kilitle
    if (this.upShares >= cfg.pairShares && this.downShares >= cfg.pairShares) {
      await this.cancelIfAny("UP");
      await this.cancelIfAny("DOWN");
      this.tryLock();
      return;
    }
    if (this.risk.halted) return;

    const oneLegFilled =
      (this.upShares > 0) !== (this.downShares > 0); // tam olarak biri dolu
    const openWindow = prox <= cfg.proxMaxUsd && secLeft > cfg.completeDeadlineSec;

    // 3) TEK BACAK DOLU: son anda tamamla / iptal
    if (oneLegFilled && secLeft <= cfg.flattenSec) {
      const haveUp = this.upShares > 0;
      const p1 = haveUp ? this.upAvg : this.downAvg;
      const otherSide: "UP" | "DOWN" = haveUp ? "DOWN" : "UP";
      const otherToken = haveUp ? m.noTokenId : m.yesTokenId;
      const otherBook = haveUp ? bookB : bookA;
      const need = cfg.pairShares - (haveUp ? this.downShares : this.upShares);
      const breakeven = 1 - p1;
      await this.cancelIfAny(otherSide); // resting'i cek, taker'a gec
      if (otherBook.bestAsk < breakeven - 0.005) {
        await this.buy(otherSide, otherToken, otherBook, need); // cross ile tamamla
        this.tryLock();
      } else {
        await this.abort(m, haveUp, bookA, bookB);
      }
      return;
    }

    // 4) Deadline gecti ve hic dolum yoksa: emirleri iptal et, bu markette dur
    if (secLeft <= cfg.completeDeadlineSec && this.upShares === 0 && this.downShares === 0) {
      await this.cancelIfAny("UP");
      await this.cancelIfAny("DOWN");
      this.done = true;
      return;
    }

    // 5) Gerekli taraflarda resting bid'i olustur/guncelle
    if (openWindow || oneLegFilled) {
      await this.ensureRestingBid("UP", m.yesTokenId, bookA);
      await this.ensureRestingBid("DOWN", m.noTokenId, bookB);
    }
  }

  /** Resting emrin dolan miktarini guncelle (live: getOrder, dry: book sim). */
  private async refreshFill(side: "UP" | "DOWN", book: { bestAsk: number }) {
    const ord = this.orders[side];
    if (!ord) return;
    let filledTotal: number;
    if (cfg.dryRun) {
      // Paper sim: resting BUY @ P, ask <= P olunca dolar say.
      filledTotal = book.bestAsk <= ord.price ? ord.shares : ord.filled;
    } else {
      filledTotal = await this.pm.getFilledShares(ord.id);
    }
    const delta = filledTotal - ord.filled;
    if (delta > 0) {
      ord.filled = filledTotal;
      if (side === "UP") {
        this.upShares += delta;
        this.upCost += delta * ord.price;
      } else {
        this.downShares += delta;
        this.downCost += delta * ord.price;
      }
      this.risk.registerTrade();
      log.trade(
        `FILL ${side} +${delta}@${ord.price.toFixed(3)} | UP=${this.upShares}@${this.upAvg.toFixed(3)} DOWN=${this.downShares}@${this.downAvg.toFixed(3)}`
      );
      if (ord.filled >= ord.shares) this.orders[side] = undefined; // tamam
    }
  }

  /** Bir tarafta istenen fiyata resting bid oldugundan emin ol (gerekiyorsa reprice). */
  private async ensureRestingBid(
    side: "UP" | "DOWN",
    token: string,
    book: { bestBid: number; bestAsk: number }
  ) {
    const already = side === "UP" ? this.upShares : this.downShares;
    const remaining = cfg.pairShares - already;
    if (remaining <= 0) {
      await this.cancelIfAny(side);
      return;
    }

    // Diger bacak doluysa tavan = maxPairCost - p1, degilse hedef fiyat.
    const otherFilled = side === "UP" ? this.downShares : this.upShares;
    const p1 = side === "UP" ? this.downAvg : this.upAvg;
    const ceiling = otherFilled > 0 ? cfg.maxPairCost - p1 : cfg.targetLegPrice;
    // En iyi bid'i +1 tick gecerek one gec ama tavani asma.
    const desired = Math.min(ceiling, Math.max(0.01, book.bestBid + 0.01, cfg.targetLegPrice));
    const px = Number(Math.min(desired, ceiling).toFixed(3));

    const ord = this.orders[side];
    if (ord && Math.abs(ord.price - px) < 0.01) return; // zaten yakin fiyatta

    // Risk kontrolu
    const notional = remaining * px;
    const gate = this.risk.canOpen(notional, this.upCost + this.downCost);
    if (!gate.ok) return;

    if (ord) await this.cancelIfAny(side); // reprice: eskiyi iptal et
    const res = await this.pm.placeLimit(token, "BUY", remaining, px);
    if (res.ok && res.id) {
      this.orders[side] = { id: res.id, price: px, shares: remaining, filled: 0 };
    }
  }

  private async cancelIfAny(side: "UP" | "DOWN") {
    const ord = this.orders[side];
    if (ord) {
      await this.pm.cancel(ord.id);
      this.orders[side] = undefined;
    }
  }

  /** Bir bacagi marketable (FAK) al. */
  private async buy(
    side: "UP" | "DOWN",
    token: string,
    book: { bestAsk: number; askSize: number },
    sharesOverride?: number
  ) {
    const shares = sharesOverride ?? cfg.pairShares;
    const px = Math.min(0.99, book.bestAsk + cfg.slippage);
    const notional = shares * px;

    const openUsd = this.upCost + this.downCost;
    const gate = this.risk.canOpen(notional, openUsd);
    if (!gate.ok) {
      log.info(`alim atlandi (${gate.reason})`);
      return;
    }
    if (book.askSize < shares * 0.5) {
      log.info(`${side}: yetersiz likidite (ask ${book.askSize} < ${shares})`);
      return;
    }

    const res = await this.pm.placeMarketable(token, "BUY", shares, px);
    if (!res.ok) return;
    const filled = res.filled ?? shares;
    this.risk.registerTrade();
    if (side === "UP") {
      this.upShares += filled;
      this.upCost += filled * px;
    } else {
      this.downShares += filled;
      this.downCost += filled * px;
    }
    log.trade(
      `AL ${side} ${filled}@${px.toFixed(3)} | UP=${this.upShares}@${this.upAvg.toFixed(3)} DOWN=${this.downShares}@${this.downAvg.toFixed(3)}`
    );
  }

  /** Eslesen bacaklar varsa kilidi dogrula ve karı kaydet. */
  private tryLock() {
    const matched = Math.min(this.upShares, this.downShares);
    if (matched <= 0) return;
    const combined = this.upAvg + this.downAvg;
    if (combined < 1) {
      const profit = matched * (1 - combined);
      this.risk.recordPnl(profit); // garanti -> simdi kaydet
      this.locked = true;
      this.done = true;
      log.ok(
        `🔒 BOX KILITLENDI | ciftMaliyet=${combined.toFixed(3)} pay=${matched} ` +
          `GARANTI KAR=${profit.toFixed(3)} USD (resolution'a kadar tutulacak)`
      );
    } else {
      log.warn(
        `Cift maliyet ${combined.toFixed(3)} >= 1.00 — kar yok! (istenmeyen durum)`
      );
      this.done = true;
    }
  }

  /** Tek bacak tamamlanamadi: riski kapat. */
  private async abort(
    m: MarketRef,
    haveUp: boolean,
    bookA: { bestBid: number },
    bookB: { bestBid: number }
  ) {
    this.done = true;
    if (cfg.abortMode === "hold") {
      log.warn(
        "ABORT=hold: tek bacak resolution'a TUTULUYOR (50/50 risk). Manuel izle."
      );
      return;
    }
    // sell: cıplak bacagi sat
    const token = haveUp ? m.yesTokenId : m.noTokenId;
    const shares = haveUp
      ? this.upShares - Math.min(this.upShares, this.downShares)
      : this.downShares - Math.min(this.upShares, this.downShares);
    const bid = haveUp ? bookA.bestBid : bookB.bestBid;
    const px = Math.max(0.01, bid - cfg.slippage);
    const cost = haveUp ? this.upAvg : this.downAvg;
    const res = await this.pm.placeMarketable(token, "SELL", shares, px);
    if (res.ok) {
      const realized = (px - cost) * shares;
      this.risk.recordPnl(realized);
      log.trade(
        `ABORT sat ${haveUp ? "UP" : "DOWN"} ${shares}@${px.toFixed(3)} | pnl=${realized.toFixed(3)} USD`
      );
    } else {
      log.err("ABORT satis basarisiz — cıplak pozisyon kaldi, manuel kontrol et!");
    }
  }
}
