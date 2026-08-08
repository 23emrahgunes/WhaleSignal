import { cfg } from "../config.js";
import { log } from "../logger.js";

/**
 * Gamma API'den aktif BTC marketlerini cekip HAM semayi gosterir.
 * auto modun filtre/parse mantigini gercek veriye gore dogrulamak icin kullan.
 *
 *   npm run inspect
 */
const GAMMA = "https://gamma-api.polymarket.com";

async function main() {
  const url = `${GAMMA}/markets?closed=false&active=true&limit=100&order=endDate&ascending=true`;
  const res = await fetch(url);
  const arr: any[] = await res.json();
  const now = Date.now() / 1000;

  const btc = arr.filter((m) => {
    const q = (m.question ?? m.title ?? "").toLowerCase();
    return q.includes("bitcoin") || q.includes("btc");
  });

  log.info(`Toplam market: ${arr.length}, BTC iceren: ${btc.length}`);
  for (const m of btc.slice(0, 15)) {
    const endTs = m.endDate ? Date.parse(m.endDate) / 1000 : 0;
    const mins = endTs ? ((endTs - now) / 60).toFixed(1) : "?";
    console.log("─".repeat(70));
    console.log("question :", m.question ?? m.title);
    console.log("id       :", m.conditionId ?? m.id);
    console.log("endDate  :", m.endDate, `(${mins} dk kaldi)`);
    console.log("outcomes :", m.outcomes);
    console.log("tokens   :", m.clobTokenIds);
    console.log("active   :", m.active, "closed:", m.closed);
  }

  if (cfg.marketMode) {
    // sadece cfg'yi import ettigimizi tekrar dogrulamak icin (lint no-unused)
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
