"""Restart-safe DEEP_VALUE_WATCH paper recorder.

Only qualifying paper entries are persisted. Normal ticks above the configured dip
threshold are deliberately not written, so they cannot consume the one-shot market
entry before a later 10c/5c touch. Research forecasts continue to be recorded at the
normal checkpoints.
"""
from __future__ import annotations

import logging

from p25_deep_value import evaluate_deep_value_watch
from p25_reconciling_recorder import P25ReconcilingPaperRecorder
from p25_research_recorder import P25ResearchRecorder

log = logging.getLogger("direction_engine.paper.deep_value")


class P25DeepValuePaperRecorder(P25ReconcilingPaperRecorder):
    def __init__(self, db_path: str, cfg) -> None:  # noqa: ANN001
        self.deep_value_cfg = cfg
        super().__init__(db_path, cfg)

    def _ensure_paper_schema(self) -> None:
        super()._ensure_paper_schema()
        existing = {
            str(row[1])
            for row in self.conn.execute("PRAGMA table_info(paper_trades)").fetchall()
        }
        additions = {
            "entry_mode": "TEXT",
            "price_band": "TEXT",
            "depth_capacity_shares": "REAL",
            "depth_required_shares": "REAL",
            "depth_age_ms": "REAL",
            "depth_source": "TEXT",
            "fee_source": "TEXT",
            "value_multiple": "REAL",
        }
        for name, sql_type in additions.items():
            if name not in existing:
                self.conn.execute(
                    f"ALTER TABLE paper_trades ADD COLUMN {name} {sql_type}"
                )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_price_band "
            "ON paper_trades(strategy_version,price_band,status)"
        )
        self.conn.commit()

    def record_forecast(self, ref, snap, checkpoint: int, trace: dict) -> bool:  # noqa: ANN001
        """Keep checkpoint research forecasts, but disable checkpoint paper entry in deep mode."""
        if not bool(getattr(self.deep_value_cfg, "paper_deep_value_enabled", False)):
            return super().record_forecast(ref, snap, checkpoint, trace)
        return P25ResearchRecorder.record_forecast(
            self,
            ref,
            snap,
            checkpoint,
            trace,
        )

    def record_deep_value_watch(self, ref, snap, trace: dict) -> bool:  # noqa: ANN001
        if not bool(getattr(self.deep_value_cfg, "paper_deep_value_enabled", False)):
            return False
        if not ref.condition_id:
            trace["paper_deep_value_watch_reason"] = "CONDITION_ID_MISSING"
            return False

        # Paper scope is intentionally separate from the P2.5 discovery scope. P2.5
        # may keep 5m/15m/1h markets alive for forecasting and for P26/P3 registry
        # consumers while DEEP_VALUE_WATCH admits only the configured paper horizons.
        horizon = str(ref.combo.horizon.value).lower()
        allowed_horizons = self.deep_value_cfg.paper_deep_value_horizons()
        if horizon not in allowed_horizons:
            trace["paper_deep_value_watch_reason"] = f"HORIZON_{horizon.upper()}_NOT_ALLOWED"
            return False

        current = self.paper_trade_for_condition(ref.condition_id)
        if current is not None:
            trace.update(
                {
                    "paper_trade_status": current.get("status"),
                    "paper_trade_side": current.get("side"),
                    "paper_trade_fill": current.get("fill_price"),
                    "paper_trade_skip_reason": current.get("skip_reason"),
                    "paper_trade_checkpoint": current.get("checkpoint_sec"),
                    "paper_deep_value_watch_reason": "ALREADY_ATTEMPTED",
                }
            )
            return False

        decision, diag = evaluate_deep_value_watch(
            ref=ref,
            snap=snap,
            trace=trace,
            policy=self.paper_policy,
            cfg=self.deep_value_cfg,
            available_bankroll_usdc=self.available_paper_bankroll(),
        )
        trace["paper_deep_value_watch_reason"] = diag.get("reason")
        trace["paper_deep_value_price_band"] = diag.get("price_band")
        trace["paper_deep_value_depth_age_ms"] = diag.get("depth_age_ms")
        if decision is None or not decision.eligible:
            return False

        before = self.conn.total_changes
        self.conn.execute(
            """
            INSERT OR IGNORE INTO paper_trades (
                condition_id, market_id, combo_key, asset, horizon, slug,
                strategy_version, checkpoint_sec, attempted_at, entry_tte_sec,
                side, forecast_p_up, selected_probability, forecast_confidence,
                forecast_grade, forecast_status, forecast_agreement,
                entry_bid, entry_ask, fill_price, forecast_edge, stake_usdc,
                shares, slippage, fee_usdc, status, skip_reason,
                entry_mode, price_band, depth_capacity_shares,
                depth_required_shares, depth_age_ms, depth_source, fee_source,
                value_multiple
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ref.condition_id,
                ref.market_id,
                ref.combo.key,
                ref.combo.asset.value,
                ref.combo.horizon.value,
                ref.slug,
                self.paper_policy.strategy_version,
                0,
                snap.ts,
                snap.tte_sec if snap.tte_sec is not None else snap.seconds_remaining,
                decision.side,
                trace.get("forecast_p_up"),
                decision.selected_probability,
                trace.get("forecast_confidence"),
                trace.get("forecast_grade"),
                trace.get("forecast_status"),
                trace.get("forecast_agreement"),
                decision.entry_bid,
                decision.entry_ask,
                decision.fill_price,
                decision.forecast_edge,
                decision.stake_usdc,
                decision.shares,
                decision.slippage,
                decision.fee_usdc,
                "OPEN",
                None,
                "DEEP_VALUE_WATCH",
                diag.get("price_band"),
                diag.get("depth_capacity_shares"),
                diag.get("depth_required_shares"),
                diag.get("depth_age_ms"),
                diag.get("depth_source"),
                diag.get("fee_source"),
                diag.get("value_multiple"),
            ),
        )
        self.conn.commit()
        created = self.conn.total_changes > before
        if not created:
            return False

        trace.update(
            {
                "paper_trade_status": "OPEN",
                "paper_trade_side": decision.side,
                "paper_trade_fill": decision.fill_price,
                "paper_trade_skip_reason": None,
                "paper_trade_checkpoint": 0,
            }
        )
        log.info(
            "DEEP VALUE PAPER OPEN %s %s band=%s ask=%.4f fill=%.4f "
            "stake=%.2f shares=%.4f depth=%.2f age=%sms edge=%+.4f value=%.2fx fee=%.5f",
            ref.combo.key,
            decision.side,
            diag.get("price_band"),
            float(decision.entry_ask or 0.0),
            float(decision.fill_price or 0.0),
            float(decision.stake_usdc),
            float(decision.shares or 0.0),
            float(diag.get("depth_capacity_shares") or 0.0),
            int(diag.get("depth_age_ms") or 0),
            float(decision.forecast_edge or 0.0),
            float(diag.get("value_multiple") or 0.0),
            float(decision.fee_usdc or 0.0),
        )
        return True

    @staticmethod
    def _paper_row(row):  # noqa: ANN001
        data = P25ReconcilingPaperRecorder._paper_row(row)
        for key in (
            "depth_capacity_shares",
            "depth_required_shares",
            "depth_age_ms",
            "value_multiple",
        ):
            if data.get(key) is not None:
                data[key] = round(float(data[key]), 6)
        return data

    def paper_analytics(self, recent_limit=None) -> dict:  # noqa: ANN001
        payload = super().paper_analytics(recent_limit)
        payload["entry_mode"] = str(self.deep_value_cfg.paper_entry_mode).upper()
        payload["deep_value"] = {
            "enabled": bool(self.deep_value_cfg.paper_deep_value_enabled),
            "min_ask": float(self.deep_value_cfg.paper_deep_value_min_ask),
            "max_ask": float(self.deep_value_cfg.paper_deep_value_max_ask),
            "stake_usdc": float(self.paper_policy.stake_usdc),
            "min_tte_sec": float(self.deep_value_cfg.paper_deep_value_min_tte_sec),
            "max_book_age_ms": int(self.deep_value_cfg.paper_deep_value_max_book_age_ms),
            "require_depth": bool(self.deep_value_cfg.paper_deep_value_require_depth),
            "require_fee_schedule": bool(
                self.deep_value_cfg.paper_deep_value_require_fee_schedule
            ),
            "min_value_multiple": float(
                self.deep_value_cfg.paper_deep_value_min_value_multiple
            ),
            "allowed_horizons": sorted(self.deep_value_cfg.paper_deep_value_horizons()),
        }
        rows = self.conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE strategy_version=? AND price_band IS NOT NULL
            ORDER BY attempted_at ASC
            """,
            (self.paper_policy.strategy_version,),
        ).fetchall()
        bands = sorted({str(row["price_band"]) for row in rows})
        payload["per_price_band"] = {
            band: self._paper_metrics(
                row for row in rows if str(row["price_band"]) == band
            )
            for band in bands
        }
        return payload
