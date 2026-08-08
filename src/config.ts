import "dotenv/config";

function num(key: string, def: number): number {
  const v = process.env[key];
  if (v === undefined || v === "") return def;
  const n = Number(v);
  if (Number.isNaN(n)) throw new Error(`Config ${key} sayi olmali, gelen: ${v}`);
  return n;
}
function str(key: string, def = ""): string {
  return process.env[key] ?? def;
}
function bool(key: string, def: boolean): boolean {
  const v = process.env[key];
  if (v === undefined || v === "") return def;
  return v.toLowerCase() === "true" || v === "1";
}

export const cfg = {
  // Cuzdan / CLOB
  privateKey: str("PRIVATE_KEY"),
  clobHost: str("CLOB_HOST", "https://clob.polymarket.com"),
  chainId: num("CHAIN_ID", 137),
  apiKey: str("CLOB_API_KEY"),
  apiSecret: str("CLOB_API_SECRET"),
  apiPassphrase: str("CLOB_API_PASSPHRASE"),

  // Fiyat kaynagi:
  //  polymarket (VARSAYILAN): Polymarket RTDS Chainlink btc/usd — marketin
  //    RESOLVE oldugu birebir fiyat (priceToBeat ile birebir tutarli).
  //  coinbase / binance: proxy borsalar (RTDS erisilemezse).
  priceSource: str("PRICE_SOURCE", "polymarket") as
    | "polymarket"
    | "coinbase"
    | "binance",

  // Market
  marketMode: str("MARKET_MODE", "auto") as "manual" | "auto",
  yesTokenId: str("MARKET_YES_TOKEN_ID"),
  noTokenId: str("MARKET_NO_TOKEN_ID"),
  strike: num("MARKET_STRIKE", 0),
  resolveTs: num("MARKET_RESOLVE_TS", 0),

  // Calisma modu
  dryRun: bool("DRY_RUN", true),

  // Strateji (BOX / cift-bacak yakalama)
  // maker: 0.40'a resting bid koy, volatilite doldursun (spread kazan, ucuz gir).
  // taker: ask hedefe dusunce al (garanti fill, ugrassiz).
  legMode: str("LEG_MODE", "maker") as "maker" | "taker",
  // Sadece fiyat strike'a bu kadar yakinken calis (USD). Box icin yakinlik = iyi fill.
  proxMaxUsd: num("PROX_MAX_USD", 5),
  // Ilk bacagi bu fiyattan (veya daha ucuza) yakala (0-1).
  targetLegPrice: num("TARGET_LEG_PRICE", 0.4),
  // Iki bacagin toplam maliyeti bunu ASLA gecmesin. <1.00 => garanti kar.
  maxPairCost: num("MAX_PAIR_COST", 0.97),
  // Kilitlemek icin gereken minimum kar marji (fiyat birimi).
  minProfit: num("MIN_PROFIT", 0.03),
  // Her bacakta alinacak hisse (share) adedi. Garanti odeme = adet * $1.
  pairShares: num("PAIR_SHARES", 10),
  // Bu sureden sonra (kalan sn) yeni ILK bacak acma; sadece tamamla/iptal et.
  completeDeadlineSec: num("COMPLETE_DEADLINE_SEC", 20),
  // Tamamlanamayan tek bacak icin: "sell" (bacagi sat, riski kapat) | "hold" (resolution'a tut, 50/50)
  abortMode: str("ABORT_MODE", "sell") as "sell" | "hold",
  // Tek bacak varken bu sureden sonra tamamla-ya-da-iptal karari ver (kalan sn).
  flattenSec: num("FLATTEN_SEC", 6),

  // Risk
  maxPositionUsd: num("MAX_POSITION_USD", 30),
  maxDailyLossUsd: num("MAX_DAILY_LOSS_USD", 20),
  maxTradesPerMarket: num("MAX_TRADES_PER_MARKET", 6),
  lossCooldownSec: num("LOSS_COOLDOWN_SEC", 10),
  slippage: num("SLIPPAGE", 0.02),
};

export function validateConfig() {
  const errs: string[] = [];
  if (!cfg.dryRun && !/^0x[0-9a-fA-F]{64}$/.test(cfg.privateKey))
    errs.push("Canli mod icin gecerli PRIVATE_KEY gerekli (0x + 64 hex karakter).");
  if (cfg.marketMode === "manual") {
    if (!cfg.yesTokenId) errs.push("manual mod: MARKET_YES_TOKEN_ID gerekli.");
    if (!cfg.noTokenId) errs.push("manual mod: MARKET_NO_TOKEN_ID gerekli.");
    if (!cfg.strike) errs.push("manual mod: MARKET_STRIKE (priceToBeat) gerekli.");
    if (!cfg.resolveTs) errs.push("manual mod: MARKET_RESOLVE_TS gerekli.");
  }
  if (cfg.targetLegPrice <= 0 || cfg.targetLegPrice >= 1)
    errs.push("TARGET_LEG_PRICE 0-1 arasi olmali.");
  if (cfg.maxPairCost >= 1)
    errs.push("MAX_PAIR_COST < 1.00 olmali (yoksa garanti kar yok).");
  if (2 * cfg.targetLegPrice > cfg.maxPairCost)
    errs.push("2*TARGET_LEG_PRICE <= MAX_PAIR_COST olmali (iki bacak da hedeften alinabilmeli).");
  if (cfg.flattenSec >= cfg.completeDeadlineSec)
    errs.push("FLATTEN_SEC < COMPLETE_DEADLINE_SEC olmali.");
  if (errs.length) {
    throw new Error("Config hatalari:\n - " + errs.join("\n - "));
  }
}
