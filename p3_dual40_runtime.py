"""Public production adapter for the DUAL40 state machine.

The hardened runtime implementation is kept in ``p3_dual40_runtime_impl``.  This
adapter fixes two production-contract mismatches without weakening its submit,
cancellation, balance-reconciliation, merge, collateral or hard-stop behaviour:

* P2.6 persists books in ``p26_clob_books`` and freshness is ordered by
  ``recv_ts_ms``;
* PAPER counts entry-time marketable limits and replays post-entry books so brief
  40-cent touches are not lost;
* PAPER waits for the official result when only one virtual leg fills and settles
  from the recorded fill price;
* the paper diagnostic reads the configured 41-cent near-touch threshold.
"""
from __future__ import annotations

from typing import Any

from p3_dual40_engine import _book_view, _levels
from p3_dual40_paper import visible_ask_capacity
from p3_dual40_runtime_impl import *  # noqa: F401,F403
from p3_dual40_runtime_impl import (
    ProductionDual40MakerEngine as _ProductionDual40MakerEngine,
)


class ProductionDual40MakerEngine(_ProductionDual40MakerEngine):
    """Production runtime bound to the actual P2.6 book-storage contract."""

    def __init__(self, settings, state, **kwargs):  # noqa: ANN001,ANN003
        super().__init__(settings, state, **kwargs)
        # Dual40Policy is frozen but intentionally not slotted.  The implementation
        # already consumes ``policy.near_touch_price`` as a diagnostic-only value;
        # attach the configured setting without changing any trading thresholds.
        object.__setattr__(
            self.policy,
            "near_touch_price",
            float(self.settings.dual40_near_touch_price),
        )

    def _latest_book(
        self,
        p26,
        condition_id: str,
        side: str,
    ) -> dict[str, Any] | None:  # noqa: ANN001
        """Read the freshest observed P2.6 book and executable ask capacity."""
        row = p26.execute(
            """
            SELECT id,condition_id,token_id,side,source_ts_ms,recv_ts_ms,
                   inserted_at_ms,bids_json,asks_json
            FROM p26_clob_books
            WHERE condition_id=? AND side=?
            ORDER BY recv_ts_ms DESC,id DESC LIMIT 1
            """,
            (str(condition_id), str(side).upper()),
        ).fetchone()
        view = _book_view(row)
        if view is None:
            return None
        asks = _levels(row["asks_json"])
        view["visible_ask_capacity_at_maker"] = visible_ask_capacity(
            asks,
            max_price=self.policy.price,
        )
        return view

    def _paper_touch_evidence(
        self,
        p26,
        cycle: dict[str, Any],
        side: str,
        now_ms: int,
    ) -> dict[str, Any] | None:  # noqa: ANN001
        """Find the first executable post-entry ask without replaying old rows."""
        side_value = str(side).upper()
        details = cycle.get("details") if isinstance(cycle.get("details"), dict) else {}
        cursor = int(details.get(f"paper_last_scanned_{side_value.lower()}_book_id") or 0)
        opened_ms = int(cycle.get("orders_posted_at_ms") or cycle["created_at_ms"])
        market_end_ms = int(cycle["market_end_ts_ms"])
        if market_end_ms < opened_ms:
            return None
        rows = p26.execute(
            """
            SELECT id,condition_id,token_id,side,source_ts_ms,recv_ts_ms,
                   inserted_at_ms,bids_json,asks_json
            FROM p26_clob_books
            WHERE condition_id=? AND side=? AND id>?
              AND recv_ts_ms>=? AND recv_ts_ms<=?
              AND source_ts_ms<=?
              AND (source_ts_ms>=? OR recv_ts_ms<=?)
            ORDER BY id ASC
            """,
            (
                str(cycle["condition_id"]),
                side_value,
                cursor,
                opened_ms,
                int(now_ms),
                market_end_ms,
                opened_ms,
                market_end_ms,
            ),
        ).fetchall()
        if not rows:
            return None

        last_scanned = max(int(row["id"]) for row in rows)
        best_view: dict[str, Any] | None = None
        first_executable: dict[str, Any] | None = None
        maker_price = float(cycle["maker_price"])
        for row in rows:
            view = _book_view(row)
            if view is None:
                continue
            if best_view is None or float(view["best_ask"]) < float(best_view["best_ask"]):
                best_view = view
            if (
                first_executable is None
                and float(view["best_ask"]) <= maker_price + 1e-12
            ):
                first_executable = view
        selected = first_executable or best_view
        if selected is None:
            return {
                "last_scanned_book_id": last_scanned,
                "book_id": None,
                "best_ask": None,
                "observed_at_ms": None,
                "touch_ts_ms": None,
                "source_ts_ms": None,
                "recv_ts_ms": None,
            }
        return {
            "last_scanned_book_id": last_scanned,
            "book_id": int(selected["id"]),
            "best_ask": float(selected["best_ask"]),
            "observed_at_ms": int(selected["recv_ts_ms"]),
            "touch_ts_ms": (
                int(selected["source_ts_ms"])
                if int(selected["source_ts_ms"]) >= opened_ms
                else int(selected["recv_ts_ms"])
            ),
            "source_ts_ms": int(selected["source_ts_ms"]),
            "recv_ts_ms": int(selected["recv_ts_ms"]),
        }
