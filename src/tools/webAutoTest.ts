// Otomatik mod testi: npx tsx src/tools/webAutoTest.ts
import { cfg } from "../config.js";

// manual market ayarla (autoMarket ag cagrisini atlamak icin)
cfg.marketMode = "manual";
cfg.yesTokenId = "UP";
cfg.noTokenId = "DOWN";
cfg.strike = 100000;
cfg.resolveTs = Math.floor(Date.now() / 1000) + 120;
cfg.dryRun = true;

const { WebController } = await import("../webController.js");

class FakeFeed {
  price = 100001; // strike'a 1$ yakin
  ready = true;
  isStale() {
    return false;
  }
  connect() {}
  sigmaOverHorizon() {
    return 10;
  }
}
class FakePM {
  books: Record<string, any> = {};
  fills: string[] = [];
  setBook(t: string, bid: number, ask: number) {
    this.books[t] = { bestBid: bid, bestAsk: ask, bidSize: 1e3, askSize: 1e3, mid: (bid + ask) / 2 };
  }
  async init() {}
  async getBook(t: string) {
    return this.books[t] ?? null;
  }
  async placeLimit(t: string, s: string, sh: number, px: number) {
    this.fills.push(`LIMIT ${s} ${t} ${sh}@${px.toFixed(2)}`);
    return { ok: true, id: `lim-${t}` };
  }
  async placeMarketable(t: string, s: string, sh: number, px: number) {
    this.fills.push(`MKT ${s} ${t} ${sh}@${px.toFixed(2)}`);
    return { ok: true, id: "m", filled: sh };
  }
  async getFilledShares() {
    return 0;
  }
  async cancel(id: string) {
    this.fills.push(`CANCEL ${id}`);
  }
}

const ctrl: any = new WebController();
ctrl.feed = new FakeFeed();
ctrl.pm = new FakePM();
const pm: FakePM = ctrl.pm;

ctrl.setAuto(true, { price: 0.4, shares: 5, proxUsd: 2, minSec: 45 });

async function run() {
  console.log("=== OTOMATIK MOD testi ===");
  // 1) Iki taraf ~0.50, spot strike'a 1$ yakin -> oto box acmali
  pm.setBook("UP", 0.49, 0.51);
  pm.setBook("DOWN", 0.49, 0.51);
  await ctrl.tick();
  console.log("tick1:", ctrl.status);
  console.log("  pinned:", ctrl.pinned, "| emirler:", pm.fills.slice());

  // 2) UP ask 0.40'a duser -> UP dolar
  pm.setBook("UP", 0.39, 0.4);
  await ctrl.tick();
  console.log("tick2 (UP düştü):", "UP filled =", ctrl.up.filled);

  // 3) DOWN ask 0.40'a duser -> DOWN dolar -> KILIT
  pm.setBook("DOWN", 0.39, 0.4);
  await ctrl.tick();
  const snap = ctrl.snapshot();
  console.log("tick3 (DOWN düştü):", "DOWN filled =", ctrl.down.filled);
  console.log("  NET PnL =", snap.netPnl.toFixed(3), "| kilitli box =", snap.lockedCount);
  console.log("  combined =", snap.combined);

  // --- ADAPTIVE mod: karar verilmiş market (0.15/0.84), best-bid'e otur ---
  console.log("\n=== ADAPTIVE mod testi (bidSum 0.99) ===");
  const c2: any = new WebController();
  c2.feed = new FakeFeed();
  c2.pm = new FakePM();
  const pm2: FakePM = c2.pm;
  c2.setAuto(true, { mode: "adaptive", shares: 5, maxCombined: 0.99, minSec: 45 });
  pm2.setBook("UP", 0.15, 0.16);
  pm2.setBook("DOWN", 0.84, 0.85);
  await c2.tick();
  console.log("tick1:", c2.status);
  console.log("  emirler:", pm2.fills.slice());
  console.log("  UP@", c2.up.price, "DOWN@", c2.down.price, "combined =", (c2.up.price + c2.down.price).toFixed(3));
}

run().catch((e) => console.error(e));
