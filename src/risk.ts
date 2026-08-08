import { cfg } from "./config.js";
import { log } from "./logger.js";

/**
 * Risk yoneticisi: gunluk zarar kill-switch'i, market basi islem limiti,
 * pozisyon buyuklugu ve zarar-sonrasi cooldown.
 */
export class RiskManager {
  private dailyPnl = 0;
  private tradesThisMarket = 0;
  private lastLossTs = 0;
  private _halted = false;
  private currentMarket = "";

  get halted() {
    return this._halted;
  }
  get pnl() {
    return this.dailyPnl;
  }

  newMarket(id: string) {
    if (id !== this.currentMarket) {
      this.currentMarket = id;
      this.tradesThisMarket = 0;
    }
  }

  /** Yeni bir giris islemine izin var mi? */
  canOpen(desiredUsd: number, openPositionUsd: number): { ok: boolean; reason?: string } {
    if (this._halted) return { ok: false, reason: "HALTED (kill-switch)" };
    if (this.tradesThisMarket >= cfg.maxTradesPerMarket)
      return { ok: false, reason: "market islem limiti" };
    if (Date.now() - this.lastLossTs < cfg.lossCooldownSec * 1000)
      return { ok: false, reason: "zarar cooldown" };
    if (openPositionUsd + desiredUsd > cfg.maxPositionUsd)
      return { ok: false, reason: "max pozisyon" };
    return { ok: true };
  }

  registerTrade() {
    this.tradesThisMarket++;
  }

  /** Kapanan bir islemin realized PnL'ini kaydet (USD). */
  recordPnl(usd: number) {
    this.dailyPnl += usd;
    if (usd < 0) this.lastLossTs = Date.now();
    if (this.dailyPnl <= -cfg.maxDailyLossUsd) {
      this._halted = true;
      log.err(
        `KILL-SWITCH: gunluk zarar ${this.dailyPnl.toFixed(2)} USD, limit ${cfg.maxDailyLossUsd}. Bot durduruluyor.`
      );
    }
  }
}
