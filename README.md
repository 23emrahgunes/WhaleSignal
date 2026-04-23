# Polymarket Whale Intelligence Engine

Polymarket Whale Engine, tüm Polymarket piyasalarını sürekli tarayarak leaderboard dışındaki istikrarlı, takip edilebilir ve yüksek kaliteli cüzdanları keşfetmek için tasarlanmış bir analiz motorudur. Repo’nun çekirdeği whale intelligence üretir; bu branch ayrıca stable-wallet seçimi ve paper copy-trader katmanını ekler.

## Milestone 2: Continuous Intelligence

Milestone 2 ile proje statik rapor üreten yapıdan sürekli çalışan bir intelligence pipeline haline gelir:
- **Persistent History**: Günlük wallet score ve tier snapshot'ları `data/history/` altında saklanır.
- **Trend Analysis**: Wallet score stabilitesi ve volatilitesi zaman içinde hesaplanır.
- **Transition Tracking**: `Rising`, `Dropped`, `Stale`, `Upgraded` ve `Downgraded` wallet'lar otomatik bulunur.
- **Bot-Ready Watchlists**: `Core`, `Emerging`, `Probation` ve kategori bazlı watchlist'ler JSON ve CSV olarak üretilir.

## Milestone 3: Archetypes, Opportunities, Risk

Milestone 3 ile wallet kalitesi yalnızca skor değil, takip edilebilirlik açısından da derecelendirilir:
- `wallet_archetypes.json`
- `followable_opportunities.json`
- `high_risk_opportunities.json`

## Stable Wallet CopyTrader (Paper)

Bu branch şu katmanları ekler:
- `src/stable_wallets.py`: Takip edilmeye değer istikrarlı cüzdanları seçer.
- `scripts/run_stable_wallet_selection.py`: `stable_wallets.json/csv` üretir.
- `src/copytrader_paper.py`: Seçilen stable wallet'ların son trade sinyallerine göre paper kopya pozisyonları açar/kapatır.
- `scripts/run_copytrader_paper.py`: paper copytrader state ve summary üretir.

Üretilen ana çıktılar:
- `reports/stable_wallets.json`
- `reports/stable_wallets.csv`
- `reports/paper_copytrader_state.json`
- `reports/paper_copytrader_actions.json`
- `reports/paper_copytrader_summary.json`

## Project Structure

- `src/`: Core logic and API clients.
  - `persistence.py`: Historical snapshot management.
  - `wallet_transitions.py`: Transition analysis between snapshots.
  - `wallet_ranker.py`: Ranking and watchlist generation.
  - `stable_wallets.py`: Stable wallet selection.
  - `copytrader_paper.py`: Paper copytrader engine.
- `scripts/`: Daily pipeline scripts.
- `data/history/`: Persistent snapshot storage.
- `reports/watchlists/`: Bot-consumable JSON/CSV outputs.

## Installation

```bash
pip install -r requirements.txt
```

## Full Workflow

```bash
export PYTHONPATH=$PYTHONPATH:.
python3 scripts/run_fast_scan.py
python3 scripts/run_enrichment.py
python3 scripts/run_daily_rescore.py
python3 scripts/run_transitions.py
python3 scripts/publish_watchlists.py
python3 scripts/run_archetype_classification.py
python3 scripts/run_opportunity_scan.py
python3 scripts/run_stable_wallet_selection.py
python3 scripts/run_copytrader_paper.py
```

## Watchlist Definitions

- **Core Watchlist**: Uzun süredir güçlü kalan A-tier wallet'lar.
- **Emerging Watchlist**: Skoru yükselen veya yakın zamanda upgrade olan wallet'lar.
- **Probation Watchlist**: Kalitesi düşen, downgrade olan veya stale davranan wallet'lar.
- **Category Watchlists**: Crypto, Sports, Politics ve Other uzmanlık listeleri.
- **Stable Wallets**: FOLLOW + policy pass + minimum score filtrelerini geçen copy-uygun cüzdanlar.

## Scoring & Penalties

Engine, weighted wallet quality formula ve history-based modifier kullanır:
- **Stability Bonus**: Kararlı score trendine ek puan.
- **Volatility Penalty**: Oynak score davranışına ceza.
- **Stale Penalty**: >14 gün inaktif wallet'lar için ceza, >30 gün için double stale penalty.
- **Concentration Penalty**: Tek markete aşırı bağımlılık cezası.

## Notes

- Paper copytrader gerçek para kullanmaz.
- Kopya pozisyonlar, seçilen stable wallet'ların son BUY/SELL sinyallerinden türetilir.
- Generated report/state dosyaları repoya commit edilmemelidir.
