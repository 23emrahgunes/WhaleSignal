"""Kalibrasyon + secici dogruluk + price-edge analitigi (SADECE olcum).

RESMI resolved market'ler geldikce modelin tahminini gercekle kiyaslar:
  - confidence bucket'lari (50-55..80+): her bucket N / gercek-winrate / ortalama-p / Brier
  - Brier skoru (dusuk iyi)
  - secici dogruluk: coverage (kapsam) vs accuracy — "az ama emin" egrisi
  - price-edge = P_model(UP) - P_market(UP) ve dogrulukla iliskisi

DURUSTLUK: n < min_n iken hicbir winrate/edge iddiasi uretilmez ("insufficient").
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

_BUCKET_EDGES = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
_BUCKET_LABELS = ["50-55", "55-60", "60-65", "65-70", "70-75", "75-80", "80+"]
_SELECTIVE_THRESHOLDS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]


@dataclass
class CalSample:
    decided: bool
    outcome_up: bool
    p_up: Optional[float] = None
    decision_up: Optional[bool] = None
    confidence: float = 0.0
    market_implied_up: Optional[float] = None


class CalibrationTracker:
    """Tek combo (veya overall) icin kalibrasyon defteri."""

    def __init__(self, maxlen: int = 5000) -> None:
        self.samples: deque[CalSample] = deque(maxlen=maxlen)

    def record(self, s: CalSample) -> None:
        self.samples.append(s)

    def _decided(self) -> list[CalSample]:
        return [s for s in self.samples if s.decided and s.p_up is not None]

    def brier(self) -> Optional[float]:
        dec = self._decided()
        if not dec:
            return None
        return sum((s.p_up - (1.0 if s.outcome_up else 0.0)) ** 2 for s in dec) / len(dec)

    def confidence_buckets(self) -> list[dict]:
        dec = self._decided()
        out = []
        for i, label in enumerate(_BUCKET_LABELS):
            lo, hi = _BUCKET_EDGES[i], _BUCKET_EDGES[i + 1]
            rows = []
            for s in dec:
                p_chosen = s.p_up if s.decision_up else (1.0 - s.p_up)
                if lo <= p_chosen < hi:
                    rows.append((p_chosen, s))
            n = len(rows)
            if n == 0:
                out.append({"bucket": label, "n": 0})
                continue
            wins = sum(1 for _, s in rows if (s.decision_up == s.outcome_up))
            mean_p = sum(p for p, _ in rows) / n
            brier = sum(
                (s.p_up - (1.0 if s.outcome_up else 0.0)) ** 2 for _, s in rows
            ) / n
            out.append(
                {
                    "bucket": label,
                    "n": n,
                    "actual_winrate": round(wins / n, 4),
                    "mean_predicted": round(mean_p, 4),
                    "brier": round(brier, 4),
                }
            )
        return out

    def selective(self) -> list[dict]:
        total = len(self.samples)
        if total == 0:
            return []
        out = []
        for thr in _SELECTIVE_THRESHOLDS:
            covered = [
                s for s in self.samples if s.decided and s.confidence >= thr and s.p_up is not None
            ]
            n = len(covered)
            acc = None
            if n > 0:
                acc = sum(1 for s in covered if s.decision_up == s.outcome_up) / n
            out.append(
                {
                    "conf_threshold": thr,
                    "coverage": round(n / total, 4),
                    "n": n,
                    "accuracy": round(acc, 4) if acc is not None else None,
                }
            )
        return out

    def price_edge(self) -> Optional[dict]:
        rows = [s for s in self._decided() if s.market_implied_up is not None]
        if not rows:
            return None
        edges = [s.p_up - s.market_implied_up for s in rows]
        mean_edge = sum(edges) / len(edges)
        # pozitif edge (UP tarafinda) dogru mu cikiyor? basit isaret-uyum orani
        agree = sum(
            1 for s in rows if ((s.p_up - s.market_implied_up) > 0) == s.outcome_up
        )
        return {
            "n": len(rows),
            "mean_edge": round(mean_edge, 4),
            "edge_sign_accuracy": round(agree / len(rows), 4),
        }

    def summary(self, min_n: int) -> dict:
        dec = self._decided()
        n = len(dec)
        base = {
            "n_decided": n,
            "n_total": len(self.samples),
            "min_n": min_n,
        }
        if n < min_n:
            base["insufficient"] = True
            return base
        base["insufficient"] = False
        wins = sum(1 for s in dec if s.decision_up == s.outcome_up)
        base["accuracy"] = round(wins / n, 4)
        base["brier"] = round(self.brier(), 4) if self.brier() is not None else None
        base["buckets"] = self.confidence_buckets()
        base["selective"] = self.selective()
        base["price_edge"] = self.price_edge()
        return base


class CalibrationBook:
    """Overall + per-combo kalibrasyon defterleri."""

    def __init__(self, min_n: int = 30) -> None:
        self.min_n = min_n
        self.overall = CalibrationTracker()
        self.per_combo: dict[str, CalibrationTracker] = {}

    def record(self, combo_key: str, s: CalSample) -> None:
        self.overall.record(s)
        self.per_combo.setdefault(combo_key, CalibrationTracker()).record(s)

    def summary(self) -> dict:
        return {
            "overall": self.overall.summary(self.min_n),
            "per_combo": {k: t.summary(self.min_n) for k, t in self.per_combo.items()},
        }
