import { cfg, validateConfig } from "./config.js";
import { log } from "./logger.js";
import { PriceFeed } from "./priceFeed.js";
import { Polymarket } from "./polymarket.js";
import { RiskManager } from "./risk.js";
import { Strategy, MarketRef } from "./strategy.js";
import { manualMarket, autoMarket } from "./marketResolver.js";

const TICK_MS = 250; // 4 Hz. Son saniyeler icin yeterli, rate-limit dostu.

async function resolveMarket(): Promise<MarketRef | null> {
  return cfg.marketMode === "manual" ? manualMarket() : await autoMarket();
}

async function main() {
  validateConfig();

  log.info("=".repeat(60));
  log.info(`Basit-Arbitraj scalp botu | DRY_RUN=${cfg.dryRun ? "EVET (paper)" : "HAYIR (CANLI!)"}`);
  log.info(
    `BOX | hedefBacak<=${cfg.targetLegPrice} ciftMaliyet<=${cfg.maxPairCost} ` +
      `yakinlik<=${cfg.proxMaxUsd}USD pay=${cfg.pairShares} deadline=${cfg.completeDeadlineSec}sn abort=${cfg.abortMode}`
  );
  log.info(
    `maxPoz=${cfg.maxPositionUsd}USD gunlukZararLimit=${cfg.maxDailyLossUsd}USD`
  );
  log.info("=".repeat(60));

  const feed = new PriceFeed(cfg.priceSource);
  feed.connect();

  const pm = new Polymarket();
  await pm.init();

  const risk = new RiskManager();
  const strat = new Strategy(feed, pm, risk);

  let market = await resolveMarket();
  if (!market) {
    log.err("Market cozulemedi. .env/manual ayarlarini kontrol et.");
    process.exit(1);
  }
  log.ok(
    `Market: strike=${market.strike} resolve=${new Date(market.resolveTs * 1000).toISOString()}`
  );

  let stopping = false;
  const shutdown = () => {
    stopping = true;
    log.warn("Kapaniyor... acik pozisyon varsa manuel kontrol et.");
    feed.close();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  // Ana dongu
  while (!stopping) {
    if (risk.halted && !strat.hasOpenRisk()) {
      log.err("Kill-switch aktif ve pozisyon yok. Bot duruyor.");
      break;
    }

    const secLeft = market.resolveTs - Date.now() / 1000;

    // Market cozuldu -> bir sonrakine gec (auto) veya bekle/dur (manual)
    if (secLeft < -2 && !strat.hasOpenRisk()) {
      if (cfg.marketMode === "auto") {
        const next = await autoMarket();
        if (next && next.id !== market.id) {
          market = next;
          log.ok(
            `Yeni market: strike=${market.strike} resolve=${new Date(
              market.resolveTs * 1000
            ).toISOString()}`
          );
        }
      } else {
        log.info("Manuel market cozuldu. Yeni market icin .env guncelle ve botu yeniden baslat.");
        break;
      }
    }

    try {
      await strat.tick(market);
    } catch (e) {
      log.err("tick hata:", (e as Error).message);
    }

    await sleep(TICK_MS);
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

main().catch((e) => {
  log.err("Fatal:", e);
  process.exit(1);
});
