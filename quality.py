"""Veri sagligi / invariant checker — 7 boyutlu quality + prediction_ready.

Her cycle marketin plumbing dogrulugunu denetler ve 7 ayri boyut uretir:
TIME / MARKET / TOKENS / CLOB / REFERENCE / CLOCK / MODEL. Ihlalde uygun
`AbstainReason` doner ve `prediction_ready=False` olur. Saf fonksiyonlar; ag yok.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import Settings
from models import (
    AbstainReason,
    FeatureSnapshot,
    MarketRef,
    QStatus,
    QualityReport,
    TimeStatus,
)


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


# ---------------------------------------------------------------------------
# 7-boyutlu invariant checker (P1 acceptance)
# ---------------------------------------------------------------------------


def _clob_status(snap: FeatureSnapshot, settings: Settings) -> tuple[QStatus, Optional[str]]:
    """CLOB=OK icin **UP ve DOWN ikisi de** gerekli: bid+ask var, bid<=ask, [0,1], fresh."""
    sides = (("up", snap.up_bid, snap.up_ask), ("down", snap.down_bid, snap.down_ask))
    for name, b, a in sides:
        if b is None or a is None:
            return QStatus.WAITING, f"clob:waiting(no_{name}_quote)"  # 0.505 fallback YOK
        if not (0.0 <= b <= 1.0) or not (0.0 <= a <= 1.0):
            return QStatus.FAIL, f"clob:{name}_out_of_range(bid={b},ask={a})"
        if b > a:
            return QStatus.FAIL, f"clob:{name}_bid>ask({b}>{a})"
    if snap.clob_age_ms is not None and snap.clob_age_ms > settings.max_clob_age_ms:
        return QStatus.WAITING, "clob:stale"  # bayat -> usable degil -> ABSTAIN(CLOB_MISSING)
    return QStatus.OK, None


def assess(
    ref: MarketRef,
    snap: FeatureSnapshot,
    settings: Settings,
    now: float,
    clock_synced: bool = True,
    model_ready: bool = False,
) -> QualityReport:
    """7 boyut + prediction_ready + snapshot_recordable + primary AbstainReason."""
    notes: list[str] = []
    horizon_sec = ref.combo.horizon.seconds
    tte = snap.tte_sec if snap.tte_sec is not None else ref.remaining_sec(now)

    # TIME
    if ref.time_status != TimeStatus.OK:
        time_q = QStatus.FAIL
        notes.append(f"time:{ref.time_status.value}")
    elif tte is None or tte < -1.0 or tte > horizon_sec + 2.0:
        time_q = QStatus.FAIL
        notes.append(f"time:TTE_out({tte})")
    else:
        time_q = QStatus.OK

    # MARKET
    if not ref.market_id:
        market_q = QStatus.FAIL
        notes.append("market:no_id")
    elif not ref.has_resolution_meta:
        market_q = QStatus.WARN
        notes.append("market:no_resolution_meta")
    else:
        market_q = QStatus.OK

    # TOKENS
    if not ref.up_token_id or not ref.down_token_id:
        tokens_q = QStatus.FAIL
        notes.append("tokens:missing")
    elif ref.up_token_id == ref.down_token_id:
        tokens_q = QStatus.FAIL
        notes.append("tokens:identical")
    else:
        tokens_q = QStatus.OK

    # CLOB
    clob_q, clob_note = _clob_status(snap, settings)
    if clob_note:
        notes.append(clob_note)

    # REFERENCE (PTB)
    if snap.reference_price is None:
        reference_q = QStatus.WAITING
        notes.append("reference:PTB_MISSING")
    else:
        reference_q = QStatus.OK

    # CLOCK
    clock_q = QStatus.OK if clock_synced else QStatus.FAIL
    if not clock_synced:
        notes.append("clock:unsync")

    # MODEL (P1: her zaman WARN -> MODEL_NOT_TRAINED)
    model_q = QStatus.OK if model_ready else QStatus.WARN

    # FEED health (transport/source) — ayrik; seyrek trade != stale
    feed_ok = (
        snap.transport_age_ms is not None
        and snap.transport_age_ms <= settings.max_transport_age_ms
    )
    if not feed_ok:
        notes.append("feed:transport_stale")

    snapshot_recordable = (
        time_q == QStatus.OK
        and market_q in (QStatus.OK, QStatus.WARN)
        and tokens_q == QStatus.OK
        and feed_ok
    )
    prediction_ready = (
        snapshot_recordable
        and clob_q == QStatus.OK
        and reference_q == QStatus.OK
        and clock_q == QStatus.OK
        and model_q == QStatus.OK
    )

    # primary abstain reason (oncelik sirasi)
    reason = AbstainReason.NONE
    if time_q == QStatus.FAIL:
        reason = AbstainReason.UNSAFE_TIME_METADATA
    elif tokens_q == QStatus.FAIL or market_q == QStatus.FAIL:
        reason = AbstainReason.UNSAFE
    elif not feed_ok:
        reason = AbstainReason.STALE_DATA
    elif clock_q == QStatus.FAIL:
        reason = AbstainReason.CLOCK_UNSYNC
    elif clob_q != QStatus.OK:
        reason = AbstainReason.CLOB_MISSING
    elif reference_q != QStatus.OK:
        reason = AbstainReason.PTB_MISSING
    elif model_q != QStatus.OK:
        reason = AbstainReason.MODEL_NOT_TRAINED

    return QualityReport(
        time=time_q, market=market_q, tokens=tokens_q, clob=clob_q,
        reference=reference_q, clock=clock_q, model=model_q,
        prediction_ready=prediction_ready, snapshot_recordable=snapshot_recordable,
        abstain_reason=reason, notes=notes,
    )
