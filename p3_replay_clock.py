"""P3 replay clock correction for live structural-arbitrage research.

Opportunity detection timestamps are wall-clock/collector times. P2.6 book rows
carry two clocks:

- source_ts_ms: exchange last-change timestamp (may stay old for resting quotes)
- recv_ts_ms: when our collector observed that book state

The original P3.4 replay searched future books on source_ts_ms, so resting books
produced false NO_SYNCHRONOUS_BOOK outcomes even while the arbitrage window stayed
open. Runtime replay reconstructs the event-sourced book state *as of* the
simulated submission time using recv_ts_ms.

This module remains ex-post SHADOW/PAPER only and never submits orders.
"""
from __future__ import annotations

import json

from p3_replay import P3ReplayEngine as _BaseReplayEngine


REPLAY_VERSION = "P3_REPLAY_RECV_ASOF_V2"


class P3ReplayEngine(_BaseReplayEngine):
    """Replay using collector-observation time rather than exchange-change time."""

    def _future_book(self, condition_id: str, side: str, target_ms: int):  # noqa: ANN001
        # Event-sourced reconstruction: the latest full-depth state observed at or
        # before target is the state available to a simulated FOK at target. If no
        # change occurs after opportunity detection, the detection book correctly
        # remains active instead of becoming a false missing snapshot.
        return self.p26.execute(
            """
            SELECT * FROM p26_clob_books
            WHERE condition_id=? AND side=? AND recv_ts_ms<=?
            ORDER BY recv_ts_ms DESC,id DESC LIMIT 1
            """,
            (condition_id, side, int(target_ms)),
        ).fetchone()

    def _record(self, result) -> None:  # noqa: ANN001
        # details is intentionally mutable even though ReplayOutcome itself is a
        # frozen dataclass. Versioning identifies rows produced by corrected replay.
        result.details["replay_version"] = REPLAY_VERSION
        result.details["time_axis"] = "recv_ts_ms_asof"
        super()._record(result)

    def purge_legacy_replays(self) -> int:
        """Repair only false legacy NO_SYNCHRONOUS_BOOK research artifacts.

        The production symptom was 0% pair fill because source-time lookup could not
        find any synchronous book. Other historical replay outcomes are left intact;
        this avoids rewriting unrelated research history and preserves scheduler
        completion semantics.
        """
        rows = self.p3.execute(
            "SELECT id,outcome,details_json FROM p3_replays WHERE outcome='NO_SYNCHRONOUS_BOOK'"
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
        placeholders = ",".join("?" for _ in stale_ids)
        self.p3.execute(f"DELETE FROM p3_replays WHERE id IN ({placeholders})", stale_ids)
        self.p3.commit()
        return len(stale_ids)
