"""P3 LIVE V3: fresh pair economics + newest-confirmed candidate selection.

V3 intentionally subclasses the battle-tested V2 execution/verification/unwind
pipeline. It changes three pre-submit behaviours:

1. the gateway reprices the current UP+DOWN pair from fresh full-depth books instead
   of enforcing historical per-leg scanner prices;
2. candidate lookup searches newest windows first with a wider bounded horizon so a
   large prefix of unconfirmed windows cannot starve a newer confirmed opportunity;
3. a small historical STRICT quantity does not permanently cap LIVE below the market
   minimum order size. The current target quantity is allowed to be revalidated from
   fresh depth/economics, while hard max, edge, notional and unwind gates still apply.

All V2 FOK submission, one-leg unwind, rolling loss, preflight, collateral and
one-network-cycle-per-arm behaviour remain unchanged.
"""
from __future__ import annotations

from typing import Any, Callable

from p3_confirmation import CONFIRMED, select_confirmed_observation
from p3_config import P3Settings
from p3_live_executor_v2 import P3LiveExecutorV2
from p3_live_gateway_fresh import FreshEconomicPolymarketLiveGateway
from p3_live_preflight import run_live_preflight
from p3_live_state import LiveState


class P3LiveExecutorV3(P3LiveExecutorV2):
    """Use fresh pair-level economics while preserving all V2 safety gates."""

    def __init__(
        self,
        settings: P3Settings,
        state: LiveState,
        *,
        gateway_factory: Callable[[P3Settings], Any] = FreshEconomicPolymarketLiveGateway,
        preflight_fn: Callable[..., dict[str, Any]] = run_live_preflight,
    ) -> None:
        super().__init__(
            settings,
            state,
            gateway_factory=gateway_factory,
            preflight_fn=preflight_fn,
        )

    def _next_candidate(
        self,
        conn,
        *,
        session_id: str,
        armed_at_ms: int,
    ) -> dict[str, Any] | None:  # noqa: ANN001
        # LIVE wants the freshest surviving opportunity. V2 read the oldest 200
        # windows first; a long prefix of unconfirmed windows could permanently hide
        # a later confirmed one. Search newest-first and widen the bounded scan.
        rows = conn.execute(
            """
            SELECT id,strategy,condition_id,combo_key,opened_ts_ms
            FROM p3_windows
            WHERE opened_ts_ms>=?
            ORDER BY opened_ts_ms DESC,id DESC
            LIMIT 2000
            """,
            (int(armed_at_ms),),
        ).fetchall()
        for window in rows:
            window_id = int(window["id"])
            if self._cycle_exists(conn, session_id=session_id, window_id=window_id):
                continue
            selection = select_confirmed_observation(
                conn,
                window_id=window_id,
                confirm_ms=int(self.settings.dry_entry_confirm_ms),
                max_gap_ms=int(self.settings.dry_confirm_max_gap_ms),
            )
            if selection.status != CONFIRMED or selection.entry_ts_ms is None:
                continue
            if int(selection.entry_ts_ms) < int(armed_at_ms):
                continue
            row = conn.execute(
                "SELECT * FROM p3_opportunities WHERE id=?",
                (int(selection.opportunity_id),),
            ).fetchone()
            if row is None:
                continue

            opp = dict(row)
            strict_quantity = float(opp.get("quantity_shares") or 0.0)
            # V2's sizing helper still takes `strict_optimal_shares` as one cap. For
            # V3 the fresh book is authoritative at submit time, so permit the
            # configured target to be tested even when the old STRICT snapshot only
            # supported fewer shares. The helper still applies target, hard max and
            # both fresh capacities; subsequent fresh economics/unwind gates must pass.
            opp["strict_quantity_shares"] = strict_quantity
            opp["quantity_shares"] = max(
                strict_quantity,
                float(self.settings.live_target_quantity_shares),
            )
            return {
                "window": dict(window),
                "selection": selection,
                "opportunity": opp,
            }
        return None
