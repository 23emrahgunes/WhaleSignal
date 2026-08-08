# basit-arbitraj — Polymarket 5dk BTC BOX arbitraj botu

Polymarket'in 5 dakikalik BTC "up/down" marketlerinde **iki bacagi da ucuza
yakalayip toplam maliyeti $1'in altinda tutan** bot.

> **Fikir:** Up @ ~0.40 + Down @ ~0.40 = ~0.80 maliyet. Resolution'da mutlaka
> bir taraf $1 oder → **yonden bagimsiz garanti kar** (~0.20/cift). Volatilite,
> her bacagi ayri ayri ucuza doldurmak icindir. En iyi bolge: fiyatin
> `priceToBeat` (strike) etrafinda aktigi an — iki taraf da ~0.50, oynaklik her
> ikisini sirayla hedefe dusurur.

Bu **gercek arbitrajdir** (klasik "box"), yonlu bahis degil. Tek gercek risk
**leg risk**: bir bacagi doldurup digerini dolduramamak. Bot bunu deadline +
abort mantigiyla yonetir.

## Strateji akisi (state machine)

```
FLAT ──(bir bacak <= hedef)──► ONE_LEG ──(diger bacak <= tavan)──► LOCKED
  │                               │                                  │
  │ (iki bacak da <= hedef)       │ (flatten'da tamamlanamaz)        │ resolution'a
  └────────► LOCKED (aninda)      └────► ABORT (bacagi sat / tut)    └─ tutulur, $1 alinir
```

- **FLAT:** strike'a yakin (`|spot−strike| ≤ PROX_MAX_USD`) ve ask ≤ `TARGET_LEG_PRICE`
  olan bacagi al. Iki taraf ayni anda ucuzsa (`askA+askB ≤ MAX_PAIR_COST`) ikisini
  birden alip aninda kilitle.
- **ONE_LEG:** diger bacak icin tavan = `MAX_PAIR_COST − p1`. Ask bu tavanin
  altina inince tamamla → **LOCKED**. `COMPLETE_DEADLINE_SEC`'ten sonra yeni ilk
  bacak acilmaz.
- **LOCKED:** iki bacak da dolu, toplam < $1 → resolution'a tut, garanti $1 al.
  (Box tamamlandiginda tutmak DOGRUdur; cıplak bacakta degildir.)
- **ABORT:** `FLATTEN_SEC` icinde tamamlanamazsa: `ABORT_MODE=sell` cıplak bacagi
  satip riski kapatir (strike'a yakinsa ~breakeven), `hold` ise resolution'a tutar (50/50).

## Mimari

| Dosya | Görev |
|---|---|
| `src/priceFeed.ts` | Binance BTC spot (yakinlik/gating icin) |
| `src/marketResolver.ts` | Aktif 5dk BTC marketini bulma (manual/auto) |
| `src/strategy.ts` | BOX state machine (giris/tamamla/kilit/abort) |
| `src/polymarket.ts` | CLOB order book + marketable (FAK) al/sat |
| `src/risk.ts` | Günlük zarar kill-switch, pozisyon/işlem limitleri |
| `src/index.ts` | Ana döngü (4 Hz) + market rollover |
| `src/tools/inspectMarket.ts` | Gamma ham yanıtı (`npm run inspect`) |
| `src/tools/boxTest.ts` | Box mantigi birim testi (`npx tsx src/tools/boxTest.ts`) |

## Kurulum

```bash
npm install
cp .env.example .env   # .env'i doldur
```

Kritik `.env` alanlari:

- `PRIVATE_KEY` — **küçük sermayeli, adanmış** Polygon cüzdanı (ana cüzdanını kullanma).
- `MARKET_MODE=manual` → `MARKET_YES_TOKEN_ID` (Up), `MARKET_NO_TOKEN_ID` (Down),
  `MARKET_STRIKE`, `MARKET_RESOLVE_TS`. Bunlari bulmak icin `npm run inspect`.
- `TARGET_LEG_PRICE=0.40` — senin "40 cent" hedefin.
- `MAX_PAIR_COST=0.97` — toplam maliyet tavani (<1 = garanti kar).
- `DRY_RUN=true` → hiç gerçek emir gitmez. **Önce bununla izle.**

## Çalıştırma

```bash
npm start        # otomatik bot (headless)
npm run dev      # dosya değişince yeniden başlat
npm run web      # WEB PANEL (manuel kontrol + net PnL)
```

## Web panel (manuel box kontrolü)

`npm run web` → `http://127.0.0.1:3000`. Net PnL, aktif market, UP/DOWN order
book ve limit fiyatlarını gösterir; tek tıkla **UP+DOWN'a aynı anda limit emir**
koyar (varsayılan 0.40 fiyat, 5 share). İki bacak da dolup toplam < $1 olunca
kilitlenir ve garanti kâr net PnL'e eklenir.

**Güvenlik katmanları:**
- Varsayılan `WEB_HOST=127.0.0.1` → sadece VPS içi (SSH tüneli ile eriş, en güvenli).
- Halka açık (`WEB_HOST=0.0.0.0`) için **şifre zorunlu** (`WEB_USER`/`WEB_PASS`) — yoksa başlamaz.
- **HTTPS** (`WEB_TLS_CERT`/`WEB_TLS_KEY`) → şifre şifreli gider (halka açıkta şart).
- **Brute-force kilidi**: 8 hatalı denemeden sonra IP 5dk bloklanır.
- Basic Auth **sabit-zamanlı** karşılaştırma (timing attack'e karşı).

### A) SSH tüneli (IP kısıtlaması yok, en güvenli)
```bash
ssh -L 3000:localhost:3000 KULLANICI@VPS_IP
```
Sonra `http://localhost:3000`. Firewall'da hiçbir port açma.

### B) Halka açık + HTTPS + key (IP değişkense)
1. Self-signed sertifika üret (VPS'te, proje kökünde):
   ```bash
   openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 3650 -subj "/CN=arbitraj"
   ```
2. `.env`: `WEB_HOST=0.0.0.0`, `WEB_USER`, uzun `WEB_PASS`, `WEB_TLS_CERT=cert.pem`, `WEB_TLS_KEY=key.pem`.
3. Firewall'da portu aç, tarayıcıda `https://VPS_IP:3000` (self-signed → tek seferlik güvenlik uyarısını kabul et).

> Not: Web panelini VE otomatik botu (`npm start`) aynı anda çalıştırma —
> ikisi de emir verir, çakışır. Birini seç.

## ⚠️ Canlıya geçmeden DOĞRULA

1. **Maker (varsayilan) / taker:** `LEG_MODE=maker` → iki tarafa `TARGET_LEG_PRICE`
   (0.40) resting bid koyar, volatilite doldurur (ucuz gir, spread kazan). Biri
   dolunca digeri zaten hedefte beklerken kilit otomatik. `LEG_MODE=taker` → ask
   hedefe dusunce alir (garanti fill, spread'i sen odersin). Maker slippage yemez,
   bu yuzden daha karli (ornek: maker 0.40+0.40=0.80 vs taker 0.42+0.42=0.84).
   **Live fill takibi `getOrder(size_matched)` ile yapilir — canlida dogrula.**
2. **Resolution kaynağı:** Marketin gerçek çözülme fiyatı (Pyth/Chainlink) — Binance
   sadece yakinlik gating icin proxy. Box kilitlendikten sonra sonuç onemsiz (iki
   tarafi da tutuyorsun), ama gating dogru olsun diye kaynagi dogrula.
3. **Gamma şeması:** `auto` mod kırılgan. `npm run inspect` ile doğrula.
4. **Fill/slippage:** FAK kısmi dolabilir → tek bacak yarim kalabilir (leg risk).
   Küçük `PAIR_SHARES` ile başla. `SLIPPAGE` toplam maliyete ekleniyor, tavani ona göre kur.
5. **Ağ:** Bu makinede Polymarket domainleri erişilebilir olmali (bazi ağlarda
   geo/firewall engelli). Binance ise ayrı çalışır.

## Bilinen sınır / sonraki adım

- **Kısmi fill eşitleme** — maker'da bacaklar farklı adette dolarsa fazla tarafı buda/sat.
- **Çoklu cift** — tek markette birden fazla box biriktirme.
- **WebSocket order takibi** — v1 fill'i REST `getOrder` ile poller (4 Hz). Yüksek
  frekansta CLOB user-channel WS'e geçmek daha iyi.
- **auto resolver sağlamlaştırma** — Gamma'dan 5dk BTC market + strike güvenilir bulma.
