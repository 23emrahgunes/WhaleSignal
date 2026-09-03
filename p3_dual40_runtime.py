"""Production runtime wrapper for the DUAL40 state machine."""
from __future__ import annotations

import json
import time
from typing import Any

from p3_dual40_analytics import build_dual40_summary
from p3_dual40_engine import Dual40MakerEngine
from p3_dual40_preflight import run_dual40_preflight
from p3_schema import open_p26_read_only


class ProductionDual40MakerEngine(Dual40MakerEngine):
    """Use the real P2.6 collector-health contract and local official labels."""

    def __init__(self, settings, state, **kwargs):  # noqa: ANN001,ANN003
        kwargs.setdefault("preflight_fn", run_dual40_preflight)
        super().__init__(settings, state, **kwargs)

    @staticmethod
    def _transport_status(p26) -> dict[str, Any]:  # noqa: ANN001
        row = p26.execute(
            "SELECT value FROM p26_meta WHERE key='book_collector_health_json'"
        ).fetchone()
        if row is None:
            return {}
        try:
            raw = json.loads(str(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        heartbeat = int(raw.get("heartbeat_ts_ms") or 0)
        last_message = int(raw.get("last_message_recv_ms") or 0)
        return {
            **raw,
            "connected": bool(raw.get("connected")),
            "last_receive_ms": max(heartbeat, last_message),
        }

    def _fetch_official_result(self, cycle: dict[str, Any]) -> tuple[str | None, str]:
        p26 = open_p26_read_only(self.settings.p26_db_path)
        try:
            row = p26.execute(
                """
                SELECT official_label,official_result_source,official_resolved_at_ms
                FROM p26_labels WHERE condition_id=?
                """,
                (str(cycle["condition_id"]),),
            ).fetchone()
        finally:
            p26.close()
        if row is not None and row["official_label"] in (0, 1):
            side = "UP" if int(row["official_label"]) == 1 else "DOWN"
            source = str(row["official_result_source"] or "P26_OFFICIAL_LABEL")
            return side, f"P26:{source}"
        return super()._fetch_official_result(cycle)

    def public_status(self) -> dict[str, Any]:
        payload = build_dual40_summary(self.settings.p3_db_path, limit=100)
        payload.update(
            {
                "policy": {
                    "price": self.policy.price,
                    "ladder": list(self.policy.ladder),
                    "pair_edge_per_share": self.policy.pair_edge_per_share,
                    "full_ladder_capital_usdc": self.policy.full_ladder_capital,
                    "minimum_live_collateral_usdc": float(
                        self.settings.dual40_min_collateral_to_arm_usdc
                    ),
                    "hard_stop_after_30": True,
                    "one_global_market_only": True,
                    "paper_fill_rule": "BEST_ASK_LE_40",
                    "near_touch_41_diagnostic_only": True,
                    "entry": "BALANCED_STABLE_TWO_WAY",
                    "market_age_sec": self.policy.min_market_age_sec,
                    "lookback_sec": self.policy.lookback_sec,
                    "confirm_sec": self.policy.confirm_sec,
                    "balanced_mid": [
                        self.policy.balanced_mid_low,
                        self.policy.balanced_mid_high,
                    ],
                    "cancel_tte_sec": self.policy.cancel_tte_sec,
                },
                "runtime": self._last_status,
                "generated_at_ms": int(time.time() * 1000),
            }
        )
        return payload
