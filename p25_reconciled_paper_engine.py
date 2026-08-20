"""Paper engine wrapper exposing restart-reconciliation diagnostics."""
from __future__ import annotations

from p25_paper_engine import P25Engine as _BaseP25Engine


class P25Engine(_BaseP25Engine):
    def attach_paper_reconciler(self, reconciler) -> None:  # noqa: ANN001
        self._paper_reconciler = reconciler

    def snapshot(self) -> dict:
        data = super().snapshot()
        reconciler = getattr(self, "_paper_reconciler", None)
        stats = reconciler.snapshot() if reconciler is not None else {
            "runs": 0,
            "rows_seen": 0,
            "settled": 0,
            "unresolved": 0,
            "fetch_empty": 0,
            "condition_mismatch": 0,
            "errors": 0,
            "last_run_ts": 0.0,
            "last_settled_condition": None,
            "last_source": None,
        }
        data["paper_reconciliation"] = stats
        footer = data.setdefault("footer", {})
        footer.update(
            {
                "paper_reconcile_runs": stats.get("runs", 0),
                "paper_reconciled": stats.get("settled", 0),
                "paper_reconcile_unresolved": stats.get("unresolved", 0),
                "paper_reconcile_fetch_empty": stats.get("fetch_empty", 0),
                "paper_reconcile_errors": stats.get("errors", 0),
            }
        )
        return data
