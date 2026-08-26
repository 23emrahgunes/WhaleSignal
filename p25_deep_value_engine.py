"""P2.5 engine wrapper that evaluates DEEP_VALUE_WATCH on every market tick.

The parent engine still owns all research forecasts, validation, settlement and
paper analytics. This wrapper only calls the simulation recorder before each card is
rendered so a brief 10c/5c touch is not missed between canonical checkpoints.
"""
from __future__ import annotations

import logging

from p25_reconciled_paper_engine import P25Engine as _BaseP25Engine

log = logging.getLogger("direction_engine.p25.deep_value")


class P25Engine(_BaseP25Engine):
    def _card_p25(self, ref, snap, q, bundle, fv) -> dict:  # noqa: ANN001
        deep_enabled = bool(getattr(self.cfg, "paper_deep_value_enabled", False))
        if deep_enabled:
            watcher = getattr(self.recorder, "record_deep_value_watch", None)
            if callable(watcher):
                try:
                    watcher(ref, snap, bundle.trace)
                except Exception as exc:  # noqa: BLE001
                    # Paper research must never take the market-data engine down.
                    bundle.trace["paper_deep_value_watch_reason"] = (
                        f"WATCH_ERROR_{type(exc).__name__}"
                    )
                    log.warning(
                        "deep-value watch failed combo=%s error=%s",
                        ref.combo.key,
                        type(exc).__name__,
                    )

        card = super()._card_p25(ref, snap, q, bundle, fv)
        if deep_enabled:
            max_ask = float(getattr(self.cfg, "paper_deep_value_max_ask", 0.10))
            card["paper_entry_mode"] = "DEEP_VALUE_WATCH"
            card["paper_entry_checkpoint"] = f"{max_ask * 100:.0f}c DIP"
            card["paper_entry_label"] = f"DIP <= {max_ask * 100:.0f}c bekleniyor"
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
