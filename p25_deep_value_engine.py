"""P2.5 DEEP_VALUE_WATCH engine with an optional one-shot XRP 5m LIVE pilot.

Paper evaluation remains the primary path. LIVE is invoked only after the recorder
has successfully created the exact paper OPEN row, so the pilot cannot invent a
separate signal path or silently trade a different cohort.

When PAPER_INDEPENDENT_ALPHA_ENABLED=true, the paper probability is replaced by the
independent PTB+Binance experiment. The normal research forecast remains visible and
recorded for comparison; Polymarket prices never enter the independent probability.
STRICT V1 can additionally require terminal-window timing and continuous signal
stability before the recorder is allowed to evaluate value/depth.
"""
from __future__ import annotations

import logging
import time

from p25_independent_alpha import build_independent_alpha
from p25_reconciled_paper_engine import P25Engine as _BaseP25Engine
from p25_signal_stability import SignalStabilityGate

log = logging.getLogger("direction_engine.p25.deep_value")


def _entry_window_reason(cfg, ref, snap) -> str | None:  # noqa: ANN001
    """Return a fail-closed reason unless a 5m market is inside its entry window."""
    horizon = str(ref.combo.horizon.value).lower()
    if horizon != "5m":
        return None
    tte = snap.tte_sec if snap.tte_sec is not None else snap.seconds_remaining
    if tte is None:
        return "ENTRY_TTE_MISSING"
    tte = float(tte)
    min_tte = float(getattr(cfg, "paper_deep_value_entry_tte_min_sec", 60.0))
    max_tte = float(getattr(cfg, "paper_deep_value_entry_tte_max_sec", 90.0))
    if tte > max_tte + 1e-9:
        return f"WAITING_FOR_ENTRY_WINDOW_TTE_{tte:.1f}"
    if tte + 1e-9 < min_tte:
        return f"ENTRY_WINDOW_CLOSED_TTE_{tte:.1f}"
    return None


def _independent_paper_trace(cfg, ref, snap, fv, trace):  # noqa: ANN001
    """Return (paper_trace, reject_reason, alpha_dict).

    The original trace is never overwritten: research forecast analytics remain an
    apples-to-apples control cohort. Only the copy passed to the paper recorder uses
    the independent probability.
    """
    if not bool(getattr(cfg, "paper_independent_alpha_enabled", False)):
        return trace, None, None

    alpha = build_independent_alpha(ref=ref, snap=snap, fv=fv, cfg=cfg)
    alpha_dict = alpha.to_dict()
    trace["independent_alpha"] = alpha_dict
    trace["independent_alpha_source"] = alpha.source
    trace["independent_alpha_direction"] = alpha.direction
    trace["independent_alpha_p_up"] = alpha.p_up
    trace["independent_alpha_confidence"] = alpha.confidence
    trace["independent_alpha_reason"] = alpha.reason

    if not alpha.ready:
        return None, f"INDEPENDENT_ALPHA_{alpha.reason}", alpha_dict
    if alpha.direction == "NEUTRAL":
        return None, "INDEPENDENT_ALPHA_NEUTRAL", alpha_dict
    if alpha.p_up is None:
        return None, "INDEPENDENT_ALPHA_PROBABILITY_MISSING", alpha_dict

    paper_trace = dict(trace)
    paper_trace.update(
        {
            "forecast_direction": alpha.direction,
            "forecast_p_up": float(alpha.p_up),
            "forecast_confidence": float(alpha.confidence),
            "forecast_grade": alpha.grade,
            "forecast_status": "PROVISIONAL",
            "forecast_source": alpha.source,
            # This probability is not a vote ensemble. Keep compatibility with the
            # recorder schema; strict deploy sets PAPER_MIN_AGREEMENT=0.
            "forecast_agreement": 1.0,
        }
    )
    return paper_trace, None, alpha_dict


class P25Engine(_BaseP25Engine):
    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002,ANN003
        super().__init__(*args, **kwargs)
        self._xrp5m_live_pilot = None
        self._strict_stability = SignalStabilityGate(
            required_sec=float(getattr(self.cfg, "paper_strict_stability_sec", 3.0)),
            max_gap_sec=float(
                getattr(self.cfg, "paper_strict_stability_max_gap_sec", 1.5)
            ),
        )

    def attach_xrp5m_live_pilot(self, pilot) -> None:  # noqa: ANN001
        self._xrp5m_live_pilot = pilot

    def xrp5m_live_pilot(self):  # noqa: ANN201
        return self._xrp5m_live_pilot

    @staticmethod
    def _stability_key(ref) -> str:  # noqa: ANN001
        return str(getattr(ref, "condition_id", None) or getattr(ref, "market_id", ""))

    def _strict_stability_reason(self, ref, snap, alpha_dict) -> str | None:  # noqa: ANN001
        if not bool(getattr(self.cfg, "paper_strict_entry_enabled", False)):
            return None
        key = self._stability_key(ref)
        now = float(getattr(snap, "ts", None) or time.time())
        self._strict_stability.prune(now)
        if not alpha_dict or not bool(alpha_dict.get("ready")):
            self._strict_stability.reset(key)
            return "INDEPENDENT_ALPHA_STABILITY_RESET"
        side = str(alpha_dict.get("direction") or "").upper()
        stable, elapsed = self._strict_stability.observe(key, side, now)
        required = float(getattr(self.cfg, "paper_strict_stability_sec", 3.0))
        alpha_dict["stability_elapsed_sec"] = round(elapsed, 3)
        alpha_dict["stability_required_sec"] = required
        alpha_dict["stability_pass"] = stable
        if not stable:
            return f"INDEPENDENT_ALPHA_STABILITY_{elapsed:.1f}_LT_{required:.1f}"
        return None

    def _card_p25(self, ref, snap, q, bundle, fv) -> dict:  # noqa: ANN001
        deep_enabled = bool(getattr(self.cfg, "paper_deep_value_enabled", False))
        independent_enabled = bool(
            getattr(self.cfg, "paper_independent_alpha_enabled", False)
        )
        paper_created = False
        paper_trace = bundle.trace
        alpha_reject = None
        alpha_dict = None

        # Compute/track the independent alpha before the entry-window gate. This is
        # intentional: T-75 may open with a signal that has already remained stable
        # for >=3 seconds instead of waiting three additional seconds after T-75.
        if deep_enabled and independent_enabled:
            paper_trace, alpha_reject, alpha_dict = _independent_paper_trace(
                self.cfg,
                ref,
                snap,
                fv,
                bundle.trace,
            )
            if alpha_reject is None:
                stability_reject = self._strict_stability_reason(ref, snap, alpha_dict)
                if stability_reject is not None:
                    alpha_reject = stability_reject
            elif bool(getattr(self.cfg, "paper_strict_entry_enabled", False)):
                self._strict_stability.reset(self._stability_key(ref))

        if deep_enabled:
            window_reason = _entry_window_reason(self.cfg, ref, snap)
            if window_reason is not None:
                bundle.trace["paper_deep_value_watch_reason"] = window_reason
            elif alpha_reject is not None:
                bundle.trace["paper_deep_value_watch_reason"] = alpha_reject
            else:
                watcher = getattr(self.recorder, "record_deep_value_watch", None)
                if callable(watcher) and paper_trace is not None:
                    try:
                        paper_created = bool(watcher(ref, snap, paper_trace))
                        for key in (
                            "paper_deep_value_watch_reason",
                            "paper_deep_value_price_band",
                            "paper_deep_value_depth_age_ms",
                            "paper_deep_value_scan_mode",
                            "paper_deep_value_depth_min_multiple",
                            "paper_trade_status",
                            "paper_trade_side",
                            "paper_trade_fill",
                            "paper_trade_skip_reason",
                            "paper_trade_checkpoint",
                        ):
                            if key in paper_trace:
                                bundle.trace[key] = paper_trace[key]
                    except Exception as exc:  # noqa: BLE001
                        bundle.trace["paper_deep_value_watch_reason"] = (
                            f"WATCH_ERROR_{type(exc).__name__}"
                        )
                        log.warning(
                            "deep-value watch failed combo=%s error=%s",
                            ref.combo.key,
                            type(exc).__name__,
                        )

        if paper_created and self._xrp5m_live_pilot is not None:
            try:
                getter = getattr(self.recorder, "paper_trade_for_condition", None)
                paper = getter(ref.condition_id) if callable(getter) else None
                self._xrp5m_live_pilot.submit_async(ref, paper)
            except Exception as exc:  # noqa: BLE001
                bundle.trace["p25_live_trigger_reason"] = (
                    f"LIVE_TRIGGER_ERROR_{type(exc).__name__}"
                )
                log.exception("XRP5m LIVE trigger failed")

        card = super()._card_p25(ref, snap, q, bundle, fv)
        if deep_enabled:
            min_ask = float(getattr(self.cfg, "paper_deep_value_min_ask", 0.01))
            max_ask = float(getattr(self.cfg, "paper_deep_value_max_ask", 0.10))
            stake = float(getattr(self.cfg, "paper_stake_usdc", 1.0))
            slippage = float(getattr(self.cfg, "paper_slippage", 0.005))
            min_value = float(
                getattr(self.cfg, "paper_deep_value_min_value_multiple", 1.50)
            )
            entry_min = float(
                getattr(self.cfg, "paper_deep_value_entry_tte_min_sec", 60.0)
            )
            entry_max = float(
                getattr(self.cfg, "paper_deep_value_entry_tte_max_sec", 90.0)
            )
            alpha = bundle.trace.get("independent_alpha") or alpha_dict or {}
            card["paper_entry_mode"] = "DEEP_VALUE_WATCH"
            card["paper_entry_checkpoint"] = (
                f"T-{entry_max:.0f}..T-{entry_min:.0f}s + {max_ask * 100:.0f}c DIP"
            )
            card["paper_entry_label"] = (
                f"Giriş yalnız son {entry_max:.0f}-{entry_min:.0f}s penceresinde"
            )
            card["paper_deep_value_min_ask"] = min_ask
            card["paper_deep_value_max_ask"] = max_ask
            card["paper_deep_value_stake_usdc"] = stake
            card["paper_deep_value_slippage"] = slippage
            card["paper_deep_value_min_value_multiple"] = min_value
            card["paper_deep_value_min_depth_multiple"] = float(
                getattr(self.cfg, "paper_deep_value_min_depth_multiple", 1.0)
            )
            card["paper_deep_value_entry_tte_min_sec"] = entry_min
            card["paper_deep_value_entry_tte_max_sec"] = entry_max
            card["paper_deep_value_watch_reason"] = bundle.trace.get(
                "paper_deep_value_watch_reason"
            )
            card["paper_deep_value_price_band"] = bundle.trace.get(
                "paper_deep_value_price_band"
            )
            card["paper_deep_value_depth_age_ms"] = bundle.trace.get(
                "paper_deep_value_depth_age_ms"
            )
            card["paper_deep_value_scan_mode"] = bundle.trace.get(
                "paper_deep_value_scan_mode"
            )
            card["paper_independent_alpha_enabled"] = independent_enabled
            card["paper_strict_entry_enabled"] = bool(
                getattr(self.cfg, "paper_strict_entry_enabled", False)
            )
            card["independent_alpha"] = alpha
            card["paper_probability_source"] = (
                alpha.get("source") if independent_enabled else card.get("forecast_source")
            )
            card["paper_p_up"] = (
                alpha.get("p_up") if independent_enabled else card.get("forecast_p_up")
            )
            card["paper_direction"] = (
                alpha.get("direction") if independent_enabled else card.get("forecast_direction")
            )
            card["paper_alpha_stability_sec"] = alpha.get("stability_elapsed_sec")
            card["paper_alpha_stability_pass"] = alpha.get("stability_pass")
        return card

    def snapshot(self) -> dict:
        data = super().snapshot()
        if self._xrp5m_live_pilot is not None:
            live = self._xrp5m_live_pilot.status()
        else:
            live = {
                "feature_enabled": False,
                "armed": False,
                "scope": "XRP:5m",
                "max_stake_usdc": 1.10,
                "max_price_drift_pct": 0.10,
                "one_cycle_per_arm": True,
                "arm_consumed": False,
                "network_cycles": 0,
            }
        data["xrp5m_live_pilot"] = live
        safety = data.setdefault("safety", {})
        strict = bool(getattr(self.cfg, "paper_strict_entry_enabled", False))
        safety["paper_independent_alpha_enabled"] = bool(
            getattr(self.cfg, "paper_independent_alpha_enabled", False)
        )
        safety["paper_independent_alpha_source"] = (
            "INDEPENDENT_PTB_BINANCE_STRICT_V1"
            if strict
            else "INDEPENDENT_PTB_BINANCE_V1"
        )
        safety["paper_strict_entry_enabled"] = strict
        safety["paper_strict_profile"] = {
            "entry_tte_min_sec": float(
                getattr(self.cfg, "paper_deep_value_entry_tte_min_sec", 60.0)
            ),
            "entry_tte_max_sec": float(
                getattr(self.cfg, "paper_deep_value_entry_tte_max_sec", 90.0)
            ),
            "deadzone_low": float(
                getattr(self.cfg, "paper_independent_deadzone_low", 0.42)
            ),
            "deadzone_high": float(
                getattr(self.cfg, "paper_independent_deadzone_high", 0.58)
            ),
            "min_abs_z": float(getattr(self.cfg, "paper_strict_min_abs_z", 0.45)),
            "max_counter_sigma": float(
                getattr(self.cfg, "paper_strict_max_counter_sigma", 0.10)
            ),
            "stability_sec": float(
                getattr(self.cfg, "paper_strict_stability_sec", 3.0)
            ),
            "max_book_age_ms": int(
                getattr(self.cfg, "paper_deep_value_max_book_age_ms", 1500)
            ),
            "min_depth_multiple": float(
                getattr(self.cfg, "paper_deep_value_min_depth_multiple", 1.0)
            ),
            "require_official_current": bool(
                getattr(self.cfg, "paper_strict_require_official_current", True)
            ),
            "direction_lock": bool(
                getattr(self.cfg, "paper_strict_direction_lock", True)
            ),
        }
        armed_ready = bool(live.get("armed")) and not bool(live.get("arm_consumed"))
        safety["p25_direction_live_feature_enabled"] = bool(
            live.get("feature_enabled")
        )
        safety["p25_direction_live_armed"] = bool(live.get("armed"))
        safety["p25_direction_live_arm_consumed"] = bool(
            live.get("arm_consumed")
        )
        safety["execution_enabled"] = armed_ready
        safety["live_orders"] = int(live.get("network_cycles") or 0)
        return data
