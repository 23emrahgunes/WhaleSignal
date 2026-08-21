"""Deterministic side and cross-combo selection for Direction Engine Paper V2.

The project is directional, not a complete-set arbitrage engine.  Two passing
sides are therefore an integrity failure rather than an invitation to choose the
larger number.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Protocol


class DirectionalDecision(Protocol):
    eligible: bool
    reason: str
    side: str
    net_edge: Optional[float]


@dataclass(frozen=True)
class SideSelection:
    eligible: bool
    reason: str
    side: Optional[str]
    decision: Optional[DirectionalDecision]
    details: tuple[str, ...]


@dataclass(frozen=True)
class RankedCandidate:
    combo_key: str
    condition_id: str
    decision: DirectionalDecision
    decision_ts_ms: int


def select_directional_side(
    *,
    up: DirectionalDecision,
    down: DirectionalDecision,
    up_token_id: str,
    down_token_id: str,
    up_book_ts_ms: int,
    down_book_ts_ms: int,
    p_lower_up: Optional[float],
    p_lower_down: Optional[float],
    max_book_skew_ms: int,
) -> SideSelection:
    if str(up.side).upper() != "UP" or str(down.side).upper() != "DOWN":
        return SideSelection(False, "SIDE_LABEL_INTEGRITY_FAILURE", None, None, ())
    if not up_token_id or not down_token_id or up_token_id == down_token_id:
        return SideSelection(False, "TOKEN_MAPPING_INTEGRITY_FAILURE", None, None, ())
    skew = abs(int(up_book_ts_ms) - int(down_book_ts_ms))
    if skew > int(max_book_skew_ms):
        return SideSelection(
            False,
            "SIDE_BOOK_TIME_MISMATCH",
            None,
            None,
            (f"book_skew_ms={skew}",),
        )
    if (
        p_lower_up is not None
        and p_lower_down is not None
        and float(p_lower_up) + float(p_lower_down) > 1.0 + 1e-12
    ):
        return SideSelection(
            False,
            "CALIBRATION_COMPLEMENT_INTEGRITY_FAILURE",
            None,
            None,
            (f"lower_sum={float(p_lower_up)+float(p_lower_down):.8f}",),
        )

    passed = [decision for decision in (up, down) if decision.eligible]
    if len(passed) == 1:
        chosen = passed[0]
        return SideSelection(True, "OPEN", str(chosen.side).upper(), chosen, ())
    if len(passed) == 2:
        return SideSelection(
            False,
            "DUAL_EDGE_INTEGRITY_FAILURE",
            None,
            None,
            (
                f"up_edge={up.net_edge}",
                f"down_edge={down.net_edge}",
                "direction engine does not execute complete-set arbitrage",
            ),
        )
    return SideSelection(
        False,
        "NO_SIDE_PASSES",
        None,
        None,
        (f"up={up.reason}", f"down={down.reason}"),
    )


def rank_eligible_candidates(
    candidates: Iterable[RankedCandidate],
) -> tuple[RankedCandidate, ...]:
    """Rank only after every per-market gate has been applied.

    The actual OOS replay must call this same function so the 12-combo max
    selection (winner's-curse mechanism) is represented in evaluation.
    """
    eligible = [
        item
        for item in candidates
        if item.decision.eligible and item.decision.net_edge is not None
    ]
    return tuple(
        sorted(
            eligible,
            key=lambda item: (
                -float(item.decision.net_edge),
                int(item.decision_ts_ms),
                item.combo_key,
                item.condition_id,
            ),
        )
    )
