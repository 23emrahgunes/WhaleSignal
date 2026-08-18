# Direction Engine vNext — Çok-Varlıklı Polymarket Kısa-Vade Yön Tahmini (SHADOW)

BTC/ETH/SOL/XRP × 5m/15m/1h = **12 kombinasyon** için, Polymarket up/down
marketlerinde kapanışta hangi tarafın kazanacağını **olasılıksal + kalibre +
ABSTAIN** yeteneğiyle tahmin eden ayrı bir Python servisi.

> **SHADOW:** Canlı emir YOK. Private key / imza / execution YOK. Servis yalnız
> veri okur, tahmin eder, kaydeder ve kalibrasyonu ölçer. Mevcut Go `pm-edge`
> (Model A) ve Dual40/arb'a **dokunmaz**.

## Neden

Mevcut yön motoru yalnız BTC 5m/15m ve "zorunlu basit model" ile ölçümde
~yazı-tura (Brier 0.25). Edge **latency-arb değil**; edge = PTB pozisyonu +
kalıcı momentum + kalıcı agresif flow + volatilite rejimi + Polymarket CLOB
teyidi **birlikte**. Model önce "bu market tahmin edilebilir mi?" (predictability)
der, sonra yön; emin değilse **ABSTAIN**.

## Mimari akış

```
DIRECT BINANCE WS (trade + diff-depth -> senkron local book, ~124ms)
   -> ring buffer -> returns + momentum persistence + agresif flow + volatilite
REFERENCE (horizon adaptoru): 5m/15m=Chainlink-oriented, 1h=Binance-candle-oriented
   -> distance_usd/bps, PTB_Z
POLYMARKET CLOB WS -> midpoint trajectory, OBI, spread (teyit)
VOLATILITE -> REGIME -> PREDICTABILITY -> DIRECTION MODEL
   -> P(UP)/P(DOWN)/confidence -> {UP, DOWN, ABSTAIN}
```

## Fazlar

- **P1** — Temel + Recorder + Shadow iskelet (feed'ler, discovery, dataset, dashboard).
- **P2** — Feature + Regime + Shadow logistic + Calibration.
- **P3** — Offline LightGBM/GBT eğitim + walk-forward backtest.

## Kurulum

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m py_compile *.py reference/*.py
pytest -q
python main.py
```

Dashboard: `http://localhost:8091`

## Discovery notu

1h ve genel keşif **Gamma active-event listeleme** ile yapılır (slug tahminine
bağlı değil); 5m/15m için `<asset>-updown-<tf>-<unix>` slug lookup yalnız fast
path. Her keşfedilen markette `resolution_source` + `resolution_type`
**zorunlu** çekilir; çözülemezse market ABSTAIN/eğitim-dışı işaretlenir.
Polymarket'te gerçekten var olan combo'lar runtime'da netleşir (dashboard "VAR/YOK").

## Dürüstlük kuralı

Yeterli gerçek market kaydedilmeden (n < `MIN_MARKETS_FOR_STATS`) **hiçbir
winrate/accuracy/PnL/edge iddiası üretilmez**. Dashboard bu durumda "yetersiz
veri" gösterir.
