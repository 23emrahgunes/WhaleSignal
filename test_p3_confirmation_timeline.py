from __future__ import annotations

import time

from p26_book_store import BookSnapshotStore
from p26_execution import OrderBookSnapshot
from p26_fee import FeeScheduleStore
from p3_config import P3Settings
from p3_confirmation import CONFIRMED, select_confirmed_observation
from p3_entry_replay import P3EntryReplayEngine
from p3_models import ARB_BUY_MERGE, StructuralOpportunity
from p3_recorder import P3Recorder
from p3_replay_clock import P3ReplayEngine as ClockReplayEngine


def _opp(ts: int, up_book_id: int, down_book_id: int) -> StructuralOpportunity:
    return StructuralOpportunity(
        strategy=ARB_BUY_MERGE,
        condition_id="cond",
        combo_key="ETH:5m",
        detected_ts_ms=ts,
        up_book_id=up_book_id,
        down_book_id=down_book_id,
        up_book_ts_ms=1_000,
        down_book_ts_ms=1_100,
        source_skew_ms=100,
        max_book_age_ms=0,
        quantity_shares=10.0,
        up_vwap=0.40,
        down_vwap=0.50,
        up_fee_usdc=0.0,
        down_fee_usdc=0.0,
        gross_edge_per_share=0.10,
        gross_profit_usdc=1.0,
        execution_buffer_usdc=0.0,
        net_profit_usdc=1.0,
        capital_usdc=9.0,
        net_roi=1.0 / 9.0,
        up_limit_price=0.40,
        down_limit_price=0.50,
        fee_lineage_ok=True,
    )


def test_dedup_state_keeps_scan_timeline_and_replays_from_confirmation_time(tmp_path):
    p26 = tmp_path / "p26.sqlite"
    p3 = tmp_path / "p3.sqlite"
    base = int(time.time() * 1000) + 5_000
    settings = P3Settings(
        p26_db_path=str(p26), p3_db_path=str(p3),
        replay_delays_ms="100", dry_latency_ms=100,
        dry_entry_confirm_ms=250, dry_confirm_max_gap_ms=400,
        dry_survival_delays_ms="0,250", web_port=18093,
    )

    fees = FeeScheduleStore(str(p26))
    fees.upsert_market_info(
        condition_id="cond", combo_key="ETH:5m",
        market_end_ts_ms=base + 300_000, source_ts_ms=500,
        payload={
            "t": [{"t": "up", "o": "UP"}, {"t": "down", "o": "DOWN"}],
            "fd": None,
        },
    )
    fees.close()

    books = BookSnapshotStore(str(p26))
    up = OrderBookSnapshot.from_levels(
        token_id="up", ts_ms=1_000,
        bids=[(0.39, 20)], asks=[(0.40, 20)],
    )
    down_initial = OrderBookSnapshot.from_levels(
        token_id="down", ts_ms=1_100,
        bids=[(0.49, 20)], asks=[(0.50, 20)],
    )
    down_after_confirmation = OrderBookSnapshot.from_levels(
        token_id="down", ts_ms=1_200,
        bids=[(0.44, 20)], asks=[(0.55, 20)],
    )
    assert books.insert(condition_id="cond", combo_key="ETH:5m", side="UP", snapshot=up)
    assert books.insert(condition_id="cond", combo_key="ETH:5m", side="DOWN", snapshot=down_initial)
    initial_rows = books.conn.execute(
        "SELECT id,side FROM p26_clob_books ORDER BY id"
    ).fetchall()
    ids = {str(row["side"]): int(row["id"]) for row in initial_rows}
    books.conn.execute(
        "UPDATE p26_clob_books SET inserted_at_ms=? WHERE id IN (?,?)",
        (base - 100, ids["UP"], ids["DOWN"]),
    )
    assert books.insert(
        condition_id="cond", combo_key="ETH:5m", side="DOWN",
        snapshot=down_after_confirmation,
    )
    new_down = books.conn.execute(
        "SELECT id FROM p26_clob_books WHERE side='DOWN' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    books.conn.execute(
        "UPDATE p26_clob_books SET inserted_at_ms=? WHERE id=?",
        (base + 300, int(new_down["id"])),
    )
    books.conn.commit()
    books.close()

    recorder = P3Recorder(str(p3))
    first = _opp(base, ids["UP"], ids["DOWN"])
    opp_id, created = recorder.record_opportunity(first)
    assert created
    window_id = recorder.touch_window(opp_id, first)

    # Exact same book/opportunity survives at +250ms. Opportunity row deduplicates,
    # but the strict scanner-observation timeline must still gain a second event.
    later = _opp(base + 250, ids["UP"], ids["DOWN"])
    same_opp_id, created_again = recorder.record_opportunity(later)
    assert same_opp_id == opp_id
    assert created_again is False
    recorder.touch_window(same_opp_id, later)
    observations = recorder.conn.execute(
        "SELECT * FROM p3_window_observations WHERE window_id=? ORDER BY observed_ts_ms",
        (window_id,),
    ).fetchall()
    assert [int(row["observed_ts_ms"]) for row in observations] == [base, base + 250]

    selection = select_confirmed_observation(
        recorder.conn, window_id=window_id, confirm_ms=250, max_gap_ms=400
    )
    assert selection.status == CONFIRMED
    assert selection.entry_ts_ms == base + 250
    assert selection.observation_id == int(observations[1]["id"])
    recorder.close()

    # Generic replay is anchored to original detection +100ms, before the DOWN move.
    generic = ClockReplayEngine(settings)
    try:
        old_time = generic.replay_one(opp_id, 100)
        assert old_time.outcome == "BOTH_FILLED"
    finally:
        generic.close()

    # Strict entry replay is anchored to confirmation +100ms, after the DOWN ask
    # moved beyond the original 0.50 limit; only one leg can fill.
    strict = P3EntryReplayEngine(settings)
    try:
        result = strict.replay_entry(
            window_id=window_id, confirm_ms=250,
            observation_id=int(selection.observation_id), delay_ms=100,
        )
        assert result.target_ts_ms == base + 350
        assert result.outcome == "ONE_LEG_FILLED_UNWIND"
        assert result.up_fill is True
        assert result.down_fill is False
        row = strict.p3.execute(
            "SELECT entry_ts_ms,target_ts_ms,outcome FROM p3_entry_replays WHERE window_id=? AND confirm_ms=250",
            (window_id,),
        ).fetchone()
        assert int(row["entry_ts_ms"]) == base + 250
        assert int(row["target_ts_ms"]) == base + 350
        assert str(row["outcome"]) == "ONE_LEG_FILLED_UNWIND"
    finally:
        strict.close()
