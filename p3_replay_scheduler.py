"""Fair incremental scheduler for P3 delayed replay jobs."""
from __future__ import annotations

import time
from typing import Optional

from p3_replay_clock import P3ReplayEngine as _BaseReplayEngine


class P3ReplayEngine(_BaseReplayEngine):
    """Replay engine whose queue advances past already-complete opportunities."""

    def process_ready(
        self,
        *,
        now_ms: Optional[int] = None,
        batch_size: Optional[int] = None,
    ) -> dict:
        # One-time research repair: old replay-clock rows are false historical
        # artifacts under the current immutable first-receive clock. Remove only
        # those legacy-version rows so corrected replay can regenerate them.
        legacy_purged = self.purge_legacy_replays()

        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        delays = self.settings.replay_delays()
        max_delay = max(delays) + self.settings.replay_snapshot_tolerance_ms
        effective_batch = (
            int(self.settings.replay_batch_size)
            if batch_size is None
            else int(batch_size)
        )
        if effective_batch < 1:
            raise ValueError("replay batch_size must be positive")
        rows = self.p3.execute(
            """
            SELECT o.id
            FROM p3_opportunities o
            LEFT JOIN p3_replays r ON r.opportunity_id=o.id
            WHERE o.detected_ts_ms<=?
            GROUP BY o.id
            HAVING COUNT(r.id) < ?
            ORDER BY o.id
            LIMIT ?
            """,
            (
                now - max_delay,
                len(delays),
                effective_batch,
            ),
        ).fetchall()
        created = 0
        for row in rows:
            opp_id = int(row["id"])
            existing = {
                int(r[0])
                for r in self.p3.execute(
                    "SELECT delay_ms FROM p3_replays WHERE opportunity_id=?",
                    (opp_id,),
                ).fetchall()
            }
            for delay in delays:
                if delay in existing:
                    continue
                self.replay_one(opp_id, delay)
                created += 1
        return {
            "opportunities_scanned": len(rows),
            "replays_created": created,
            "legacy_replays_purged": legacy_purged,
            "batch_size": effective_batch,
        }
