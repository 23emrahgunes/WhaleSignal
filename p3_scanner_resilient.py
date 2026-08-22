"""Reconnect-aware runtime wrapper for the P3 structural scanner.

The base P3 scanner remains intentionally strict for deterministic research tests.
The live runtime additionally understands the P2.6 collector's scheduled websocket
rotation: a brief disconnected state is tolerated only when the previous session ran
long enough to be a normal registry rotation and real CLOB data arrived moments ago.
Actual dead/disrupted transports still fail closed.
"""
from __future__ import annotations

from typing import Any, Optional

from p3_complete_set import BookPair
from p3_scanner import StructuralArbScanner


RECONNECT_GRACE_MS = 2_000
MIN_ROTATION_SESSION_AGE_MS = 20_000


class ReconnectAwareStructuralArbScanner(StructuralArbScanner):
    def _latest_book(self, condition_id: str, side: str):  # noqa: ANN001
        # recv_ts_ms is the collector observation time.  A resting quote can retain
        # an old source timestamp while being freshly observed after reconnect.
        return self.p26.execute(
            """
            SELECT * FROM p26_clob_books
            WHERE condition_id=? AND side=?
            ORDER BY recv_ts_ms DESC,id DESC LIMIT 1
            """,
            (condition_id, side),
        ).fetchone()

    def _transport_current_or_planned_rotation(
        self,
        health: dict[str, Any],
        now_ms: int,
    ) -> bool:
        heartbeat = int(health.get("heartbeat_ts_ms") or 0)
        max_heartbeat_age = max(5_000, int(self.settings.max_book_age_ms))
        if heartbeat <= 0 or now_ms - heartbeat > max_heartbeat_age:
            return False
        if bool(health.get("connected")):
            return True

        session_started = int(health.get("session_started_ms") or 0)
        last_recv = int(health.get("last_message_recv_ms") or 0)
        if session_started <= 0 or last_recv <= 0:
            return False
        session_age = now_ms - session_started
        recv_age = now_ms - last_recv
        # The P2.6 collector normally rotates after ~30s.  Requiring an already
        # mature session prevents an arbitrary fresh disconnect from masquerading
        # as a scheduled rotation.  The grace itself remains only two seconds.
        return (
            session_age >= MIN_ROTATION_SESSION_AGE_MS
            and 0 <= recv_age <= RECONNECT_GRACE_MS
        )

    def _pair(
        self,
        item: dict,
        now_ms: int,
        health: Optional[dict[str, Any]],
    ) -> tuple[Optional[BookPair], str]:
        condition_id = str(item["condition_id"])
        up_row = self._latest_book(condition_id, "UP")
        down_row = self._latest_book(condition_id, "DOWN")
        if up_row is None or down_row is None:
            return None, "MISSING_BOOK"

        up_source_ts = int(up_row["source_ts_ms"])
        down_source_ts = int(down_row["source_ts_ms"])
        if health is not None:
            if not self._transport_current_or_planned_rotation(health, now_ms):
                return None, "TRANSPORT_STALE"
            session_started = int(health.get("session_started_ms") or 0)
            if session_started <= 0:
                return None, "SESSION_INCOMPLETE"
            if (
                int(up_row["recv_ts_ms"]) < session_started
                or int(down_row["recv_ts_ms"]) < session_started
            ):
                return None, "SESSION_INCOMPLETE"
        else:
            if (
                now_ms - up_source_ts > self.settings.max_book_age_ms
                or now_ms - down_source_ts > self.settings.max_book_age_ms
            ):
                return None, "STALE_BOOK"
            if abs(up_source_ts - down_source_ts) > self.settings.max_source_skew_ms:
                return None, "SOURCE_SKEW"

        return (
            BookPair(
                condition_id=condition_id,
                combo_key=str(item["combo_key"]),
                up_book_id=int(up_row["id"]),
                down_book_id=int(down_row["id"]),
                up=self._decode_book(up_row),
                down=self._decode_book(down_row),
            ),
            "OK",
        )
