"""P3 replay clock correction for structural-arbitrage research.

P2.6 book rows carry three relevant clocks:

- source_ts_ms: exchange last-change timestamp (may stay old for resting quotes)
- recv_ts_ms: latest local observation of that exact persisted state; reconnect can
  advance this value for an unchanged row
- inserted_at_ms: immutable first local persistence time for the state

Historical as-of replay must use the immutable first-receive clock. Using recv_ts_ms
can move an old state forward after a reconnect and retroactively change history.

This module remains ex-post SHADOW/PAPER only and never submits orders.
"""
from __future__ import annotations

import json

from p3_replay import P3ReplayEngine as _BaseReplayEngine


REPLAY_VERSION = "P3_REPLAY_FIRST_RECV_ASOF_V3"


class P3ReplayEngine(_BaseReplayEngine):
    """Replay using immutable collector first-receive time."""

    def _future_book(self, condition_id: str, side: str, target_ms: int):  # noqa: ANN001
        return self.p26.execute(
            """
            SELECT * FROM p26_clob_books
            WHERE condition_id=? AND side=? AND inserted_at_ms<=?
            ORDER BY inserted_at_ms DESC,id DESC LIMIT 1
            """,
            (condition_id, side, int(target_ms)),
        ).fetchone()

    def _record(self, result) -> None:  # noqa: ANN001
        result.details["replay_version"] = REPLAY_VERSION
        result.details["time_axis"] = "inserted_at_ms_asof"
        super()._record(result)

    def purge_legacy_replays(self) -> int:
        """Remove rows produced under a different historical clock version.

        P3 is research-only. Recomputing the replay table is preferable to mixing
        incomparable clock semantics in the same pair-fill/PnL aggregates.
        """
        rows = self.p3.execute(
            "SELECT id,details_json FROM p3_replays"
        ).fetchall()
        stale_ids: list[int] = []
        for row in rows:
            try:
                details = json.loads(str(row["details_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {}
            if details.get("replay_version") != REPLAY_VERSION:
                stale_ids.append(int(row["id"]))
        if not stale_ids:
            return 0
        for start in range(0, len(stale_ids), 500):
            batch = stale_ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            self.p3.execute(
                f"DELETE FROM p3_replays WHERE id IN ({placeholders})", batch
            )
        self.p3.commit()
        return len(stale_ids)
