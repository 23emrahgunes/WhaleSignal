import { cfg } from "./config.js";
import { log } from "./logger.js";
import type { MarketRef } from "./strategy.js";

const GAMMA = "https://gamma-api.polymarket.com";
const WINDOW_SEC = 300; // 5 dakika
const OFFSETS = [-1, 0, 1, 2];
const SLUG_PATTERNS = ["btc-updown-5m-{ts}", "btc-up-or-down-5m-{ts}"];
const UP_OUTCOMES = new Set(["up", "long", "yes"]);
const DOWN_OUTCOMES = new Set(["down", "short", "no"]);
const STRIKE_KEYS = [
  "priceToBeat",
  "price_to_beat",
  "targetPrice",
  "target_price",
  "strikePrice",
  "strike_price",
  "target",
];

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

/** 5dk pencere sinirina gore aday slug'lar uret (kardes repo mantigi). */
export function buildSlugCandidates(now = Math.floor(Date.now() / 1000)): string[] {
  const window = now - (now % WINDOW_SEC);
  const out: string[] = [];
  for (const off of OFFSETS) {
    const ts = window + off * WINDOW_SEC;
    for (const pat of SLUG_PATTERNS) out.push(pat.replace("{ts}", String(ts)));
  }
  return out;
}

function coerceList(v: any): any[] {
  if (Array.isArray(v)) return v;
  if (typeof v === "string") {
    try {
      const p = JSON.parse(v);
      return Array.isArray(p) ? p : [];
    } catch {
      return [];
    }
  }
  return [];
}

/** payload icinde verilen anahtarlari ic ice arar. */
function recursiveFind(obj: any, keys: string[]): any {
  if (obj == null || typeof obj !== "object") return undefined;
  for (const k of keys) if (obj[k] != null) return obj[k];
  for (const val of Object.values(obj)) {
    if (val && typeof val === "object") {
      const found = recursiveFind(val, keys);
      if (found != null) return found;
    }
  }
  return undefined;
}

function upDownIndexes(outcomes: any[]): [number, number] {
  const find = (set: Set<string>) =>
    outcomes.findIndex((o) => set.has(String(o).trim().toLowerCase()));
  const up = find(UP_OUTCOMES);
  const down = find(DOWN_OUTCOMES);
  if (up < 0 || down < 0 || up === down) return [0, 1];
  return [up, down];
}

/** slug'tan market baslangic (start) epoch'unu cikar (saniye). */
function startFromSlug(slug: string): number | null {
  const m = slug.match(/-5m-(\d+)$/);
  return m ? Number(m[1]) : null;
}

async function fetchSlug(slug: string): Promise<any | null> {
  try {
    const r = await fetch(`${GAMMA}/markets/slug/${slug}`, {
      signal: AbortSignal.timeout(4000),
    });
    if (r.status !== 200) return null;
    const p = await r.json();
    if (Array.isArray(p)) return p[0] ?? null;
    return p;
  } catch {
    return null;
  }
}

/** Strike yoksa Binance 5m mum acilisindan turet (start anindaki open). */
async function strikeFromBinance(startSec: number): Promise<number> {
  try {
    const url =
      `https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m` +
      `&startTime=${startSec * 1000}&limit=1`;
    const r = await fetch(url, { signal: AbortSignal.timeout(4000) });
    if (!r.ok) return 0;
    const k = await r.json();
    // kline: [openTime, open, high, low, close, ...]
    return k?.[0]?.[1] ? Number(k[0][1]) : 0;
  } catch {
    return 0;
  }
}

/**
 * auto modda aktif 5dk BTC marketini /markets/slug/{slug} ile bulur.
 * Kardes repo (pyton-polymarket) mekanizmasi baz alinmistir.
 */
export async function autoMarket(): Promise<MarketRef | null> {
  const now = Math.floor(Date.now() / 1000);
  const slugs = buildSlugCandidates(now);
  const payloads = await Promise.all(slugs.map(fetchSlug));

  type Cand = { p: any; slug: string; start: number; resolveTs: number };
  const cands: Cand[] = [];
  for (let i = 0; i < slugs.length; i++) {
    const p = payloads[i];
    if (!p || p.active !== true) continue;
    const slug = String(p.slug || slugs[i]);
    const start = startFromSlug(slug) ?? 0;
    // Resolve: endDate varsa onu kullan, yoksa start+300
    const endRaw = recursiveFind(p, ["endDate", "endDateIso", "closedTime"]);
    const resolveTs = endRaw
      ? Math.floor(Date.parse(String(endRaw)) / 1000) || start + WINDOW_SEC
      : start + WINDOW_SEC;
    cands.push({ p, slug, start, resolveTs });
  }

  // Cozulmesi gelecekte olan en yakin market
  const live = cands
    .filter((c) => c.resolveTs > now + 2)
    .sort((a, b) => a.resolveTs - b.resolveTs)[0];
  if (!live) {
    log.warn("auto: aktif 5dk BTC market bulunamadi.");
    return null;
  }

  const tokens = coerceList(live.p.clobTokenIds);
  if (tokens.length < 2) {
    log.err("auto: clobTokenIds cozulemedi:", live.slug);
    return null;
  }
  const outcomes = coerceList(live.p.outcomes);
  const [ui, di] = upDownIndexes(outcomes);

  let strike = Number(recursiveFind(live.p, STRIKE_KEYS)) || 0;
  if (!strike && live.start) {
    strike = await strikeFromBinance(live.start);
    if (strike) log.info(`strike Binance 5m acilisindan turetildi: ${strike}`);
  }
  if (!strike) {
    log.warn("auto: strike bulunamadi, market atlaniyor:", live.slug);
    return null;
  }

  log.ok(
    `auto market: ${live.slug} strike=${strike} resolve=${new Date(live.resolveTs * 1000).toISOString()}`
  );
  return {
    id: live.slug,
    yesTokenId: String(tokens[ui]),
    noTokenId: String(tokens[di]),
    strike,
    resolveTs: live.resolveTs,
  };
}
