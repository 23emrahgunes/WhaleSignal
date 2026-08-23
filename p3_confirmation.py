"""Strict independent-window confirmation using persisted positive scan timestamps.

Window lifecycle grace and trade confirmation are intentionally different concepts.
A P3 window may stay OPEN briefly while an opportunity disappears, but a DRY entry
must prove a continuous positive scanner chain from window open through the configured
confirmation horizon. Any excessive gap fails that window for that policy.

SHADOW/DRY analytics only; no execution path exists here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import sqlite3
from typing import Optional


CONFIRMED = "CONFIRMED"
PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
SKIPPED_CONFIRMATION = "SKIPPED_CONFIRMATION"
CONFIRMATION_GAP = "CONFIRMATION_GAP"
LEGACY_CONFIRMATION_UNPROVEN = "LEGACY_CONFIRMATION_UNPROVEN"


@dataclass(frozen=True)
class ConfirmationSelection:
    window_id: int
    status: str
    confirm_ms: int
    target_ts_ms: int
    observation_id: Optional[int]
    opportunity_id: Optional[int]
    entry_ts_ms: Optional[int]
    events_seen: int
    max_gap_seen_ms: Optional[int]
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def select_confirmed_observation(
    conn: sqlite3.Connection,
    *,
    window_id: int,
    confirm_ms: int,
    max_gap_ms: int,
) -> ConfirmationSelection:
    """Select the first continuously-proven positive observation at/after target.

    Old windows created before observation-timeline deployment are deliberately not
    backfilled from coarse window metadata. They remain indicative/legacy evidence
    and cannot enter strict DRY PnL or readiness.
    """
    if confirm_ms < 0 or max_gap_ms < 1:
        raise ValueError("confirm_ms must be >=0 and max_gap_ms must be positive")

    window = conn.execute(
        "SELECT * FROM p3_windows WHERE id=?", (int(window_id),)
    ).fetchone()
    if window is None:
        raise KeyError(f"window not found: {window_id}")

    opened = int(window["opened_ts_ms"])
    target = opened + int(confirm_ms)
    status = str(window["status"])
    events = conn.execute(
        """
        SELECT id,window_id,opportunity_id,observed_ts_ms
        FROM p3_window_observations
        WHERE window_id=?
        ORDER BY observed_ts_ms,id
        """,
        (int(window_id),),
    ).fetchall()

    if not events:
        return ConfirmationSelection(
            window_id=int(window_id), status=LEGACY_CONFIRMATION_UNPROVEN,
            confirm_ms=int(confirm_ms), target_ts_ms=target,
            observation_id=None, opportunity_id=None, entry_ts_ms=None,
            events_seen=0, max_gap_seen_ms=None,
            reason="NO_STRICT_OBSERVATION_TIMELINE",
        )

    first_ts = int(events[0]["observed_ts_ms"])
    opening_gap = max(0, first_ts - opened)
    if opening_gap > int(max_gap_ms):
        return ConfirmationSelection(
            window_id=int(window_id), status=LEGACY_CONFIRMATION_UNPROVEN,
            confirm_ms=int(confirm_ms), target_ts_ms=target,
            observation_id=None, opportunity_id=None, entry_ts_ms=None,
            events_seen=len(events), max_gap_seen_ms=opening_gap,
            reason="TIMELINE_STARTED_AFTER_WINDOW_OPEN",
        )

    max_gap_seen = opening_gap
    previous_ts = first_ts
    candidate = None
    for event in events:
        event_ts = int(event["observed_ts_ms"])
        gap = max(0, event_ts - previous_ts)
        max_gap_seen = max(max_gap_seen, gap)
        if gap > int(max_gap_ms):
            return ConfirmationSelection(
                window_id=int(window_id), status=CONFIRMATION_GAP,
                confirm_ms=int(confirm_ms), target_ts_ms=target,
                observation_id=None, opportunity_id=None, entry_ts_ms=None,
                events_seen=len(events), max_gap_seen_ms=max_gap_seen,
                reason=f"POSITIVE_SCAN_GAP_EXCEEDED:{gap}>{int(max_gap_ms)}",
            )
        if event_ts >= target:
            candidate = event
            break
        previous_ts = event_ts

    if candidate is None:
        pending = status == "OPEN"
        return ConfirmationSelection(
            window_id=int(window_id),
            status=PENDING_CONFIRMATION if pending else SKIPPED_CONFIRMATION,
            confirm_ms=int(confirm_ms), target_ts_ms=target,
            observation_id=None, opportunity_id=None, entry_ts_ms=None,
            events_seen=len(events), max_gap_seen_ms=max_gap_seen,
            reason="TARGET_NOT_YET_COVERED" if pending else "WINDOW_ENDED_BEFORE_CONFIRMATION",
        )

    return ConfirmationSelection(
        window_id=int(window_id), status=CONFIRMED,
        confirm_ms=int(confirm_ms), target_ts_ms=target,
        observation_id=int(candidate["id"]),
        opportunity_id=int(candidate["opportunity_id"]),
        entry_ts_ms=int(candidate["observed_ts_ms"]),
        events_seen=len(events), max_gap_seen_ms=max_gap_seen,
        reason="CONTINUOUS_POSITIVE_TIMELINE",
    )
