from dataclasses import dataclass

from p26_selection import RankedCandidate, rank_eligible_candidates, select_directional_side


@dataclass(frozen=True)
class D:
    eligible: bool
    reason: str
    side: str
    net_edge: float | None


def test_exactly_one_side_may_pass_and_dual_edge_is_integrity_failure():
    up = D(True, "OPEN", "UP", 0.04)
    down = D(False, "NET_EDGE_BELOW_MINIMUM", "DOWN", -0.02)
    selected = select_directional_side(
        up=up, down=down,
        up_token_id="up", down_token_id="down",
        up_book_ts_ms=1000, down_book_ts_ms=1010,
        p_lower_up=0.65, p_lower_down=0.30,
        max_book_skew_ms=100,
    )
    assert selected.eligible and selected.side == "UP"

    dual = select_directional_side(
        up=up, down=D(True, "OPEN", "DOWN", 0.03),
        up_token_id="up", down_token_id="down",
        up_book_ts_ms=1000, down_book_ts_ms=1000,
        p_lower_up=0.65, p_lower_down=0.30,
        max_book_skew_ms=100,
    )
    assert not dual.eligible
    assert dual.reason == "DUAL_EDGE_INTEGRITY_FAILURE"


def test_side_integrity_and_cross_combo_ranking_are_deterministic():
    bad = select_directional_side(
        up=D(False, "x", "UP", None),
        down=D(False, "y", "DOWN", None),
        up_token_id="same", down_token_id="same",
        up_book_ts_ms=1000, down_book_ts_ms=1000,
        p_lower_up=0.8, p_lower_down=0.3,
        max_book_skew_ms=100,
    )
    assert bad.reason == "TOKEN_MAPPING_INTEGRITY_FAILURE"

    ranked = rank_eligible_candidates(
        [
            RankedCandidate("ETH:5m", "b", D(True, "OPEN", "UP", 0.03), 10),
            RankedCandidate("BTC:5m", "a", D(True, "OPEN", "DOWN", 0.05), 10),
            RankedCandidate("SOL:5m", "c", D(False, "NO", "UP", 0.20), 10),
        ]
    )
    assert [item.combo_key for item in ranked] == ["BTC:5m", "ETH:5m"]
