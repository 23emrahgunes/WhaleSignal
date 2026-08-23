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


def _settings(tmp_path, db, **overrides):
    values = dict(
        p3_db_path=str(db),
        p26_db_path=str(tmp_path / "p26.sqlite"),
        replay_delays_ms="10,25,50,100,200,500",
        dry_latency_ms=100,
        dry_entry_confirm_ms=0,
        dry_confirm_max_gap_ms=400,
        dry_survival_delays_ms="0,250",
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
    values.update(overrides)
    return P3Settings(**values)


def _observation(conn, window_id: int, ts: int):
    row = conn.execute(
        "SELECT * FROM p3_window_observations WHERE window_id=? AND observed_ts_ms=?",
        (window_id, ts),
    ).fetchone()
    assert row is not None
    return row


def _insert_entry_replay(
    conn,
    *,
    window_id: int,
    confirm_ms: int,
    observation_id: int,
    opp_id: int,
    entry_ts: int,
    delay: int,
    outcome: str,
    pnl: float,
    both: int,
):
    conn.execute(
        """
        INSERT INTO p3_entry_replays(
            window_id,confirm_ms,observation_id,opportunity_id,entry_ts_ms,
            delay_ms,target_ts_ms,observed_ts_ms,strategy,quantity_shares,
            up_fill,down_fill,both_fill,outcome,cycle_net_pnl_usdc,
            details_json,created_at_ms
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            window_id, confirm_ms, observation_id, opp_id, entry_ts,
            delay, entry_ts + delay, entry_ts + delay, ARB_BUY_MERGE, 1.0,
            both, both, both, outcome, pnl,
            '{"entry_replay_version":"P3_STRICT_CONFIRM_ENTRY_V1"}', 20_000,
        ),
    )
    conn.commit()


def test_strict_confirm_zero_counts_one_entry_per_window(tmp_path):
    db = tmp_path / "p3.sqlite"
    recorder = P3Recorder(str(db))
    try:
        first_id, _ = recorder.record_opportunity(_opp("ETH", 1000, 1))
        eth_window = recorder.touch_window(first_id, _opp("ETH", 1000, 1))
        later_id, _ = recorder.record_opportunity(_opp("ETH", 1250, 2, pnl=0.08))
        recorder.touch_window(later_id, _opp("ETH", 1250, 2, pnl=0.08))

        second_id, _ = recorder.record_opportunity(_opp("XRP", 5000, 3))
        xrp_window = recorder.touch_window(second_id, _opp("XRP", 5000, 3))

        big_id, _ = recorder.record_opportunity(_opp("BTC", 9000, 4, capital=25.0, pnl=0.20))
        btc_window = recorder.touch_window(big_id, _opp("BTC", 9000, 4, capital=25.0, pnl=0.20))
        recorder.close_stale_windows(set(), now_ms=12_000, grace_ms=0)

        for window_id, ts, opp_id, outcome, pnl, both in (
            (eth_window, 1000, first_id, "BOTH_FILLED", 0.05, 1),
            (xrp_window, 5000, second_id, "ONE_LEG_FILLED_UNWIND", -0.02, 0),
            (btc_window, 9000, big_id, "BOTH_FILLED", 0.20, 1),
        ):
            obs = _observation(recorder.conn, window_id, ts)
            _insert_entry_replay(
                recorder.conn, window_id=window_id, confirm_ms=0,
                observation_id=int(obs["id"]), opp_id=opp_id, entry_ts=ts,
                delay=100, outcome=outcome, pnl=pnl, both=both,
            )
    finally:
        recorder.close()

    settings = _settings(tmp_path, db)
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    try:
        summary = build_dry_summary(conn, settings)
    finally:
        conn.close()

    assert summary["evidence_level"] == "STRICT_CONTINUOUS_TIMELINE"
    assert summary["entry_confirm_ms"] == 0
    assert summary["strict_timeline_windows"] == 3
    assert summary["legacy_unproven_windows"] == 0
    assert summary["attempts_executed"] == 2
    assert summary["skipped_capital"] == 1
    assert abs(summary["cumulative_pnl_usdc"] - 0.03) < 1e-9
    assert summary["pair_fills"] == 1
    assert summary["one_leg"] == 1
    eth = [x for x in summary["recent_attempts"] if x["combo_key"] == "ETH:5m"][0]
    assert eth["opportunity_id"] == first_id
    assert eth["entry_age_ms"] == 0
    assert abs(eth["cycle_net_pnl_usdc"] - 0.05) < 1e-9
    assert summary["readiness"]["status"] == "DRY_VALIDATED"


def test_strict_confirmation_filters_toxic_first_print(tmp_path):
    db = tmp_path / "p3.sqlite"
    recorder = P3Recorder(str(db))
    try:
        first_eth, _ = recorder.record_opportunity(_opp("ETH", 1000, 1, pnl=0.05))
        eth_window = recorder.touch_window(first_eth, _opp("ETH", 1000, 1, pnl=0.05))
        surviving_eth, _ = recorder.record_opportunity(_opp("ETH", 1250, 2, pnl=0.08))
        recorder.touch_window(surviving_eth, _opp("ETH", 1250, 2, pnl=0.08))

        xrp, _ = recorder.record_opportunity(_opp("XRP", 5000, 3, pnl=0.05))
        xrp_window = recorder.touch_window(xrp, _opp("XRP", 5000, 3, pnl=0.05))
        recorder.close_stale_windows(set(), now_ms=6000, grace_ms=0)

        eth0 = _observation(recorder.conn, eth_window, 1000)
        eth250 = _observation(recorder.conn, eth_window, 1250)
        xrp0 = _observation(recorder.conn, xrp_window, 5000)
        _insert_entry_replay(
            recorder.conn, window_id=eth_window, confirm_ms=0,
            observation_id=int(eth0["id"]), opp_id=first_eth, entry_ts=1000,
            delay=100, outcome="ONE_LEG_FILLED_UNWIND", pnl=-0.40, both=0,
        )
        _insert_entry_replay(
            recorder.conn, window_id=xrp_window, confirm_ms=0,
            observation_id=int(xrp0["id"]), opp_id=xrp, entry_ts=5000,
            delay=100, outcome="BOTH_FILLED", pnl=0.05, both=1,
        )
        _insert_entry_replay(
            recorder.conn, window_id=eth_window, confirm_ms=250,
            observation_id=int(eth250["id"]), opp_id=surviving_eth, entry_ts=1250,
            delay=100, outcome="BOTH_FILLED", pnl=0.08, both=1,
        )
    finally:
        recorder.close()

    settings = _settings(
        tmp_path, db, dry_entry_confirm_ms=250,
        readiness_min_windows=1, readiness_min_pair_completion=0.90,
        readiness_min_pair_wilson_lower=0.0, readiness_max_one_leg_rate=0.10,
    )
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    try:
        summary = build_dry_summary(conn, settings)
    finally:
        conn.close()

    assert summary["policy"] == "ONE_STRICT_CONFIRMED_ENTRY_PER_INDEPENDENT_WINDOW"
    assert summary["confirmed_windows"] == 1
    assert summary["skipped_confirmation"] == 1
    assert summary["attempts_executed"] == 1
    assert summary["pair_completion_rate"] == 1.0
    assert summary["one_leg_rate"] == 0.0
    assert abs(summary["cumulative_pnl_usdc"] - 0.08) < 1e-9
    eth = [x for x in summary["recent_attempts"] if x["combo_key"] == "ETH:5m"][0]
    assert eth["opportunity_id"] == surviving_eth
    assert eth["entry_age_ms"] == 250
    assert eth["replay_outcome"] == "BOTH_FILLED"

    baseline = summary["survival_by_confirm_ms"]["0"]
    confirmed = summary["survival_by_confirm_ms"]["250"]
    assert baseline["attempts_executed"] == 2
    assert baseline["one_leg"] == 1
    assert abs(baseline["cumulative_pnl_usdc"] - (-0.35)) < 1e-9
    assert confirmed["attempts_executed"] == 1
    assert confirmed["one_leg"] == 0
    assert abs(confirmed["pnl_delta_vs_0_usdc"] - 0.43) < 1e-9


def test_confirmation_gap_is_not_allowed_to_recover_inside_window_grace(tmp_path):
    db = tmp_path / "p3.sqlite"
    recorder = P3Recorder(str(db))
    try:
        first, _ = recorder.record_opportunity(_opp("SOL", 1000, 1))
        window = recorder.touch_window(first, _opp("SOL", 1000, 1))
        # Same lifecycle window returns after a 500ms hole. Window grace could keep
        # it open, but strict confirmation must reject the discontinuity.
        later, _ = recorder.record_opportunity(_opp("SOL", 1500, 2))
        recorder.touch_window(later, _opp("SOL", 1500, 2))
        recorder.close_stale_windows(set(), now_ms=2000, grace_ms=0)
    finally:
        recorder.close()

    settings = _settings(
        tmp_path, db, dry_entry_confirm_ms=250,
        readiness_min_windows=1, readiness_min_pair_completion=0.0,
        readiness_min_pair_wilson_lower=0.0, readiness_max_one_leg_rate=1.0,
    )
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    try:
        summary = build_dry_summary(conn, settings)
    finally:
        conn.close()

    assert summary["strict_timeline_windows"] == 1
    assert summary["confirmation_gaps"] == 1
    assert summary["confirmed_windows"] == 0
    assert summary["attempts_executed"] == 0
    attempt = summary["recent_attempts"][0]
    assert attempt["window_id"] == window
    assert attempt["dry_status"] == "CONFIRMATION_GAP"
    assert attempt["max_gap_seen_ms"] == 500


def test_open_window_without_target_coverage_is_pending(tmp_path):
    db = tmp_path / "p3.sqlite"
    recorder = P3Recorder(str(db))
    try:
        first_id, _ = recorder.record_opportunity(_opp("SOL", 1000, 1))
        recorder.touch_window(first_id, _opp("SOL", 1000, 1))
    finally:
        recorder.close()

    settings = _settings(
        tmp_path, db, dry_entry_confirm_ms=250,
        readiness_min_windows=1, readiness_min_pair_completion=0.0,
        readiness_min_pair_wilson_lower=0.0, readiness_max_one_leg_rate=1.0,
    )
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    try:
        summary = build_dry_summary(conn, settings)
    finally:
        conn.close()

    assert summary["pending_confirmation"] == 1
    assert summary["attempts_executed"] == 0
    assert summary["recent_attempts"][0]["dry_status"] == "PENDING_CONFIRMATION"


def test_p90_nearest_rank_cannot_fall_below_median_for_two_windows():
    lifetimes = [2259, 20321]
    assert _nearest_rank(lifetimes, 0.90) == 20321
