"""Public production adapter for the DUAL40 state machine.

The hardened runtime implementation is kept in ``p3_dual40_runtime_impl``.  This
adapter fixes two production-contract mismatches without weakening its submit,
cancellation, balance-reconciliation, merge, collateral or hard-stop behaviour:

* P2.6 persists books in ``p26_clob_books`` and freshness is ordered by
  ``recv_ts_ms``;
* the paper diagnostic reads the configured 41-cent near-touch threshold from
  ``P3Settings``.
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
