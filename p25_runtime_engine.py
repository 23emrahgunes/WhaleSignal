"""Small runtime wrapper exposing settlement-poller diagnostics."""
from __future__ import annotations

from p25_engine import P25Engine as _BaseP25Engine


class P25Engine(_BaseP25Engine):
    """P2.5 engine with observable resolution counters in ``/api/state``."""

    def snapshot(self) -> dict:
        data = super().snapshot()
        discovery = getattr(self.hub, "discovery", None)
        footer = data.setdefault("footer", {})
        resolution = {
            "polls": int(getattr(discovery, "resolution_polls", 0) or 0),
            "waiting": int(getattr(discovery, "resolution_waiting", 0) or 0),
            "fetch_empty": int(
                getattr(discovery, "resolution_fetch_empty", 0) or 0
            ),
            "resolved_runtime": int(
                getattr(discovery, "resolution_resolved", 0) or 0
            ),
            "errors": int(getattr(discovery, "resolution_errors", 0) or 0),
            "last_poll_ts": getattr(discovery, "last_resolution_poll_ts", 0.0),
            "last_source": getattr(discovery, "last_resolution_source", None),
        }
        data["resolution"] = resolution
        footer.update(
            {
                "resolution_waiting": resolution["waiting"],
                "resolution_polls": resolution["polls"],
                "resolution_fetch_empty": resolution["fetch_empty"],
                "resolution_resolved_runtime": resolution["resolved_runtime"],
                "resolution_errors": resolution["errors"],
            }
        )
        return data
