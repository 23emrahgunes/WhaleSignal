from __future__ import annotations

import sqlite3
from pathlib import Path

from p3_config import P3Settings
from p3_models import ARB_BUY_MERGE, StructuralOpportunity
from p3_recorder import P3Recorder
from p3_replay_scheduler import P3ReplayEngine


def _opp(condition: str, detected: int) -> StructuralOpportunity:
    return StructuralOpportunity(
        strategy=ARB_BUY_MERGE,
        condition_id=condition,
        combo_key="BTC:5m",
        detected_ts_ms=detected,
        up_book_id=1,
        down_book_id=2,
        up_book_ts_ms=detected,
        down_book_ts_ms=detected,
        source_skew_ms=0,
        max_book_age_ms=0,
        quantity_shares=1.0,
        up_vwap=0.4,
        down_vwap=0.5,
        up_fee_usdc=0.0,
        down_fee_usdc=0.0,
        gross_edge_per_share=0.1,
        gross_profit_usdc=0.1,
        execution_buffer_usdc=0.0,
        net_profit_usdc=0.1,
        capital_usdc=0.9,
        net_roi=0.1 / 0.9,
        up_limit_price=0.4,
        down_limit_price=0.5,
        fee_lineage_ok=True,
    )


def test_scheduler_does_not_starve_later_opportunities(tmp_path, monkeypatch):
    p26 = tmp_path / "p26.sqlite"
    sqlite3.connect(p26).close()
    p3 = tmp_path / "p3.sqlite"
    settings = P3Settings(
        p26_db_path=str(p26),
        p3_db_path=str(p3),
        replay_delays_ms="10,20",
        replay_snapshot_tolerance_ms=5,
        replay_batch_size=1,
        web_port=18093,
    )
    recorder = P3Recorder(str(p3))
    first_id, _ = recorder.record_opportunity(_opp("c1", 1000))
    second_id, _ = recorder.record_opportunity(_opp("c2", 1000))
    for delay in (10, 20):
        recorder.conn.execute(
            """
            INSERT INTO p3_replays(
                opportunity_id,delay_ms,target_ts_ms,observed_ts_ms,strategy,
                quantity_shares,up_fill,down_fill,both_fill,outcome,
                details_json,created_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (first_id, delay, 1000 + delay, None, ARB_BUY_MERGE, 1.0, 0, 0, 0, "DONE", "{}", 1),
        )
    recorder.conn.commit()
    recorder.close()

    engine = P3ReplayEngine(settings)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(engine, "replay_one", lambda oid, delay: calls.append((oid, delay)))
    result = engine.process_ready(now_ms=5000)
    assert result["opportunities_scanned"] == 1
    assert calls == [(second_id, 10), (second_id, 20)]
    engine.close()
