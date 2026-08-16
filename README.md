# dual-arbitraj

Polymarket ikili (UP/DOWN) opsiyon piyasalarinda **0.40$ Bid + 0.40$ Ask cift limit**
market-making + post-spike **mean-reversion** stratejisi yuruten, Binance (ATR/hiz) ve
Deribit (IV/DVOL) dis verileriyle beslenen; modüler, asenkron, test edilebilir Python botu.

> **Guvenlik:** Varsayilan mod `SIM` — gercek emir gondermez. Gercek emir yalniz
> `.env`'de `EXEC_MODE=LIVE` + gecerli anahtarlar verildiginde gonderilir. Anahtarlar
> yalniz env'den okunur, repoya girmez.

## Mimari / moduller

| Dosya | Sorumluluk |
|---|---|
| `models.py` | Paylasilan dataclass/enum'lar (OrderBook, Candle, MarketMeta, MarketState, ...) |
| `config.py` | Pydantic-settings: uc noktalar, esikler, risk, exec modu, CLOB creds |
| `data_ingestion.py` | Async WS/REST akislari (CLOB book, Binance kline, Deribit IV, Gamma) + **exponential-backoff reconnect** + `DataHub` |
| `analytics_engine.py` | Saf hesaplar: **OBI**, ATR(14), Bollinger squeeze, **ADX**(14), fiyat hizi/doyum, **time-decay** |
| `execution_strategy.py` | Giris tetigi (`should_enter`) + **Adverse-Selection** durum makinesi (tek-bacak guard) |
| `clob_executor.py` | `py_clob_client_v2` sarmalayici (SIM/DRY/LIVE); imza type3/POLY_1271 |
| `simulator_backtester.py` | Paper/sim: fill-rate, PnL, **Sharpe**; ndjson replay |
| `main.py` | `asyncio.TaskGroup` orkestrator; temiz kapanis (SIGINT/SIGTERM -> emir iptal) |
| `test_suite.py` | pytest: OBI, expiry, adverse-selection, WS reconnect backoff |

## Akis

```
MARKET DATA (Gamma meta + Binance kline + Deribit IV + Polymarket CLOB book)
   -> DataHub (birlesik anlik durum)
   -> analytics_engine (OBI / ATR / ADX / hiz / time-decay)
   -> should_enter:  |OBI|<0.15  &  ATR%<esik  &  ADX<20  &  kalan_sure>%10
   -> ClobExecutor.place(UP@0.40) + place(DOWN@0.40)   [SIM/DRY/LIVE]
   -> Simulator.on_tick -> dolumlar -> AdverseSelectionGuard
        bir bacak doldu, digeri T sn'de dolmazsa -> CANCEL_OPEN (iptal/hedge)
        iki bacak doldu -> box KILITLENDI (garanti kar)
```

## Giris sartlari (execution_strategy.should_enter)
- `|OBI| < OBI_MAX` (simetrik tahta)
- `ATR_1m / fiyat < ATR_MAX_PCT` (dusuk volatilite)
- `ADX < ADX_MAX` (konsolidasyon / trendsiz)
- kalan sure > toplam surenin `TIME_DECAY_PCT`'i (vade sonu %10 disi)
- (opsiyonel) Bollinger squeeze

## Kurulum

```bash
cd dual-arbitraj
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # gerekirse duzenle
```

## Calistirma

```bash
# SIM (guvenli, emir yok) — canli WS'e baglanir, sinyal + simule PnL loglar
EXEC_MODE=SIM python main.py

# DRY — emir imzalanir ama POST edilmez (creds/imza dogrulama)
EXEC_MODE=DRY python main.py

# LIVE — GERCEK emir (dikkat!). py_clob_client_v2 + gecerli anahtarlar gerekir.
EXEC_MODE=LIVE python main.py
```

`LIVE`/`DRY` icin resmi `py_clob_client_v2` kutuphanesi kurulu olmali (bu workspace'te
`pm-edge` / `pyton-polymarket` ile ayni surum). SIM ve testler bu kutuphane olmadan calisir.

## Test

```bash
pytest -q
```

Zorunlu senaryolar `test_suite.py` icinde:
1. **OBI** — 100/100 -> 0.0 ; 300/100 -> 0.5
2. **Expiry** — 1000 sn kalan -> True ; 50 sn kalan -> False
3. **Adverse-selection** — DOWN doldu, 15 sn'de UP dolmadi -> CANCEL_OPEN
4. **WS reconnect** — bozuk JSON / kopma -> cokmeden exponential backoff ile yeniden baglanir
