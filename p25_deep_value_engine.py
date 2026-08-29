"""P2.5 DEEP_VALUE_WATCH engine with an optional one-shot XRP 5m LIVE pilot.

Paper evaluation remains the primary path.  LIVE is invoked only after the recorder
has successfully created the exact paper OPEN row, so the first pilot cannot invent
a separate signal path or silently trade a different cohort.
"""
from __future__ import annotations

import logging

from p25_reconciled_paper_engine import P25Engine as _BaseP25Engine

log = logging.getLogger("direction_engine.p25.deep_value")


class P25Engine(_BaseP25Engine):
    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002,ANN003
        super().__init__(*args, **kwargs)
        self._xrp5m_live_pilot = None

    def attach_xrp5m_live_pilot(self, pilot) -> None:  # noqa: ANN001
        self._xrp5m_live_pilot = pilot

    def _card_p25(self, ref, snap, q, bundle, fv) -> dict:  # noqa: ANN001
        deep_enabled = bool(getattr(self.cfg, "paper_deep_value_enabled", False))
        paper_created = False
        if deep_enabled:
            watcher = getattr(self.recorder, "record_deep_value_watch", None)
            if callable(watcher):
                try:
                    paper_created = bool(watcher(ref, snap, bundle.trace))
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
            card["paper_entry_mode"] = "DEEP_VALUE_WATCH"
            card["paper_entry_checkpoint"] = f"{max_ask * 100:.0f}c DIP"
            card["paper_entry_label"] = f"DIP <= {max_ask * 100:.0f}c bekleniyor"
            card["paper_deep_value_min_ask"] = min_ask
            card["paper_deep_value_max_ask"] = max_ask
            card["paper_deep_value_stake_usdc"] = stake
            card["paper_deep_value_slippage"] = slippage
            card["paper_deep_value_min_value_multiple"] = min_value
            card["paper_deep_value_watch_reason"] = bundle.trace.get(
                "paper_deep_value_watch_reason"
            )
            card["paper_deep_value_price_band"] = bundle.trace.get(
                "paper_deep_value_price_band"
            )
            card["paper_deep_value_depth_age_ms"] = bundle.trace.get(
                "paper_deep_value_depth_age_ms"
            )
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
                "one_cycle_per_arm": True,
                "arm_consumed": False,
            }
        data["xrp5m_live_pilot"] = live
        safety = data.setdefault("safety", {})
        safety["p25_direction_live_feature_enabled"] = bool(
            live.get("feature_enabled")
        )
        safety["p25_direction_live_armed"] = bool(live.get("armed"))
        safety["p25_direction_live_arm_consumed"] = bool(
            live.get("arm_consumed")
        )
        return data
