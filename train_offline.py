"""P3 — Offline egitim + walk-forward backtest (recorded dataset uzerinde).

Recorder SQLite dataset'inden GBT egitir ve out-of-sample kalibrasyonu olcer.

KRITIK kurallar (plan geregi):
  - **Label = RESMI resolved sonuc** (snapshots.final_result); yerel fiyat kiyasi DEGIL.
  - **Split = market_id (condition_id) bazli + KRONOLOJIK walk-forward**: ayni marketin
    snapshot'lari asla train/test'e bolunmez; test HER ZAMAN train'den SONRAKI marketler.
  - **CLOB'lu vs CLOB'suz** iki varyant AYRI egitilir/kiyaslanir; ayrica **Polymarket-implied**
    (up_mid) baz cizgisi.
  - **n < MIN_MARKETS iken hicbir ustunluk/accuracy iddiasi YAZILMAZ** (uydurma yok).

Model: lightgbm varsa onu, yoksa sklearn HistGradientBoostingClassifier (GBT).
`python train_offline.py [db_path]` rapor basar + models/offline_report.json yazar.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from config import get_settings
from features import FeatureVector

log = logging.getLogger("direction_engine.train")

MIN_MARKETS = 30  # bunun altinda ustunluk/accuracy iddiasi YOK
_BASE = FeatureVector._BASE_FIELDS
_CLOB = FeatureVector._CLOB_FIELDS


# ---------------------------------------------------------------------------
# Veri modeli
# ---------------------------------------------------------------------------


@dataclass
class SnapRow:
    condition_id: str
    combo_key: str
    seconds_remaining: float
    features: dict
    up_mid: Optional[float]
    label_up: int  # RESMI resolved: UP=1, DOWN=0


@dataclass
class MarketData:
    condition_id: str
    combo_key: str
    start_ts: float
    label_up: int
    snaps: list[SnapRow] = field(default_factory=list)

    @property
    def last_snap(self) -> SnapRow:
        """Kapanisa en yakin (en kucuk seconds_remaining) snapshot."""
        return min(self.snaps, key=lambda s: s.seconds_remaining)


def feature_vector(features: dict, include_clob: bool) -> list[float]:
    """extra_json dict'inden model_features ile ayni sirali vektor (leakage-guvenli)."""
    names = list(_BASE) + (list(_CLOB) if include_clob else [])
    return [float(features.get(n, 0.0) or 0.0) for n in names]


# ---------------------------------------------------------------------------
# Dataset yukleme
# ---------------------------------------------------------------------------


def load_dataset(db_path: str) -> list[MarketData]:
    """Yalniz RESMI resolved + meta_ok + etiketli snapshot'lari yukle; markete grupla."""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT s.condition_id, s.combo_key, s.seconds_remaining, s.extra_json,
               s.up_mid, s.final_result, m.start_ts
        FROM snapshots s
        JOIN markets m ON s.condition_id = m.condition_id
        WHERE m.resolved = 1 AND m.meta_ok = 1 AND s.final_result IS NOT NULL
              AND s.extra_json IS NOT NULL
        """
    ).fetchall()
    conn.close()

    markets: dict[str, MarketData] = {}
    for r in rows:
        try:
            feats = json.loads(r["extra_json"])
        except (ValueError, TypeError):
            continue
        label = 1 if str(r["final_result"]).upper() == "UP" else 0
        md = markets.get(r["condition_id"])
        if md is None:
            md = MarketData(
                condition_id=r["condition_id"],
                combo_key=r["combo_key"],
                start_ts=float(r["start_ts"] or 0.0),
                label_up=label,
            )
            markets[r["condition_id"]] = md
        md.snaps.append(
            SnapRow(
                condition_id=r["condition_id"],
                combo_key=r["combo_key"],
                seconds_remaining=float(r["seconds_remaining"] or 0.0),
                features=feats,
                up_mid=(float(r["up_mid"]) if r["up_mid"] is not None else None),
                label_up=label,
            )
        )
    return list(markets.values())


# ---------------------------------------------------------------------------
# Walk-forward split (market_id bazli + kronolojik) — LEAKAGE YOK
# ---------------------------------------------------------------------------


def walk_forward_folds(
    markets: list[MarketData], n_folds: int = 4, min_train: int = 10
) -> list[tuple[list[MarketData], list[MarketData]]]:
    """Marketleri start_ts'e gore sirala; genisleyen-pencere walk-forward foldlari.

    Her fold: train = ilk k market, test = sonraki dilim. Ayni market ikisinde birden
    OLMAZ; test hep train'den sonra baslar (kronolojik). Az veri -> tek split.
    """
    ordered = sorted(markets, key=lambda m: (m.start_ts, m.condition_id))
    n = len(ordered)
    if n < min_train + 2:
        return []
    folds: list[tuple[list[MarketData], list[MarketData]]] = []
    # test dilim sinirlari: min_train'den n'e n_folds parca
    start = max(min_train, n // (n_folds + 1))
    bounds = np.linspace(start, n, n_folds + 1).astype(int)
    bounds = sorted(set(int(b) for b in bounds))
    for i in range(len(bounds) - 1):
        tr_end = bounds[i]
        te_end = bounds[i + 1]
        if te_end <= tr_end:
            continue
        train = ordered[:tr_end]
        test = ordered[tr_end:te_end]
        if train and test:
            folds.append((train, test))
    return folds


def simple_split(
    markets: list[MarketData], train_frac: float = 0.7
) -> tuple[list[MarketData], list[MarketData]]:
    ordered = sorted(markets, key=lambda m: (m.start_ts, m.condition_id))
    k = max(1, int(len(ordered) * train_frac))
    return ordered[:k], ordered[k:]


# ---------------------------------------------------------------------------
# Matris insaasi + metrikler
# ---------------------------------------------------------------------------


def build_xy(
    markets: list[MarketData], include_clob: bool, market_level: bool = False
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Snapshot-level (tum snap) veya market-level (yalniz son snap) X,y."""
    X: list[list[float]] = []
    y: list[int] = []
    cids: list[str] = []
    for md in markets:
        snaps = [md.last_snap] if market_level else md.snaps
        for s in snaps:
            X.append(feature_vector(s.features, include_clob))
            y.append(s.label_up)
            cids.append(md.condition_id)
    return np.array(X, dtype=float), np.array(y, dtype=int), cids


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def log_loss(y: np.ndarray, p: np.ndarray, eps: float = 1e-12) -> float:
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def accuracy(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p >= 0.5).astype(int) == y))


def reliability_bins(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> list[dict]:
    out = []
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (p >= lo) & (p < hi) if i < n_bins - 1 else (p >= lo) & (p <= hi)
        n = int(mask.sum())
        if n == 0:
            continue
        out.append(
            {
                "bin": f"{lo:.1f}-{hi:.1f}",
                "n": n,
                "mean_pred": round(float(p[mask].mean()), 4),
                "actual": round(float(y[mask].mean()), 4),
            }
        )
    return out


def metrics(y: np.ndarray, p: np.ndarray) -> dict:
    if len(y) == 0:
        return {"n": 0}
    return {
        "n": int(len(y)),
        "accuracy": round(accuracy(y, p), 4),
        "brier": round(brier(y, p), 4),
        "log_loss": round(log_loss(y, p), 4),
        "reliability": reliability_bins(y, p),
    }


# ---------------------------------------------------------------------------
# GBT egitici (lightgbm lazy; sklearn HistGBT fallback)
# ---------------------------------------------------------------------------


def _new_gbt():
    try:
        import lightgbm as lgb  # noqa: F401

        return ("lightgbm", lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05,
                                               max_depth=-1, num_leaves=31, verbose=-1))
    except Exception:  # noqa: BLE001
        from sklearn.ensemble import HistGradientBoostingClassifier

        return ("hist_gbt", HistGradientBoostingClassifier(
            max_iter=200, learning_rate=0.05, max_depth=None))


def train_and_eval(
    train: list[MarketData], test: list[MarketData], include_clob: bool
) -> Optional[dict]:
    """Bir varyanti egit + market-level test degerlendir. Tek sinif -> None."""
    Xtr, ytr, _ = build_xy(train, include_clob, market_level=False)
    Xte, yte, _ = build_xy(test, include_clob, market_level=True)
    if len(Xtr) == 0 or len(Xte) == 0 or len(set(ytr.tolist())) < 2:
        return None
    backend, clf = _new_gbt()
    clf.fit(Xtr, ytr)
    proba = clf.predict_proba(Xte)
    classes = list(clf.classes_)
    idx = classes.index(1) if 1 in classes else 0
    p = proba[:, idx]
    return {"backend": backend, **metrics(yte, p)}


def polymarket_baseline(test: list[MarketData]) -> dict:
    """Polymarket-implied baz cizgisi: up_mid = P(UP) (market-level, son snap)."""
    y: list[int] = []
    p: list[float] = []
    for md in test:
        s = md.last_snap
        if s.up_mid is None:
            continue
        y.append(md.label_up)
        p.append(max(0.0, min(1.0, s.up_mid)))
    if not y:
        return {"n": 0}
    return metrics(np.array(y), np.array(p))


# ---------------------------------------------------------------------------
# Rapor
# ---------------------------------------------------------------------------


def build_report(db_path: str) -> dict:
    markets = load_dataset(db_path)
    n_markets = len(markets)
    report: dict = {
        "db_path": db_path,
        "n_resolved_markets": n_markets,
        "min_markets": MIN_MARKETS,
    }
    if n_markets < MIN_MARKETS:
        report["insufficient"] = True
        report["note"] = (
            f"Yalnizca {n_markets} resmi resolved market — esik {MIN_MARKETS}. "
            "Ustunluk/accuracy iddiasi URETILMEDI (uydurma yok)."
        )
        # yine de per-combo sayimlari goster
        per_combo: dict[str, int] = {}
        for m in markets:
            per_combo[m.combo_key] = per_combo.get(m.combo_key, 0) + 1
        report["resolved_per_combo"] = per_combo
        return report

    report["insufficient"] = False
    folds = walk_forward_folds(markets, n_folds=4)
    if not folds:
        train, test = simple_split(markets)
        folds = [(train, test)]
        report["split"] = "single_chronological"
    else:
        report["split"] = f"walk_forward_{len(folds)}_folds"

    variants = {"with_clob": True, "no_clob": False}
    agg: dict[str, list[dict]] = {"with_clob": [], "no_clob": [], "polymarket_implied": []}
    for train, test in folds:
        for name, inc in variants.items():
            res = train_and_eval(train, test, inc)
            if res is not None:
                agg[name].append(res)
        pm = polymarket_baseline(test)
        if pm.get("n", 0) > 0:
            agg["polymarket_implied"].append(pm)

    def _avg(key: str) -> dict:
        rows = agg[key]
        if not rows:
            return {"n_folds": 0}
        return {
            "n_folds": len(rows),
            "mean_accuracy": round(float(np.mean([r["accuracy"] for r in rows])), 4),
            "mean_brier": round(float(np.mean([r["brier"] for r in rows])), 4),
            "mean_log_loss": round(float(np.mean([r["log_loss"] for r in rows])), 4),
            "test_markets": int(sum(r["n"] for r in rows)),
            "last_fold_reliability": rows[-1].get("reliability"),
        }

    report["model_B_with_clob"] = _avg("with_clob")
    report["model_B_no_clob"] = _avg("no_clob")
    report["polymarket_implied"] = _avg("polymarket_implied")
    report["model_A_note"] = (
        "Model A (Go pm-edge) tahminleri bu dataset'te yok; kiyas icin ayri export "
        "gerekir. Burada yalniz Model B(CLOB'lu/suz) + Polymarket-implied kiyaslanir."
    )
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    db_path = sys.argv[1] if len(sys.argv) > 1 else get_settings().db_path
    report = build_report(db_path)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    os.makedirs("models", exist_ok=True)
    with open("models/offline_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    log.info("rapor yazildi: models/offline_report.json")


if __name__ == "__main__":
    main()
