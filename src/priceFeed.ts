import WebSocket from "ws";
import { log } from "./logger.js";

export type PriceSource = "polymarket" | "coinbase" | "binance";

const URLS: Record<PriceSource, string> = {
  // Polymarket RTDS — 5dk marketlerin RESOLVE oldugu birebir Chainlink BTC/USD
  polymarket: "wss://ws-live-data.polymarket.com",
  coinbase: "wss://ws-feed.exchange.coinbase.com",
  binance: "wss://stream.binance.com:9443/ws/btcusdt@trade",
};

// Polymarket RTDS abonelik mesaji (kardes repo ters-trader'dan birebir)
const RTDS_SUB = JSON.stringify({
  action: "subscribe",
  subscriptions: [
    { topic: "crypto_prices_chainlink", type: "*", filters: '{"symbol":"btc/usd"}' },
  ],
});

/**
 * BTC fiyat akisi. Kaynak:
 *  - polymarket (VARSAYILAN): Polymarket RTDS Chainlink btc/usd — marketin
 *    RESOLVE oldugu birebir fiyat. priceToBeat ile fark birebir tutarli.
 *  - coinbase / binance: proxy borsalar.
 * En son fiyati tutar + son ~N sn getiriden anlik volatilite tahmini uretir.
 */
export class PriceFeed {
  private ws?: WebSocket;
  private _price = 0;
  private lastTs = 0;
  private samples: { t: number; p: number }[] = [];
  private readonly volWindowMs = 20_000;
  private closed = false;
  private url: string;
  private keepalive?: ReturnType<typeof setInterval>;

  constructor(public source: PriceSource = "polymarket") {
    this.url = URLS[source] ?? URLS.polymarket;
  }

  get price() {
    return this._price;
  }
  get ready() {
    return this._price > 0;
  }

  connect() {
    this.ws = new WebSocket(this.url);
    this.ws.on("open", () => {
      log.ok(`${this.source} WS baglandi`);
      if (this.source === "polymarket") {
        this.ws?.send(RTDS_SUB);
        // RTDS keepalive: 5sn'de bir PING (text)
        this.keepalive = setInterval(() => {
          try {
            this.ws?.send("PING");
          } catch {
            /* recv kopmayi gorecek */
          }
        }, 5000);
      } else if (this.source === "coinbase") {
        this.ws?.send(
          JSON.stringify({
            type: "subscribe",
            product_ids: ["BTC-USD"],
            channels: ["ticker"],
          })
        );
      }
    });
    this.ws.on("message", (buf) => this.onMsg(buf));
    this.ws.on("error", (e) => log.err(`${this.source} WS hata:`, (e as Error).message));
    this.ws.on("close", () => {
      if (this.keepalive) clearInterval(this.keepalive);
      if (this.closed) return;
      log.warn(`${this.source} WS kapandi, 1sn sonra yeniden baglaniyor...`);
      setTimeout(() => this.connect(), 1000);
    });
  }

  private onMsg(buf: WebSocket.RawData) {
    const raw = buf.toString();
    if (!raw || (raw[0] !== "{" && raw[0] !== "[")) return; // PONG vb.
    try {
      const m = JSON.parse(raw);
      let p = 0;
      if (this.source === "polymarket") {
        // { payload: { symbol:"btc/usd", value:"...", timestamp:... } }
        const pl = m?.payload;
        if (!pl || typeof pl !== "object") return;
        if (pl.symbol !== "btc/usd") return; // snapshot/diger sembol -> ele
        p = Number(pl.value);
      } else if (this.source === "coinbase") {
        if (m.type !== "ticker") return;
        p = Number(m.price);
      } else {
        p = Number(m.p);
      }
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
    if (this.keepalive) clearInterval(this.keepalive);
    this.ws?.close();
  }
}
