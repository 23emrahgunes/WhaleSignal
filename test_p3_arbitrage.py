from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from p26_book_store import BookSnapshotStore
from p26_execution import OrderBookSnapshot
from p26_fee import FeeSchedule, FeeScheduleStore
from p3_complete_set import BookPair, best_buy_merge, best_split_sell
from p3_config import P3Settings
from p3_models import ARB_BUY_MERGE, ARB_SPLIT_SELL
from p3_recorder import P3Recorder
from p3_replay import P3ReplayEngine
from p3_scanner import StructuralArbScanner
from p3_schema import connect_p3, ensure_p3_schema, integrity_check
from p3_web import build_summary


def fee(condition: str, token: str, *, enabled: bool = False, rate: float = 0.0) -> FeeSchedule:
    return FeeSchedule(
        condition_id=condition,
        token_id=token,
        enabled=enabled,
        rate=rate,
        exponent=1.0,
        taker_only=True,
        source="TEST",
        source_ts_ms=1,
    )


def book(token: str, ts: int, *, bids, asks) -> OrderBookSnapshot:
    return OrderBookSnapshot.from_levels(token_id=token, ts_ms=ts, bids=bids, asks=asks)


def pair(*, ts: int = 1000, up_asks=((0.40, 10),), down_asks=((0.50, 10),), up_bids=((0.39, 10),), down_bids=((0.49, 10),)) -> BookPair:
    return BookPair(
        condition_id="cond",
        combo_key="BTC:5m",
        up_book_id=1,
        down_book_id=2,
        up=book("up", ts, bids=up_bids, asks=up_asks),
        down=book("down", ts, bids=down_bids, asks=down_asks),
    )


def settings(tmp_path: Path, **kwargs) -> P3Settings:
    values = dict(
        p26_db_path=str(tmp_path / "p26.sqlite"),
        p3_db_path=str(tmp_path / "p3.sqlite"),
        reports_dir=str(tmp_path / "reports"),
        scan_interval_ms=250,
        max_book_age_ms=750,
        max_source_skew_ms=500,
        replay_delays_ms="100,200",
        replay_snapshot_tolerance_ms=100,
        web_port=18093,
    )
    values.update(kwargs)
    return P3Settings(**values)


def seed_p26(
    path: Path,
    *,
    condition_id: str = "cond",
    combo_key: str = "BTC:5m",
    ts: int = 1_000_000,
    up_asks=((0.40, 10),),
    down_asks=((0.50, 10),),
    up_bids=((0.39, 10),),
    down_bids=((0.49, 10),),
    fee_rate: float = 0.0,
) -> None:
    fees = FeeScheduleStore(str(path))
    fees.upsert_market_info(
        condition_id=condition_id,
        combo_key=combo_key,
        market_end_ts_ms=ts + 300_000,
        payload={
            "t": [{"t": "up", "o": "UP"}, {"t": "down", "o": "DOWN"}],
            "fd": {"r": fee_rate, "e": 1.0, "to": True},
        },
        source_ts_ms=ts,
        source="TEST",
    )
    fees.close()
    books = BookSnapshotStore(str(path))
    books.insert(
        condition_id=condition_id,
        combo_key=combo_key,
        side="UP",
        snapshot=book("up", ts, bids=up_bids, asks=up_asks),
    )
    books.insert(
        condition_id=condition_id,
        combo_key=combo_key,
        side="DOWN",
        snapshot=book("down", ts, bids=down_bids, asks=down_asks),
    )
    books.close()


def test_p30_schema_isolated_and_integrity(tmp_path):
    s = settings(tmp_path)
    s.validate_research_safety()
    conn = connect_p3(s.p3_db_path)
    ensure_p3_schema(conn)
    assert integrity_check(conn) == "ok"
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"p3_opportunities", "p3_windows", "p3_replays", "p3_health_events"} <= tables
    conn.close()
    with pytest.raises(ValueError, match="separate"):
        P3Settings(p26_db_path="same.sqlite", p3_db_path="same.sqlite").validate_research_safety()


def test_p31_buy_merge_is_model_free_and_profitable_after_depth():
    p = pair()
    result = best_buy_merge(
        p,
        up_fee=fee("cond", "up"),
        down_fee=fee("cond", "down"),
        detected_ts_ms=1000,
        max_quantity_shares=10,
    )
    assert result is not None
    assert result.strategy == ARB_BUY_MERGE
    assert result.quantity_shares == pytest.approx(10)
    assert result.net_profit_usdc == pytest.approx(1.0)
    assert result.net_roi > 0


def test_p31_dynamic_fee_can_destroy_fake_topline_arb():
    p = pair(up_asks=((0.49, 10),), down_asks=((0.49, 10),))
    result = best_buy_merge(
        p,
        up_fee=fee("cond", "up", enabled=True, rate=0.07),
        down_fee=fee("cond", "down", enabled=True, rate=0.07),
        detected_ts_ms=1000,
        max_quantity_shares=10,
    )
    assert result is not None
    assert result.gross_edge_per_share == pytest.approx(0.02)
    assert result.net_profit_usdc < 0


def test_p32_split_sell_scanner_finds_reverse_parity():
    p = pair(up_bids=((0.55, 8),), down_bids=((0.56, 8),))
    result = best_split_sell(
        p,
        up_fee=fee("cond", "up"),
        down_fee=fee("cond", "down"),
        detected_ts_ms=1000,
        max_quantity_shares=8,
    )
    assert result is not None
    assert result.strategy == ARB_SPLIT_SELL
    assert result.net_profit_usdc == pytest.approx(0.88)


def test_p31_depth_optimizer_uses_equal_shares_and_best_total_profit():
    p = pair(
        up_asks=((0.40, 2), (0.48, 10)),
        down_asks=((0.50, 12),),
    )
    result = best_buy_merge(
        p,
        up_fee=fee("cond", "up"),
        down_fee=fee("cond", "down"),
        detected_ts_ms=1000,
        max_quantity_shares=12,
    )
    assert result is not None
    assert result.quantity_shares == pytest.approx(12)
    assert result.up_vwap == pytest.approx((0.4 * 2 + 0.48 * 10) / 12)
    assert result.down_vwap == pytest.approx(0.50)


def test_p33_scanner_records_and_closes_lifetime_window(tmp_path):
    s = settings(tmp_path, window_grace_ms=500)
    seed_p26(Path(s.p26_db_path), ts=1_000_000)
    scanner = StructuralArbScanner(s)
    first = scanner.scan_once(now_ms=1_000_100)
    assert first.positive_buy_merge == 1
    assert first.inserted == 1

    books = BookSnapshotStore(s.p26_db_path)
    books.insert(
        condition_id="cond", combo_key="BTC:5m", side="UP",
        snapshot=book("up", 1_000_300, bids=((0.39, 10),), asks=((0.60, 10),)),
    )
    books.insert(
        condition_id="cond", combo_key="BTC:5m", side="DOWN",
        snapshot=book("down", 1_000_300, bids=((0.39, 10),), asks=((0.60, 10),)),
    )
    books.close()
    mid = scanner.scan_once(now_ms=1_000_400)
    assert mid.windows_closed == 0
    last = scanner.scan_once(now_ms=1_000_800)
    assert last.windows_closed == 1
    row = scanner.recorder.conn.execute("SELECT * FROM p3_windows").fetchone()
    assert row["status"] == "CLOSED"
    assert int(row["closed_ts_ms"]) - int(row["opened_ts_ms"]) == 700
    scanner.close()


def test_p34_replay_measures_pair_completion_and_one_leg_unwind(tmp_path):
    s = settings(tmp_path, replay_delays_ms="100", replay_snapshot_tolerance_ms=50)
    seed_p26(Path(s.p26_db_path), ts=1_000_000)
    scanner = StructuralArbScanner(s)
    scanner.scan_once(now_ms=1_000_000)
    opp_id = int(scanner.recorder.conn.execute(
        "SELECT id FROM p3_opportunities WHERE strategy=?", (ARB_BUY_MERGE,)
    ).fetchone()[0])
    scanner.close()

    books = BookSnapshotStore(s.p26_db_path)
    books.insert(
        condition_id="cond", combo_key="BTC:5m", side="UP",
        snapshot=book("up", 1_000_100, bids=((0.39, 10),), asks=((0.40, 10),)),
    )
    books.insert(
        condition_id="cond", combo_key="BTC:5m", side="DOWN",
        snapshot=book("down", 1_000_100, bids=((0.49, 10),), asks=((0.50, 10),)),
    )
    books.close()
    replay = P3ReplayEngine(s)
    result = replay.replay_one(opp_id, 100)
    assert result.both_fill is True
    assert result.outcome == "BOTH_FILLED"
    assert result.cycle_net_pnl_usdc == pytest.approx(1.0)
    replay.close()


def test_p34_replay_does_not_call_one_leg_atomic(tmp_path):
    s = settings(tmp_path, replay_delays_ms="100", replay_snapshot_tolerance_ms=50)
    seed_p26(Path(s.p26_db_path), ts=1_000_000)
    scanner = StructuralArbScanner(s)
    scanner.scan_once(now_ms=1_000_000)
    opp_id = int(scanner.recorder.conn.execute(
        "SELECT id FROM p3_opportunities WHERE strategy=?", (ARB_BUY_MERGE,)
    ).fetchone()[0])
    scanner.close()
    books = BookSnapshotStore(s.p26_db_path)
    books.insert(
        condition_id="cond", combo_key="BTC:5m", side="UP",
        snapshot=book("up", 1_000_100, bids=((0.38, 10),), asks=((0.40, 10),)),
    )
    books.insert(
        condition_id="cond", combo_key="BTC:5m", side="DOWN",
        snapshot=book("down", 1_000_100, bids=((0.49, 10),), asks=((0.60, 10),)),
    )
    books.close()
    replay = P3ReplayEngine(s)
    result = replay.replay_one(opp_id, 100)
    assert result.both_fill is False
    assert result.up_fill is True and result.down_fill is False
    assert result.outcome == "ONE_LEG_FILLED_UNWIND"
    assert result.cycle_net_pnl_usdc < 0
    replay.close()


def test_p35_summary_defaults_to_dry_and_reports_replay(tmp_path):
    s = settings(tmp_path)
    conn = connect_p3(s.p3_db_path); ensure_p3_schema(conn); conn.close()
    summary = build_summary(s)
    assert summary["ok"] is True
    assert summary["mode"] == "DRY"
    assert summary["execution_enabled"] is False
    assert summary["order_submission_enabled"] is False
    assert summary["live"]["live_feature_enabled"] is False


def test_p3_static_safety_and_shell_syntax():
    # Keep the DRY research core execution-client-free. Actual live integrations are
    # isolated in p3_live_*.py, opt-in, and default disabled.
    core_paths = [
        path
        for path in Path(".").glob("p3_*.py")
        if not path.name.startswith("p3_live_")
        and path.name not in {"p3_daemon.py", "p3_web.py", "p3_config.py"}
    ]
    core_source = "\n".join(path.read_text(encoding="utf-8") for path in core_paths)
    forbidden = (
        "py_clob_client",
        "submit_order(",
        "create_order(",
        "place_order(",
        "POLYMARKET_PRIVATE_KEY",
        "private_key =",
    )
    for token in forbidden:
        assert token not in core_source

    config = Path("p3_config.py").read_text(encoding="utf-8")
    assert "live_feature_enabled: bool = Field(default=False" in config
    assert "P3_LIVE_AUTO_EXECUTE_ENABLED" in config
    assert 'live_control_host: str = Field(default="127.0.0.1"' in config
    assert "P3 LIVE v1 only supports BUY+MERGE" in config

    public_web = Path("p3_web.py").read_text(encoding="utf-8")
    assert 'web.post("/api/arm"' not in public_web
    assert 'web.post("/api/disarm"' not in public_web

    control = Path("p3_live_control.py").read_text(encoding="utf-8")
    assert "X-P3-Control-Token" in control
    assert 'web.post("/api/arm"' in control
    assert 'web.post("/api/disarm"' in control

    deploy = Path("deploy_p3.sh").read_text(encoding="utf-8")
    assert "systemctl stop direction-engine-p25" not in deploy
    assert "systemctl stop direction-engine-p26" not in deploy
    assert "p3_daemon.py" in deploy
    for script in ("deploy_p3.sh", "scripts/status_p3.sh", "scripts/stop_p3.sh", "scripts/smoke_p3.sh"):
        result = subprocess.run(["bash", "-n", script], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
