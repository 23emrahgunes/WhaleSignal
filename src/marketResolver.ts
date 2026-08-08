import { cfg } from "./config.js";
import { log } from "./logger.js";
import type { MarketRef } from "./strategy.js";

const GAMMA = "https://gamma-api.polymarket.com";

/** manual modda .env'den market referansini dondurur. */
export function manualMarket(): MarketRef {
  return {
    id: `${cfg.yesTokenId.slice(0, 8)}-manual`,
    yesTokenId: cfg.yesTokenId,
    noTokenId: cfg.noTokenId,
    strike: cfg.strike,
    resolveTs: cfg.resolveTs,
  };
}

/**
 * auto modda Gamma API'den aktif 5dk BTC "up/down" marketini bulmaya calisir.
 *
 * !!! KIRILGAN !!! Gamma yanit semasi ve market slug/etiketleri zamanla degisir.
 * Canliya gecmeden `npm run inspect` ile gercek yaniti incele ve buradaki
 * filtreleri/parse mantigini dogrula. Strike parse'i ozellikle riskli.
 */
export async function autoMarket(): Promise<MarketRef | null> {
  try {
    // Aktif, yakinda cozulecek BTC marketlerini cek.
    const url =
      `${GAMMA}/markets?closed=false&active=true&limit=100` +
      `&order=endDate&ascending=true`;
    const res = await fetch(url);
    if (!res.ok) {
      log.err("Gamma istegi basarisiz:", res.status);
      return null;
    }
    const arr: any[] = await res.json();
    const now = Date.now() / 1000;

    const candidates = arr
      .filter((m) => {
        const q = (m.question ?? m.title ?? "").toLowerCase();
        const isBtc = q.includes("bitcoin") || q.includes("btc");
        const isUpDown =
          q.includes("up or down") ||
          q.includes(" above ") ||
          q.includes("higher") ||
          q.includes("$");
        return isBtc && isUpDown;
      })
      .map((m) => {
        const endTs = m.endDate ? Date.parse(m.endDate) / 1000 : 0;
        return { m, endTs };
      })
      .filter((c) => c.endTs > now && c.endTs - now < 15 * 60) // <15dk kalan
      .sort((a, b) => a.endTs - b.endTs);

    if (!candidates.length) {
      log.warn("auto: uygun BTC 5dk market bulunamadi.");
      return null;
    }

    const { m, endTs } = candidates[0];
    // clobTokenIds genellikle JSON string dizisi: "[\"yesId\",\"noId\"]"
    let tokens: string[] = [];
    try {
      tokens =
        typeof m.clobTokenIds === "string"
          ? JSON.parse(m.clobTokenIds)
          : m.clobTokenIds ?? [];
    } catch {
      /* yut */
    }
    let outcomes: string[] = [];
    try {
      outcomes =
        typeof m.outcomes === "string" ? JSON.parse(m.outcomes) : m.outcomes ?? [];
    } catch {
      /* yut */
    }
    if (tokens.length < 2) {
      log.err("auto: clobTokenIds cozulemedi:", m.clobTokenIds);
      return null;
    }
    // YES/NO eslemesi: outcomes ["Yes","No"] veya ["Up","Down"] sirasina gore.
    const yesIdx = outcomes.findIndex((o) =>
      ["yes", "up", "above", "higher"].includes(String(o).toLowerCase())
    );
    const yi = yesIdx >= 0 ? yesIdx : 0;
    const ni = yi === 0 ? 1 : 0;

    // Strike parse: sorudaki ilk "$X" veya sayi. COK KIRILGAN, dogrula!
    const strike = parseStrike(m.question ?? m.title ?? "");
    if (!strike) {
      log.warn("auto: strike parse edilemedi, market atlaniyor:", m.question);
      return null;
    }

    return {
      id: m.conditionId ?? m.id ?? String(endTs),
      yesTokenId: tokens[yi],
      noTokenId: tokens[ni],
      strike,
      resolveTs: endTs,
    };
  } catch (e) {
    log.err("autoMarket hata:", (e as Error).message);
    return null;
  }
}

function parseStrike(q: string): number {
  // "$62,345" veya "62345" gibi kaliplari yakala
  const m = q.replace(/,/g, "").match(/\$?\s*(\d{4,7}(?:\.\d+)?)/);
  return m ? Number(m[1]) : 0;
}
