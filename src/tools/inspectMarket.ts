import { buildSlugCandidates, autoMarket } from "../marketResolver.js";
import { log } from "../logger.js";

/**
 * Aktif 5dk BTC marketini /markets/slug/{slug} ile bulup gosterir.
 * Bulunan yesTokenId/noTokenId/strike/resolveTs degerlerini .env manual moda
 * kopyalayabilirsin. Calistir: npm run inspect
 */
const GAMMA = "https://gamma-api.polymarket.com";

async function main() {
  const slugs = buildSlugCandidates();
  console.log("Aday slug'lar:");
  for (const s of slugs) console.log("  ", s);
  console.log("─".repeat(60));

  for (const slug of slugs) {
    try {
      const r = await fetch(`${GAMMA}/markets/slug/${slug}`, {
        signal: AbortSignal.timeout(4000),
      });
      if (r.status !== 200) continue;
      let p: any = await r.json();
      if (Array.isArray(p)) p = p[0];
      if (!p) continue;
      console.log(`\n✓ ${slug}  (active=${p.active})`);
      console.log("  title  :", p.title || p.question);
      console.log("  tokens :", p.clobTokenIds);
      console.log("  outcome:", p.outcomes);
      console.log("  strike :", p.priceToBeat ?? p.strikePrice ?? p.target ?? "(yok -> Binance fallback)");
      console.log("  endDate:", p.endDate ?? p.endDateIso);
    } catch {
      /* atla */
    }
  }

  console.log("\n" + "═".repeat(60));
  console.log("Resolver'in sectigi CANLI market (.env manual icin):");
  const m = await autoMarket();
  if (m) {
    console.log(`  MARKET_YES_TOKEN_ID=${m.yesTokenId}`);
    console.log(`  MARKET_NO_TOKEN_ID=${m.noTokenId}`);
    console.log(`  MARKET_STRIKE=${m.strike}`);
    console.log(`  MARKET_RESOLVE_TS=${m.resolveTs}`);
  } else {
    log.warn("Canli market bulunamadi (ag/geo engeli veya market yok).");
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
