from __future__ import annotations

from pathlib import Path

from p26_book_store import BookSnapshotStore
from p26_execution import OrderBookSnapshot
from p26_fee import FeeScheduleStore
from p3_config import P3Settings
from p3_models import ARB_BUY_MERGE, StructuralOpportunity
from p3_recorder import P3Recorder
from p3_replay import P3ReplayEngine as LegacyReplayEngine
from p3_replay_clock import REPLAY_VERSION, P3ReplayEngine as ClockReplayEngine
from p3_replay_scheduler import P3ReplayEngine as ScheduledReplayEngine


def _settings(tmp_path: Path) -> P3Settings:
    return P3Settings(
        p26_db_path=str(tmp_path / "p26.sqlite"),
        p3_db_path=str(tmp_path / "p3.sqlite"),
        reports_dir=str(tmp_path / "reports"),
        replay_delays_ms="10",
        replay_snapshot_tolerance_ms=250,
        replay_batch_size=20,
        web_port=18093,
    )


def _seed_opportunity(tmp_path: Path) -> tuple[P3Settings, int, int]:
    settings = _settings(tmp_path)
    detected = 1_000_000

    fees = FeeScheduleStore(settings.p26_db_path)
    fees.upsert_market_info(
        condition_id="cond",
        combo_key="ETH:5m",
        market_end_ts_ms=detected + 300_000,
        source_ts_ms=500,
        payload={
            "t": [{"t": "up", "o": "UP"}, {"t": "down", "o": "DOWN"}],
            "fd": None,
        },
    )
    fees.close()

    books = BookSnapshotStore(settings.p26_db_path)
    up = OrderBookSnapshot.from_levels(
        token_id="up",
        ts_ms=1_000,  # exchange last-change clock is intentionally ancient
        bids=[(0.39, 20)],
        asks=[(0.40, 20)],
    )
    down = OrderBookSnapshot.from_levels(
        token_id="down",
        ts_ms=1_100,
        bids=[(0.49, 20)],
        asks=[(0.50, 20)],
    )
    assert books.insert(
        condition_id="cond", combo_key="ETH:5m", side="UP",
        snapshot=up, recv_ts_ms=detected - 20,
    )
    assert books.insert(
        condition_id="cond", combo_key="ETH:5m", side="DOWN",
        snapshot=down, recv_ts_ms=detected - 15,
    )
    rows = books.conn.execute(
        "SELECT id,side FROM p26_clob_books ORDER BY id"
    ).fetchall()
    ids = {str(row["side"]): int(row["id"]) for row in rows}
    books.close()

    recorder = P3Recorder(settings.p3_db_path)
    opp = StructuralOpportunity(
        strategy=ARB_BUY_MERGE,
        condition_id="cond",
        combo_key="ETH:5m",
        detected_ts_ms=detected,
        up_book_id=ids["UP"],
        down_book_id=ids["DOWN"],
        up_book_ts_ms=1_000,
        down_book_ts_ms=1_100,
        source_skew_ms=100,
        max_book_age_ms=detected - 1_000,
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
    opp_id, created = recorder.record_opportunity(opp)
    assert created
    recorder.close()
    return settings, opp_id, detected


def test_recv_clock_replay_keeps_unchanged_detection_book_executable(tmp_path):
    settings, opp_id, _ = _seed_opportunity(tmp_path)
    replay = ClockReplayEngine(settings)
    try:
        result = replay.replay_one(opp_id, 10)
        assert result.outcome == "BOTH_FILLED"
        assert result.both_fill is True
        assert result.cycle_net_pnl_usdc == 1.0
        assert result.details["replay_version"] == REPLAY_VERSION
        assert result.details["time_axis"] == "recv_ts_ms_asof"
    finally:
        replay.close()


def test_scheduler_replaces_false_no_synchronous_book_legacy_rows(tmp_path):
    settings, opp_id, detected = _seed_opportunity(tmp_path)

    # Reproduce the production bug: detected_ts is wall-clock ~1,000,000 while
    # source_ts is ~1,000, so the old source-time replay cannot find a future row.
    legacy = LegacyReplayEngine(settings)
    try:
        old = legacy.replay_one(opp_id, 10)
        assert old.outcome == "NO_SYNCHRONOUS_BOOK"
        assert old.both_fill is False
    finally:
        legacy.close()

    scheduler = ScheduledReplayEngine(settings)
    try:
        result = scheduler.process_ready(now_ms=detected + 1_000)
        assert result["legacy_replays_purged"] == 1
        row = scheduler.p3.execute(
            "SELECT outcome,both_fill,details_json FROM p3_replays WHERE opportunity_id=? AND delay_ms=10",
            (opp_id,),
        ).fetchone()
        assert row is not None
        assert str(row["outcome"]) == "BOTH_FILLED"
        assert int(row["both_fill"]) == 1
        assert REPLAY_VERSION in str(row["details_json"])
    finally:
        scheduler.close()
