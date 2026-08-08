import WebSocket from "ws";
import { log } from "./logger.js";

/**
 * Binance BTCUSDT trade akisi. En son spot fiyati tutar ve son ~N saniyelik
 * getiriden anlik volatilite (saniye basi std) tahmini uretir.
 *
 * UYARI: Bu yalnizca bir PROXY fiyat kaynagidir. Polymarket marketinin
 * gercek resolution kaynagi (Pyth/Chainlink/başka) farkli olabilir; strike'a
 * cok yakin son saniyelerde kucuk farklar sonucu tersine cevirebilir.
 * Canliya gecmeden resolution kaynagini DOGRULA.
 */
export class PriceFeed {
  private ws?: WebSocket;
  private _price = 0;
  private lastTs = 0;
  private samples: { t: number; p: number }[] = [];
  private readonly volWindowMs = 20_000;
  private closed = false;

  constructor(private url: string) {}

  get price() {
    return this._price;
  }
  get ready() {
    return this._price > 0;
  }

  connect() {
    this.ws = new WebSocket(this.url);
    this.ws.on("open", () => log.ok("Binance WS baglandi"));
    this.ws.on("message", (buf) => this.onMsg(buf));
    this.ws.on("error", (e) => log.err("Binance WS hata:", (e as Error).message));
    this.ws.on("close", () => {
      if (this.closed) return;
      log.warn("Binance WS kapandi, 1sn sonra yeniden baglaniyor...");
      setTimeout(() => this.connect(), 1000);
    });
  }

  private onMsg(buf: WebSocket.RawData) {
    try {
      const m = JSON.parse(buf.toString());
      // @btcusdt@trade: { p: "fiyat", ... }
      const p = Number(m.p);
      if (!p || Number.isNaN(p)) return;
      this._price = p;
      const now = Date.now();
      this.lastTs = now;
      this.samples.push({ t: now, p });
      const cutoff = now - this.volWindowMs;
      while (this.samples.length && this.samples[0].t < cutoff) this.samples.shift();
    } catch {
      /* yut */
    }
  }

  /** Fiyatin bayat olup olmadigini kontrol et (ms). */
  isStale(maxAgeMs = 3000) {
    return Date.now() - this.lastTs > maxAgeMs;
  }

  /**
   * Kalan `secondsLeft` saniyede BTC fiyatinin tahmini standart sapmasi (USD).
   * Son penceredeki saniye-basi log-getiri std'sini alir, sqrt(t) ile olcekler.
   */
  sigmaOverHorizon(secondsLeft: number): number {
    if (this.samples.length < 5) {
      // yetersiz veri: kaba varsayilan ~ %0.03/dk -> cok kaba
      return this._price * 0.0004 * Math.sqrt(Math.max(secondsLeft, 1));
    }
    // saniyelik bucket'lara indir
    const rets: number[] = [];
    for (let i = 1; i < this.samples.length; i++) {
      const a = this.samples[i - 1].p;
      const b = this.samples[i].p;
      if (a > 0) rets.push(Math.log(b / a));
    }
    if (rets.length < 2) return this._price * 0.0004 * Math.sqrt(Math.max(secondsLeft, 1));
    const mean = rets.reduce((s, r) => s + r, 0) / rets.length;
    const varr = rets.reduce((s, r) => s + (r - mean) ** 2, 0) / (rets.length - 1);
    const stdPerTick = Math.sqrt(varr);
    // ortalama tick araligi (saniye)
    const spanSec =
      (this.samples[this.samples.length - 1].t - this.samples[0].t) / 1000 || 1;
    const ticksPerSec = this.samples.length / spanSec;
    const stdPerSec = stdPerTick * Math.sqrt(Math.max(ticksPerSec, 0.001));
    const sigmaFrac = stdPerSec * Math.sqrt(Math.max(secondsLeft, 0.001));
    return this._price * sigmaFrac;
  }

  close() {
    this.closed = true;
    this.ws?.close();
  }
}
