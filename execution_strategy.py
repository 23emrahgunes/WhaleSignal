"""Giris sinyali + tek-bacak (adverse selection) koruma mantigi.

Iki bileseni var:
1. `should_enter`  : konsolidasyon/simetrik-tahta filtresini uygular.
2. `AdverseSelectionGuard` : bir bacak dolup digeri T sn icinde dolmazsa acik
   emri iptal/hedge sinyali ureten durum makinesi.

Karar mantigi saf ve deterministiktir; runtime `main.py` bunlari surer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from analytics_engine import market_time_decay_ok
from config import Settings
from models import LegStatus, MarketState, Outcome


# ----------------------------------------------------------------------------
# Giris karari
# ----------------------------------------------------------------------------


@dataclass
class EntryDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)  # RED sebepleri (bos => giris)

    def __bool__(self) -> bool:
        return self.allowed


def should_enter(state: MarketState, cfg: Settings) -> EntryDecision:
    """Giris sartlari (hepsi saglanmali):

    |OBI| < OBI_MAX  AND  ATR_1m/fiyat < ATR_MAX_PCT  AND  ADX < ADX_MAX
    AND  kalan_sure > %TIME_DECAY_PCT  (opsiyonel: Bollinger squeeze / doyum).
    """
    reasons: list[str] = []
    a = state.analytics

    if not a.ready:
        reasons.append("VERI_HAZIR_DEGIL")
    if state.meta is None:
        reasons.append("MARKET_METADATA_YOK")
    elif not market_time_decay_ok(state.meta, state.now, cfg.time_decay_pct):
        reasons.append("VADE_SONU_%10")

    if abs(a.obi) >= cfg.obi_max:
        reasons.append(f"OBI_ASIMETRIK(|{a.obi:.3f}|>={cfg.obi_max})")
    if a.atr_pct >= cfg.atr_max_pct:
        reasons.append(f"ATR_YUKSEK({a.atr_pct:.4f}>={cfg.atr_max_pct})")
    if a.adx >= cfg.adx_max:
        reasons.append(f"ADX_TRENDLI({a.adx:.1f}>={cfg.adx_max})")
    if cfg.require_squeeze and not a.bb_squeeze:
        reasons.append("SQUEEZE_YOK")

    return EntryDecision(allowed=len(reasons) == 0, reasons=reasons)


# ----------------------------------------------------------------------------
# Tek-bacak / Adverse-Selection koruma durum makinesi
# ----------------------------------------------------------------------------


class GuardAction(str, Enum):
    NONE = "NONE"
    CANCEL_OPEN = "CANCEL_OPEN"  # acik (dolmayan) bacagi iptal et / riski kapat


@dataclass
class GuardResult:
    action: GuardAction
    missing_side: Optional[Outcome] = None
    elapsed: float = 0.0


class AdverseSelectionGuard:
    """Cift bacak dolum takibi.

    Kural: bir bacak (0.40) dolar, diger bacak `timeout_sec` icinde dolmazsa
    `CANCEL_OPEN` uretilir (acik emri cek, tek-yonlu riski kapat/hedge).
    """

    def __init__(self, timeout_sec: float) -> None:
        self.timeout_sec = timeout_sec
        self._up_ts: Optional[float] = None
        self._down_ts: Optional[float] = None
        self._triggered = False

    def reset(self) -> None:
        self._up_ts = None
        self._down_ts = None
        self._triggered = False

    def record_fill(self, side: Outcome, ts: float) -> None:
        if side == Outcome.UP:
            self._up_ts = ts
        else:
            self._down_ts = ts

    @property
    def status(self) -> LegStatus:
        up, down = self._up_ts is not None, self._down_ts is not None
        if up and down:
            return LegStatus.LOCKED
        if up or down:
            return LegStatus.ONE_LEG
        return LegStatus.RESTING

    def check(self, now: float) -> GuardResult:
        """Simdiki zamana gore koruma karari.

        Tam olarak BIR bacak dolu ve gecen sure >= timeout ise CANCEL_OPEN.
        """
        if self._triggered:
            return GuardResult(GuardAction.NONE)
        up, down = self._up_ts is not None, self._down_ts is not None
        if up == down:  # ikisi de dolu ya da ikisi de bos -> risk yok
            return GuardResult(GuardAction.NONE)
        filled_ts = self._up_ts if up else self._down_ts
        assert filled_ts is not None
        elapsed = now - filled_ts
        if elapsed >= self.timeout_sec:
            self._triggered = True
            missing = Outcome.DOWN if up else Outcome.UP
            return GuardResult(GuardAction.CANCEL_OPEN, missing_side=missing, elapsed=elapsed)
        return GuardResult(GuardAction.NONE)
