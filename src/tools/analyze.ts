// logs/trades.jsonl analizi. Calistir: npm run analyze
import fs from "node:fs";

interface Rec {
  ts: number;
  slug: string;
  kind: string;
  result: string;
  shares: number;
  pnl: number;
  strike?: number;
  spot?: number;
  drift20?: number;
  dry?: boolean;
}

const FILE = "logs/trades.jsonl";

function summarize(rows: Rec[]) {
  const n = rows.length;
  if (n === 0) return null;
  const net = rows.reduce((s, r) => s + r.pnl, 0);
  const mean = net / n;
  const varr = rows.reduce((s, r) => s + (r.pnl - mean) ** 2, 0) / Math.max(n - 1, 1);
  const std = Math.sqrt(varr);
  const se = std / Math.sqrt(n);
  const t = se > 0 ? mean / se : 0;
  const wins = rows.filter((r) => r.pnl >= 0).length;
  return { n, net, mean, std, se, t, winRate: wins / n };
}

function line(label: string, s: ReturnType<typeof summarize>) {
  if (!s) {
    console.log(`${label.padEnd(22)} (veri yok)`);
    return;
  }
  const sig = Math.abs(s.t) > 2 ? "✓ anlamlı" : "yetersiz";
  console.log(
    `${label.padEnd(22)} n=${String(s.n).padStart(4)}  net=${s.net.toFixed(2).padStart(8)}  ` +
      `ort=${s.mean.toFixed(3).padStart(7)} ±${s.se.toFixed(3)}  t=${s.t.toFixed(2).padStart(6)} ${sig}  ` +
      `kazan=${(100 * s.winRate).toFixed(0)}%`
  );
}

function main() {
  if (!fs.existsSync(FILE)) {
    console.log(`Log yok: ${FILE}. Once botu (npm run web) calistir, islemler birikince tekrar dene.`);
    return;
  }
  const rows: Rec[] = fs
    .readFileSync(FILE, "utf8")
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => {
      try {
        return JSON.parse(l);
      } catch {
        return null;
      }
    })
    .filter((r): r is Rec => r !== null);

  console.log(`\n=== TOPLAM (${rows.length} islem) ===`);
  line("HEPSI", summarize(rows));

  console.log(`\n=== TIP BAZINDA ===`);
  const kinds = [...new Set(rows.map((r) => r.kind.split(" ")[0]))];
  for (const k of kinds) line(k, summarize(rows.filter((r) => r.kind.startsWith(k))));

  console.log(`\n=== GIRIS OYNAKLIGI (drift20) BAZINDA ===`);
  line("sakin (<3$)", summarize(rows.filter((r) => (r.drift20 ?? 99) < 3)));
  line("orta (3-8$)", summarize(rows.filter((r) => (r.drift20 ?? 99) >= 3 && (r.drift20 ?? 99) < 8)));
  line("volatil (>8$)", summarize(rows.filter((r) => (r.drift20 ?? 0) >= 8)));

  console.log(`\n=== MOD (dry/canli) ===`);
  line("DRY", summarize(rows.filter((r) => r.dry)));
  line("CANLI", summarize(rows.filter((r) => r.dry === false)));

  const box = rows.filter((r) => r.kind === "BOX").length;
  const naked = rows.filter((r) => r.kind.startsWith("NAKED")).length;
  const abort = rows.filter((r) => r.kind.startsWith("ABORT")).length;
  console.log(`\n=== OZET ===`);
  console.log(`Box tamamlanma: ${box}  |  Naked: ${naked}  |  Abort: ${abort}`);
  const denom = box + naked + abort;
  if (denom > 0)
    console.log(`Box tamamlanma orani: ${((100 * box) / denom).toFixed(0)}%  (naked+abort riski: ${((100 * (naked + abort)) / denom).toFixed(0)}%)`);
  console.log(
    `\nYorum: |t|>2 VE net>0 ise edge istatistiksel anlamli. Aksi halde daha cok veri ` +
      `topla veya strateji negatif. DRY_RUN fill'i iyimser oldugundan gercek EV daha dusuk olabilir.\n`
  );
}

main();
