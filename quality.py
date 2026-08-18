"""Veri sagligi / freshness kapisi -> bayat ise ABSTAIN(STALE_DATA).

Hizli kaynak (direct Binance) bayatsa yon uretme guvenli DEGIL. Bu modul saf
fonksiyonlarla FeatureSnapshot'in tazeligini denetler; ag baglantisi yok (test).
"""
from __future__ import annotations

from dataclasses import dataclass

from config import Settings
from models import AbstainReason, FeatureSnapshot


@dataclass
class QualityResult:
    ok: bool
    reason: AbstainReason
    notes: list[str]


def _stale(age_ms, limit_ms) -> bool:  # noqa: ANN001
    return age_ms is None or age_ms > limit_ms


def check_freshness(snap: FeatureSnapshot, settings: Settings) -> QualityResult:
    """Spot/book KRITIK (hizli kaynak); bayatsa STALE_DATA -> ABSTAIN.

    reference/clob eksikligi stale-block DEGIL ama not olarak isaretlenir
    (distance/teyit hesaplanamaz; yon katmani bunu ayrica degerlendirir).
    """
    notes: list[str] = []
    critical_stale = False

    if _stale(snap.spot_age_ms, settings.max_spot_age_ms):
        notes.append("spot_stale")
        critical_stale = True
    if _stale(snap.book_age_ms, settings.max_book_age_ms):
        notes.append("book_stale")
        critical_stale = True

    if _stale(snap.reference_age_ms, settings.max_reference_age_ms):
        notes.append("reference_missing_or_stale")
    if _stale(snap.clob_age_ms, settings.max_clob_age_ms):
        notes.append("clob_missing_or_stale")

    if critical_stale:
        return QualityResult(False, AbstainReason.STALE_DATA, notes)
    return QualityResult(True, AbstainReason.NONE, notes)
