"""Runtime wrapper exposing paper-trade state and scorecards.

The parent engine continues to produce the always-on research forecast and the
validation-gated signal. This wrapper only surfaces simulation state from the
SQLite recorder; it cannot submit or sign orders.
"""
from __future__ import annotations

import os
import time

from p25_safety_engine import P25Engine as _BaseP25Engine


class P25Engine(_BaseP25Engine):
    def _card_p25(self, ref, snap, q, bundle, fv) -> dict:  # noqa: ANN001
        card = super()._card_p25(ref, snap, q, bundle, fv)

        # Optional paper-only high-frequency observer.  The engine already builds a
        # fresh snapshot/forecast every SNAPSHOT_LOOP_MS (500ms by default), so a
        # deep-value paper strategy can watch transient 3c/5c/15c dips without
        # persisting a snapshot or forecast row on every tick.  Canonical paper mode
        # has no observer and keeps its historical behavior.
        observer = getattr(self.recorder, "observe_paper_tick", None)
        if callable(observer):
            observer(
                ref=ref,
                snap=snap,
                trace=bundle.trace,
                data_ready=self._data_ready(q),
            )

        getter = getattr(self.recorder, "paper_trade_for_condition", None)
        paper = getter(ref.condition_id) if callable(getter) else None
        card["paper_trade"] = paper
        if hasattr(self.cfg, "paper_entry_mode_normalized"):
            card["paper_entry_mode"] = self.cfg.paper_entry_mode_normalized()
        if hasattr(self.cfg, "paper_entry_checkpoint"):
            card["paper_entry_checkpoint"] = self.cfg.paper_entry_checkpoint(
                ref.combo.horizon.value
            )
        return card

    @staticmethod
    def _paper_analytics_cache_sec() -> float:
        raw = os.getenv("P25_PAPER_ANALYTICS_CACHE_SEC", "30")
        try:
            return max(1.0, float(raw))
        except ValueError:
            return 30.0

    @staticmethod
    def _empty_paper_payload() -> dict:
        return {
            "enabled": False,
            "paper_only": True,
            "overall": {},
            "per_asset": {},
            "per_horizon": {},
            "per_combo": {},
            "recent_markets": [],
            "open_positions": [],
        }

    def _paper_analytics_cached(self) -> dict:
        """Return paper analytics without re-scanning SQLite on every poll."""
        now = time.monotonic()
        cached = getattr(self, "_paper_analytics_cache", None)
        cached_at = float(
            getattr(self, "_paper_analytics_cache_at", 0.0) or 0.0
        )
        if (
            cached is not None
            and now - cached_at < self._paper_analytics_cache_sec()
        ):
            return cached

        analytics_fn = getattr(self.recorder, "paper_analytics", None)
        paper = (
            analytics_fn(getattr(self.cfg, "paper_recent_limit", 50))
            if callable(analytics_fn)
            else self._empty_paper_payload()
        )
        self._paper_analytics_cache = paper
        self._paper_analytics_cache_at = now
        return paper

    def snapshot(self) -> dict:
        data = super().snapshot()
        paper = self._paper_analytics_cached()
        data["paper_trading"] = paper
        overall = paper.get("overall") or {}
        footer = data.setdefault("footer", {})
        footer.update(
            {
                "paper_attempts": overall.get("attempts", 0),
                "paper_open": overall.get("open", 0),
                "paper_settled": overall.get("settled", 0),
                "paper_skipped": overall.get("skipped", 0),
                "paper_wins": overall.get("wins", 0),
                "paper_losses": overall.get("losses", 0),
                "paper_hit_rate": overall.get("hit_rate"),
                "paper_realized_pnl_usdc": overall.get(
                    "realized_pnl_usdc", 0.0
                ),
                "paper_equity_usdc": overall.get("equity_usdc"),
            }
        )
        safety = data.setdefault("safety", {})
        safety.update(
            {
                "paper_trading_enabled": bool(paper.get("enabled")),
                "paper_only": True,
                "paper_order_submissions": 0,
            }
        )
        return data
