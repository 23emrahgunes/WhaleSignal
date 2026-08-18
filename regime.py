"""Rejim + predictability motoru — YONDEN ONCE calisir.

"Bu market su an tahmin edilebilir mi?" sorusu. Once rejim (volatilite/mikroyapi
karakteri) siniflandirilir; sonra predictability_score [0,1] uretilir. Guvensiz
kosullarda (CHAOTIC / HIGH_VOL / UNSAFE / feature-conflict / dusuk predictability)
karar ABSTAIN olur — model yon bile denemez.

Saf/kural-tabanli (agirlik ogrenmesi model_direction'da); ag yok, test edilebilir.
Esikler config'ten degil sabit-mantik degil — makul varsayilanlar; P3'te veriyle
ayarlanabilir.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from features import FeatureVector
from models import AbstainReason, Regime


@dataclass
class RegimeResult:
    regime: Regime
    predictability: float  # 0..1
    abstain: bool
    abstain_reason: AbstainReason
    reasons: list[str] = field(default_factory=list)


# makul varsayilan esikler (P3'te veriyle kalibre edilebilir)
VOL_PCT_HIGH = 0.92        # rv_fast percentile bu ustunde -> yuksek vol
CHAOS_FLIP = 0.6           # flip_rate bu ustunde -> kaos
CHAOS_VOL_ACCEL = 2.5      # rv_fast/rv_slow bu ustunde -> vol patlamasi
TREND_PERSIST = 0.66       # sign_persistence bu ustunde -> trend
TREND_MAX_FLIP = 0.4
PREDICTABILITY_MIN = 0.45  # bunun altinda -> ABSTAIN(LOW_PREDICTABILITY)


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _feature_conflict(fv: FeatureVector) -> bool:
    """Ana yon sinyalleri birbirini gucluce yalanliyor mu?

    momentum (ret_slow), agresif flow (flow_mid) ve PTB mesafesi (distance_bps)
    isaretleri; ikisi gucluce zit ise conflict -> ABSTAIN.
    """
    signals = []
    if abs(fv.ret_slow) > 1e-6:
        signals.append(("mom", 1 if fv.ret_slow > 0 else -1, abs(fv.ret_slow)))
    if abs(fv.flow_mid) > 0.1:
        signals.append(("flow", 1 if fv.flow_mid > 0 else -1, abs(fv.flow_mid)))
    if fv.has_reference and abs(fv.distance_bps) > 0.5:
        signals.append(("ptb", 1 if fv.distance_bps > 0 else -1, abs(fv.distance_bps)))
    strong = [s for s in signals if s[2] > 0]
    if len(strong) < 2:
        return False
    signs = {s[1] for s in strong}
    return len(signs) > 1 and len(strong) >= 2 and _mixed_strength(strong)


def _mixed_strength(strong: list) -> bool:
    """Zit isaretli en az iki sinyal de 'kayda deger' guclu mu?"""
    pos = [s for s in strong if s[1] > 0]
    neg = [s for s in strong if s[1] < 0]
    return len(pos) >= 1 and len(neg) >= 1


def classify_regime(fv: FeatureVector) -> RegimeResult:
    reasons: list[str] = []

    # UNSAFE: yeterli ham veri yok (returns/vol sifir gibi)
    if fv.rv_slow <= 0 and fv.rv_fast <= 0 and fv.ret_slow == 0.0:
        return RegimeResult(
            Regime.UNSAFE, 0.0, True, AbstainReason.INSUFFICIENT_DATA, ["ham veri yetersiz"]
        )

    # HIGH_VOL: volatilite persentili asiri yuksek
    if fv.vol_percentile >= VOL_PCT_HIGH:
        reasons.append(f"vol_pct={fv.vol_percentile:.2f} yuksek")
        return RegimeResult(Regime.HIGH_VOL, 0.15, True, AbstainReason.HIGH_VOL, reasons)

    # CHAOTIC: yuksek flip + vol patlamasi
    if fv.flip_rate >= CHAOS_FLIP and fv.vol_accel >= CHAOS_VOL_ACCEL:
        reasons.append(f"flip={fv.flip_rate:.2f} vol_accel={fv.vol_accel:.2f}")
        return RegimeResult(Regime.CHAOTIC, 0.1, True, AbstainReason.CHAOTIC, reasons)

    # feature conflict
    if _feature_conflict(fv):
        reasons.append("feature conflict (momentum/flow/PTB zit)")
        return RegimeResult(
            Regime.CHOP, 0.2, True, AbstainReason.FEATURE_CONFLICT, reasons
        )

    # TREND vs CHOP
    is_trend = fv.sign_persistence >= TREND_PERSIST and fv.flip_rate <= TREND_MAX_FLIP
    if is_trend and fv.ret_slow != 0.0:
        regime = Regime.TREND_UP if fv.ret_slow > 0 else Regime.TREND_DOWN
        reasons.append(f"persist={fv.sign_persistence:.2f} flip={fv.flip_rate:.2f}")
    else:
        regime = Regime.CHOP
        reasons.append("yatay/karisik")

    # predictability_score: momentum tutarliligi + flow tutarliligi + dusuk flip +
    # olculu vol + book/flow uyumu. Normalize agirliksiz ortalama.
    comps = [
        fv.sign_persistence,
        fv.flow_persistence,
        1.0 - fv.flip_rate,
        1.0 - min(1.0, abs(fv.vol_percentile - 0.5) * 2),  # orta vol tercih
        _clip01(0.5 + 0.5 * fv.book_flow_agree),
    ]
    predictability = _clip01(sum(comps) / len(comps))

    if predictability < PREDICTABILITY_MIN:
        reasons.append(f"predictability={predictability:.2f} dusuk")
        return RegimeResult(
            regime, predictability, True, AbstainReason.LOW_PREDICTABILITY, reasons
        )

    return RegimeResult(regime, predictability, False, AbstainReason.NONE, reasons)
