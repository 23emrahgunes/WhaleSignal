import sqlite3

from p3_config import P3Settings
from p3_dry_run import build_dry_summary
from p3_models import ARB_BUY_MERGE, StructuralOpportunity
from p3_recorder import P3Recorder
from p3_web import _nearest_rank


def _opp(condition: str, ts: int, book_id: int, *, capital: float = 10.0, pnl: float = 0.05):
    return StructuralOpportunity(
        strategy=ARB_BUY_MERGE,
        condition_id=condition,
        combo_key=f"{condition}:5m",
        detected_ts_ms=ts,
        up_book_id=book_id,
        down_book_id=book_id + 1000,
        up_book_ts_ms=ts,
        down_book_ts_ms=ts,
        source_skew_ms=0,
        max_book_age_ms=0,
        quantity_shares=capital + pnl,
        up_vwap=0.40,
        down_vwap=0.58,
        up_fee_usdc=0.01,
        down_fee_usdc=0.01,
        gross_edge_per_share=0.02,
        gross_profit_usdc=pnl,
        execution_buffer_usdc=0.0,
        net_profit_usdc=pnl,
        capital_usdc=capital,
        net_roi=pnl / capital,
        up_limit_price=0.40,
        down_limit_price=0.58,
        fee_lineage_ok=True,
    )


def _insert_replay(conn, opp_id: int, delay: int, outcome: str, pnl: float, both: int):
    conn.execute(
        """
        INSERT INTO p3_replays(
            opportunity_id,delay_ms,target_ts_ms,observed_ts_ms,strategy,
            quantity_shares,up_fill,down_fill,both_fill,outcome,
            cycle_net_pnl_usdc,details_json,created_at_ms
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (opp_id, delay, 1000 + delay, 1000 + delay, ARB_BUY_MERGE,
         1.0, both, both, both, outcome, pnl, "{}", 2000),
    )
    conn.commit()


def test_dry_run_counts_one_first_entry_per_window(tmp_path):
    db = tmp_path / "p3.sqlite"
    recorder = P3Recorder(str(db))
    try:
        first_id, _ = recorder.record_opportunity(_opp("ETH", 1000, 1))
        recorder.touch_window(first_id, _opp("ETH", 1000, 1))
        # Same ETH window observed again: must never become a second dry attempt.
        later_id, _ = recorder.record_opportunity(_opp("ETH", 1250, 2, pnl=0.08))
        recorder.touch_window(later_id, _opp("ETH", 1250, 2, pnl=0.08))

        second_id, _ = recorder.record_opportunity(_opp("XRP", 5000, 3))
        recorder.touch_window(second_id, _opp("XRP", 5000, 3))

        big_id, _ = recorder.record_opportunity(_opp("BTC", 9000, 4, capital=25.0, pnl=0.20))
        recorder.touch_window(big_id, _opp("BTC", 9000, 4, capital=25.0, pnl=0.20))

        recorder.close_stale_windows(set(), now_ms=12_000, grace_ms=0)
        _insert_replay(recorder.conn, first_id, 100, "BOTH_FILLED", 0.05, 1)
        _insert_replay(recorder.conn, later_id, 100, "BOTH_FILLED", 0.08, 1)
        _insert_replay(recorder.conn, second_id, 100, "ONE_LEG_FILLED_UNWIND", -0.02, 0)
        _insert_replay(recorder.conn, big_id, 100, "BOTH_FILLED", 0.20, 1)
    finally:
        recorder.close()

    settings = P3Settings(
        p3_db_path=str(db),
        p26_db_path=str(tmp_path / "p26.sqlite"),
        replay_delays_ms="10,25,50,100,200,500",
        dry_latency_ms=100,
        dry_start_bankroll_usdc=100,
        max_capital_per_cycle_usdc=20,
        dry_min_net_profit_usdc=0.01,
        dry_min_net_roi=0.0025,
        readiness_min_windows=2,
        readiness_min_pair_completion=0.50,
        readiness_min_pair_wilson_lower=0.09,
        readiness_max_one_leg_rate=0.50,
        readiness_max_drawdown_usdc=1.0,
    )
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        summary = build_dry_summary(conn, settings)
    finally:
        conn.close()

    assert summary["windows_seen"] == 3
    assert summary["attempts_executed"] == 2
    assert summary["skipped_capital"] == 1
    assert abs(summary["cumulative_pnl_usdc"] - 0.03) < 1e-9
    assert abs(summary["bankroll_usdc"] - 100.03) < 1e-9
    assert abs(summary["max_drawdown_usdc"] - 0.02) < 1e-9
    assert summary["pair_fills"] == 1
    assert summary["one_leg"] == 1
    # First ETH observation wins; hindsight peak observation must not be selected.
    eth = [x for x in summary["recent_attempts"] if x["combo_key"] == "ETH:5m"][0]
    assert eth["opportunity_id"] == first_id
    assert abs(eth["cycle_net_pnl_usdc"] - 0.05) < 1e-9
    assert summary["readiness"]["status"] == "DRY_VALIDATED"


def test_p90_nearest_rank_cannot_fall_below_median_for_two_windows():
    lifetimes = [2259, 20321]
    assert _nearest_rank(lifetimes, 0.90) == 20321
