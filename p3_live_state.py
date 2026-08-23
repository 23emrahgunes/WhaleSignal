"""Fail-safe in-memory LIVE arming state for P3.

The process always starts in DRY. Arming is intentionally ephemeral: a restart,
crash or deploy returns the process to DRY and requires an explicit local control
action before any live executor may submit orders.
"""
from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

MODE_DRY = "DRY"
MODE_LIVE_ARMED = "LIVE_ARMED"
MODE_LIVE_HALTED = "LIVE_HALTED"


@dataclass(frozen=True)
class LiveSnapshot:
    mode: str
    session_id: str
    armed_at_ms: int | None
    halted_at_ms: int | None
    reason: str | None
    last_preflight: dict[str, Any] | None
    live_feature_enabled: bool
    auto_execute_enabled: bool


class LiveState:
    """Thread-safe process-local arming state.

    No key material is stored here. The CSRF token is only used by the localhost
    control panel and is never exposed by the public/read-only dashboard API.
    """

    def __init__(self, *, live_feature_enabled: bool, auto_execute_enabled: bool) -> None:
        self._lock = threading.RLock()
        self._mode = MODE_DRY
        self._session_id = uuid.uuid4().hex
        self._armed_at_ms: int | None = None
        self._halted_at_ms: int | None = None
        self._reason: str | None = "process_start"
        self._last_preflight: dict[str, Any] | None = None
        self._live_feature_enabled = bool(live_feature_enabled)
        self._auto_execute_enabled = bool(auto_execute_enabled)
        self._control_token = secrets.token_urlsafe(32)

    @property
    def control_token(self) -> str:
        return self._control_token

    def is_armed(self) -> bool:
        with self._lock:
            return self._mode == MODE_LIVE_ARMED

    def can_auto_execute(self) -> bool:
        with self._lock:
            return (
                self._mode == MODE_LIVE_ARMED
                and self._live_feature_enabled
                and self._auto_execute_enabled
            )

    def arm(self, preflight: dict[str, Any]) -> LiveSnapshot:
        """Arm LIVE only after a successful preflight payload."""
        if not bool(preflight.get("ok")):
            raise ValueError("LIVE cannot be armed with a failed preflight")
        if not self._live_feature_enabled:
            raise ValueError("P3_LIVE_FEATURE_ENABLED=false")
        with self._lock:
            self._mode = MODE_LIVE_ARMED
            self._armed_at_ms = int(time.time() * 1000)
            self._halted_at_ms = None
            self._reason = "operator_armed"
            self._last_preflight = dict(preflight)
            return self.snapshot()

    def disarm(self, reason: str = "operator_disarmed") -> LiveSnapshot:
        with self._lock:
            self._mode = MODE_DRY
            self._armed_at_ms = None
            self._halted_at_ms = None
            self._reason = str(reason)
            return self.snapshot()

    def halt(self, reason: str) -> LiveSnapshot:
        """Fail closed after an execution/settlement safety failure."""
        with self._lock:
            self._mode = MODE_LIVE_HALTED
            self._halted_at_ms = int(time.time() * 1000)
            self._reason = str(reason)
            return self.snapshot()

    def remember_preflight(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._last_preflight = dict(payload)

    def snapshot(self) -> LiveSnapshot:
        with self._lock:
            return LiveSnapshot(
                mode=self._mode,
                session_id=self._session_id,
                armed_at_ms=self._armed_at_ms,
                halted_at_ms=self._halted_at_ms,
                reason=self._reason,
                last_preflight=(dict(self._last_preflight) if self._last_preflight else None),
                live_feature_enabled=self._live_feature_enabled,
                auto_execute_enabled=self._auto_execute_enabled,
            )

    def public_dict(self) -> dict[str, Any]:
        """Sanitized status safe for the read-only dashboard."""
        snap = self.snapshot()
        preflight = snap.last_preflight or {}
        return {
            "mode": snap.mode,
            "session_id": snap.session_id[:12],
            "armed_at_ms": snap.armed_at_ms,
            "halted_at_ms": snap.halted_at_ms,
            "reason": snap.reason,
            "live_feature_enabled": snap.live_feature_enabled,
            "auto_execute_enabled": snap.auto_execute_enabled,
            "preflight_ok": preflight.get("ok"),
            "preflight_checked_at_ms": preflight.get("checked_at_ms"),
            "preflight_reasons": list(preflight.get("reasons") or []),
        }
