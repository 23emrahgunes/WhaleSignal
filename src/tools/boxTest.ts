// Box stratejisi birim testi (sahte order book). Calistir: npx tsx src/tools/boxTest.ts
import { Strategy, MarketRef } from "../strategy.js";
import { RiskManager } from "../risk.js";
import { cfg } from "../config.js";
import { log } from "../logger.js";

// --- sahte feed ---
class FakeFeed {
  price = 100001; // strike'a 1 USD yakin
  ready = true;
  isStale() {
    return false;
  }
  sigmaOverHorizon() {
    return 10;
  }
}
// --- sahte polymarket ---
class FakePM {
  books: Record<string, any> = {};
  fills: string[] = [];
  setBook(token: string, bestBid: number, bestAsk: number) {
    this.books[token] = { bestBid, bestAsk, bidSize: 1000, askSize: 1000, mid: (bestBid + bestAsk) / 2 };
  }
  async getBook(token: string) {
    return this.books[token] ?? null;
  }
  async placeMarketable(token: string, side: string, shares: number, px: number) {
    this.fills.push(`${side} ${token} ${shares}@${px.toFixed(3)}`);
    return { ok: true, id: "x", filled: shares };
  }
  async placeLimit(token: string, side: string, shares: number, px: number) {
    this.fills.push(`LIMIT ${side} ${token} ${shares}@${px.toFixed(3)}`);
    return { ok: true, id: `lim-${token}-${px}` };
  }
  async getFilledShares() {
    return 0;
  }
  async cancel(id: string) {
    this.fills.push(`CANCEL ${id}`);
  }
}

const M: MarketRef = {
  id: "test",
  yesTokenId: "UP",
  noTokenId: "DOWN",
  strike: 100000,
  resolveTs: 0,
};

async function scenario(name: string, setup: (pm: FakePM) => void, secLeft: number) {
  const feed = new FakeFeed() as any;
  const pm = new FakePM();
  const risk = new RiskManager();
  const strat = new Strategy(feed, pm as any, risk);
  M.resolveTs = Date.now() / 1000 + secLeft;
  setup(pm);
  console.log(`\n=== ${name} (secLeft=${secLeft}) ===`);
  await strat.tick(M);
  return { pm, risk, strat };
}

async function main() {
  cfg.legMode = "taker"; // 1-3 taker senaryolari
  // 1) Iki taraf da hedefte -> aninda box
  await scenario(
    "1: aninda box (UP 0.40 / DOWN 0.40)",
    (pm) => {
      pm.setBook("UP", 0.39, 0.4);
      pm.setBook("DOWN", 0.39, 0.4);
    },
    60
  ).then((r) => {
    console.log("fills:", r.pm.fills);
    console.log("gunlukPnL:", r.risk.pnl.toFixed(3));
  });

  // 2) Bacak-bacak: once UP@0.40, sonra DOWN@0.45 dususte tamamla
  {
    const feed = new FakeFeed() as any;
    const pm = new FakePM();
    const risk = new RiskManager();
    const strat = new Strategy(feed, pm as any, risk);
    M.resolveTs = Date.now() / 1000 + 60;
    console.log("\n=== 2: bacak-bacak tamamla ===");
    pm.setBook("UP", 0.39, 0.4);
    pm.setBook("DOWN", 0.58, 0.6); // A+B=1.0 >0.97, instant degil; DOWN hedef ustu
    await strat.tick(M); // UP alinir
    pm.setBook("DOWN", 0.44, 0.45); // simdi DOWN dustu
    await strat.tick(M); // DOWN alinir -> kilit
    console.log("fills:", pm.fills);
    console.log("gunlukPnL:", risk.pnl.toFixed(3));
  }

  // 3) Leg risk: UP@0.40 dolar, DOWN gelmez, flatten'da abort-sell
  {
    const feed = new FakeFeed() as any;
    const pm = new FakePM();
    const risk = new RiskManager();
    const strat = new Strategy(feed, pm as any, risk);
    console.log("\n=== 3: leg risk -> abort sell ===");
    M.resolveTs = Date.now() / 1000 + 60;
    pm.setBook("UP", 0.39, 0.4);
    pm.setBook("DOWN", 0.68, 0.7);
    await strat.tick(M); // UP alinir
    M.resolveTs = Date.now() / 1000 + 3; // flatten penceresi (<6)
    pm.setBook("UP", 0.4, 0.42); // UP satis icin bid 0.40
    pm.setBook("DOWN", 0.68, 0.7); // DOWN hala pahali (breakeven=0.58, 0.72>0.58)
    await strat.tick(M); // abort -> UP sat
    console.log("fills:", pm.fills);
    console.log("gunlukPnL:", risk.pnl.toFixed(3));
  }

  // 4) MAKER: iki tarafa resting 0.40 bid, volatilite doldurur -> kilit
  {
    cfg.legMode = "maker";
    const feed = new FakeFeed() as any;
    const pm = new FakePM();
    const risk = new RiskManager();
    const strat = new Strategy(feed, pm as any, risk);
    console.log("\n=== 4: MAKER resting 0.40 -> box ===");
    M.resolveTs = Date.now() / 1000 + 60;
    pm.setBook("UP", 0.35, 0.45);
    pm.setBook("DOWN", 0.35, 0.45);
    await strat.tick(M); // iki tarafa resting bid @0.40 koyar
    pm.setBook("UP", 0.4, 0.4); // UP ask 0.40'a duser -> UP bid dolar
    await strat.tick(M);
    pm.setBook("DOWN", 0.4, 0.4); // DOWN ask 0.40'a duser -> DOWN bid dolar -> kilit
    await strat.tick(M);
    console.log("fills:", pm.fills);
    console.log("gunlukPnL:", risk.pnl.toFixed(3));
  }
}

main().catch((e) => log.err(e));
