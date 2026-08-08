import { ethers } from "ethers";
import { ClobClient, Side, OrderType } from "@polymarket/clob-client";
import { cfg } from "./config.js";
import { log } from "./logger.js";

export interface Book {
  bestBid: number; // en yuksek alis
  bestAsk: number; // en dusuk satis
  bidSize: number;
  askSize: number;
  mid: number;
}

/**
 * Polymarket CLOB sarmalayici. DRY_RUN=true iken gercek emir GONDERMEZ.
 *
 * NOT: @polymarket/clob-client surumune gore method imzalari degisebilir.
 * `init()` icindeki createOrDeriveApiKey ve `postOrder` cagrilarini kendi
 * surumunle dogrula (npm view @polymarket/clob-client version).
 */
export class Polymarket {
  private client!: ClobClient;
  private ready = false;

  async init() {
    if (cfg.dryRun && !cfg.privateKey.startsWith("0x")) {
      // DRY_RUN'da sadece okuma icin anahtarsiz da calisir; ama order book
      // publik oldugundan client'i yine de kurmaya calisiriz.
      log.warn("DRY_RUN: cuzdansiz salt-okunur mod (yalnizca order book).");
      this.client = new ClobClient(cfg.clobHost, cfg.chainId);
      this.ready = true;
      return;
    }

    const wallet = new ethers.Wallet(cfg.privateKey);
    let creds:
      | { key: string; secret: string; passphrase: string }
      | undefined;

    if (cfg.apiKey && cfg.apiSecret && cfg.apiPassphrase) {
      creds = {
        key: cfg.apiKey,
        secret: cfg.apiSecret,
        passphrase: cfg.apiPassphrase,
      };
    } else {
      // Anahtar yoksa private key'den turet (L2 header'lari icin gerekli).
      const bootstrap = new ClobClient(cfg.clobHost, cfg.chainId, wallet);
      const derived = await bootstrap.createOrDeriveApiKey();
      creds = {
        key: (derived as any).key,
        secret: (derived as any).secret,
        passphrase: (derived as any).passphrase,
      };
      log.ok("API anahtari turetildi:", creds.key);
    }

    this.client = new ClobClient(
      cfg.clobHost,
      cfg.chainId,
      wallet,
      creds as any
    );
    this.ready = true;
    log.ok("CLOB client hazir. Adres:", await wallet.getAddress());
  }

  async getBook(tokenId: string): Promise<Book | null> {
    try {
      const ob: any = await this.client.getOrderBook(tokenId);
      const bids = (ob.bids ?? []).map((b: any) => ({
        price: Number(b.price),
        size: Number(b.size),
      }));
      const asks = (ob.asks ?? []).map((a: any) => ({
        price: Number(a.price),
        size: Number(a.size),
      }));
      // Polymarket bids artan, asks... surume gore. Guvenli tarafta sirala.
      bids.sort((a: any, b: any) => b.price - a.price);
      asks.sort((a: any, b: any) => a.price - b.price);
      if (!bids.length || !asks.length) return null;
      const bestBid = bids[0].price;
      const bestAsk = asks[0].price;
      return {
        bestBid,
        bestAsk,
        bidSize: bids[0].size,
        askSize: asks[0].size,
        mid: (bestBid + bestAsk) / 2,
      };
    } catch (e) {
      log.err("getBook hata:", (e as Error).message);
      return null;
    }
  }

  /**
   * Marketable (FAK) emir. side=BUY icin limitPrice = bestAsk + slippage,
   * side=SELL icin bestBid - slippage seklinde cagirilmali.
   * sizeShares: kac adet outcome token (her biri 1 USD'ye resolve olur).
   */
  async placeMarketable(
    tokenId: string,
    side: "BUY" | "SELL",
    sizeShares: number,
    limitPrice: number
  ): Promise<{ ok: boolean; id?: string; filled?: number }> {
    const px = Math.min(0.999, Math.max(0.001, Number(limitPrice.toFixed(3))));
    const sz = Number(sizeShares.toFixed(2));

    if (cfg.dryRun) {
      log.trade(
        `DRY_RUN ${side} ${sz} @ ${px}  (token ${tokenId.slice(0, 10)}…)`
      );
      return { ok: true, id: "dry-" + Date.now(), filled: sz };
    }

    try {
      const order = await this.client.createOrder({
        tokenID: tokenId,
        price: px,
        size: sz,
        side: side === "BUY" ? Side.BUY : Side.SELL,
        feeRateBps: 0,
      } as any);
      const resp: any = await this.client.postOrder(order, OrderType.FAK);
      const ok = resp?.success !== false;
      log.trade(
        `${ok ? "✓" : "✗"} ${side} ${sz} @ ${px} -> ${resp?.orderID ?? resp?.orderId ?? "?"}`
      );
      return { ok, id: resp?.orderID ?? resp?.orderId, filled: sz };
    } catch (e) {
      log.err("placeMarketable hata:", (e as Error).message);
      return { ok: false };
    }
  }

  /**
   * Resting (GTC) limit emir. Maker modu icin. dry-run'da gercek emir gitmez;
   * fill simulasyonunu strateji katmani order book'a gore yapar.
   */
  async placeLimit(
    tokenId: string,
    side: "BUY" | "SELL",
    shares: number,
    price: number
  ): Promise<{ ok: boolean; id?: string }> {
    const px = Math.min(0.999, Math.max(0.001, Number(price.toFixed(3))));
    const sz = Number(shares.toFixed(2));

    if (cfg.dryRun) {
      const id = `dry-lim-${Date.now()}-${Math.floor(Math.random() * 1e4)}`;
      log.trade(`DRY_RUN resting ${side} ${sz} @ ${px} (token ${tokenId.slice(0, 10)}…)`);
      return { ok: true, id };
    }
    try {
      const order = await this.client.createOrder({
        tokenID: tokenId,
        price: px,
        size: sz,
        side: side === "BUY" ? Side.BUY : Side.SELL,
        feeRateBps: 0,
      } as any);
      const resp: any = await this.client.postOrder(order, OrderType.GTC);
      const ok = resp?.success !== false;
      const id = resp?.orderID ?? resp?.orderId;
      log.trade(`${ok ? "✓" : "✗"} resting ${side} ${sz} @ ${px} -> ${id ?? "?"}`);
      return { ok, id };
    } catch (e) {
      log.err("placeLimit hata:", (e as Error).message);
      return { ok: false };
    }
  }

  /** Bir emrin o ana dek dolan hisse miktari (live). dry-run'da 0 doner. */
  async getFilledShares(orderId: string): Promise<number> {
    if (cfg.dryRun || orderId.startsWith("dry-")) return 0;
    try {
      const o: any = await this.client.getOrder(orderId);
      return Number(o?.size_matched ?? o?.sizeMatched ?? 0);
    } catch (e) {
      log.err("getFilledShares hata:", (e as Error).message);
      return 0;
    }
  }

  async cancel(orderId: string): Promise<void> {
    if (cfg.dryRun || orderId.startsWith("dry-")) {
      log.info(`DRY_RUN iptal ${orderId}`);
      return;
    }
    try {
      await this.client.cancelOrder({ orderID: orderId } as any);
    } catch (e) {
      log.err("cancel hata:", (e as Error).message);
    }
  }
}
