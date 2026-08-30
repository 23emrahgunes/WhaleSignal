"""Small in-memory stability gate for STRICT paper-entry confirmation.

The state is intentionally process-local and conservative. A direction must remain
unchanged with sufficiently frequent observations for the configured number of
seconds before a paper entry is allowed. Restarting the process resets stability.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _State:
    side: str
    since_ts: float
    last_ts: float


class SignalStabilityGate:
    def __init__(self, required_sec: float, max_gap_sec: float) -> None:
        self.required_sec = max(0.0, float(required_sec))
        self.max_gap_sec = max(0.05, float(max_gap_sec))
        self._states: dict[str, _State] = {}

    def reset(self, key: str) -> None:
        if key:
            self._states.pop(str(key), None)

    def observe(self, key: str, side: str, now_ts: float) -> tuple[bool, float]:
        key = str(key or "")
        side = str(side or "").upper()
        now = float(now_ts)
        if not key or side not in {"UP", "DOWN"}:
            self.reset(key)
            return False, 0.0
        if self.required_sec <= 0:
            return True, self.required_sec

        state = self._states.get(key)
        if (
            state is None
            or state.side != side
            or now < state.last_ts
            or now - state.last_ts > self.max_gap_sec
        ):
            self._states[key] = _State(side=side, since_ts=now, last_ts=now)
            return False, 0.0

        state.last_ts = now
        elapsed = max(0.0, now - state.since_ts)
        return elapsed + 1e-9 >= self.required_sec, elapsed

    def prune(self, now_ts: float, max_idle_sec: float = 600.0) -> None:
        now = float(now_ts)
        stale = [
            key
            for key, state in self._states.items()
            if now - state.last_ts > float(max_idle_sec)
        ]
        for key in stale:
            self._states.pop(key, None)
