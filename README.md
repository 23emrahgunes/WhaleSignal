# Polymarket Whale Intelligence Engine

Polymarket Whale Engine, tüm Polymarket piyasalarını sürekli tarayarak leaderboard dışındaki istikrarlı, takip edilebilir ve yüksek kaliteli cüzdanları keşfetmek için tasarlanmış bir analiz motorudur. Repo’nun amacı trade execution değil, Polymarket ekosistemindeki güçlü cüzdanları sistematik biçimde tespit etmek, puanlamak, sıralamak ve güncel watchlist’ler üretmektir.

## Milestone 2: Continuous Intelligence

Milestone 2 ile proje statik rapor üreten yapıdan sürekli çalışan bir intelligence pipeline haline gelir:
- **Persistent History**: Günlük wallet score ve tier snapshot'ları `data/history/` altında saklanır.
- **Trend Analysis**: Wallet score stabilitesi ve volatilitesi zaman içinde hesaplanır.
- **Transition Tracking**: `Rising`, `Dropped`, `Stale`, `Upgraded` ve `Downgraded` wallet'lar otomatik bulunur.
- **Bot-Ready Watchlists**: `Core`, `Emerging`, `Probation` ve kategori bazlı watchlist'ler JSON ve CSV olarak üretilir.

## Project Structure

- `src/`: Core logic and API clients.
  - `persistence.py`: Historical snapshot management.
  - `wallet_transitions.py`: Transition analysis between snapshots.
  - `wallet_ranker.py`: Ranking and watchlist generation.
- `scripts/`: Daily pipeline scripts.
- `data/history/`: Persistent snapshot storage.
- `reports/watchlists/`: Bot-consumable JSON/CSV outputs.

## Installation

```bash
pip install -r requirements.txt
```

## Daily Pipeline Workflow

```bash
export PYTHONPATH=$PYTHONPATH:.
python3 scripts/run_fast_scan.py
python3 scripts/run_enrichment.py
python3 scripts/run_daily_rescore.py
python3 scripts/run_transitions.py
python3 scripts/publish_watchlists.py
```

## Watchlist Definitions

- **Core Watchlist**: Uzun süredir güçlü kalan A-tier wallet'lar.
- **Emerging Watchlist**: Skoru yükselen veya yakın zamanda upgrade olan wallet'lar.
- **Probation Watchlist**: Kalitesi düşen, downgrade olan veya stale davranan wallet'lar.
- **Category Watchlists**: Crypto, Sports, Politics ve Other uzmanlık listeleri.

## Scoring & Penalties

Engine, weighted wallet quality formula ve history-based modifier kullanır:
- **Stability Bonus**: Kararlı score trendine ek puan.
- **Volatility Penalty**: Oynak score davranışına ceza.
- **Stale Penalty**: >14 gün inaktif wallet'lar için ceza, >30 gün için double stale penalty.
- **Concentration Penalty**: Tek markete aşırı bağımlılık cezası.
