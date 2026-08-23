from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from p3_complete_set import CompleteSetArbitrageEngine
from p3_config import P3Settings
from p3_models import ARB_BUY_MERGE, ARB_SPLIT_SELL
from p3_recorder import P3Recorder
from p3_replay import P3ReplayEngine
from p3_scanner import StructuralArbScanner
from p3_schema import connect_p3, ensure_p3_schema
from p3_web import build_summary
from p26_book_store import BookSnapshotStore
from p26_fee import FeeSchedule
from p26_models import BookLevel, BookSnapshot


def settings(tmp_path: Path, **overrides) -> P3Settings:
    values = {
        "p26_db_path": str(tmp_path / "p26.sqlite"),
        "p3_db_path": str(tmp_path / "p3.sqlite"),
        "reports_dir": str(tmp_path / "reports"),
        "scan_interval_ms": 250,
        "max_book_age_ms": 750,
        "max_source_skew_ms": 500,
        "replay_delays_ms": "10,25,50,100,200,500",
        "dry_latency_ms": 100,
        "web_host": "127.0.0.1",
        "web_port": 18093,
        "live_control_host": "127.0.0.1",
        "live_control_port": 18094,
    }
    values.update(overrides)
    return P3Settings(**values)


def book(
    token: str,
    ts: int,
    *,
    bids: tuple[tuple[float, float], ...],
    asks: tuple[tuple[float, float], ...],
) -> BookSnapshot:
    return BookSnapshot(
        token_id=token,
        recv_ts_ms=ts,
        source_ts_ms=ts,
        bids=tuple(BookLevel(price=p, size=s) for p, s in bids),
        asks=tuple(BookLevel(price=p, size=s) for p, s in asks),
        source="TEST",
        transport_connected=True,
        transport_stale=False,
        session_complete=True,
    )


def seed_p26(path: Path, *, ts: int = 1_000_000) -> None:
    store = BookSnapshotStore(str(path))
    store.ensure_schema()
    store.insert(
        condition_id="cond",
        combo_key="BTC:5m",
        side="UP",
        snapshot=book("up", ts, bids=((0.38, 100),), asks=((0.40, 100),)),
    )
    store.insert(
        condition_id="cond",
        combo_key="BTC:5m",
        side="DOWN",
        snapshot=book("down", ts, bids=((0.48, 100),), asks=((0.50, 100),)),
    )
    store.close()


# ---------------------------------------------------------------------------
# P3.1/P3.2/P3.3 complete-set and recorder regressions
# ---------------------------------------------------------------------------

def test_p31_buy_merge_positive_complete_set(tmp_path):
    s = settings(tmp_path)
    engine = CompleteSetArbitrageEngine(s)
    fee = FeeSchedule(rate_bps=0, source="TEST")
    result = engine.evaluate_buy_merge(
        condition_id="cond",
        combo_key="BTC:5m",
        up_book=book("up", 1_000_000, bids=((0.38, 100),), asks=((0.40, 100),)),
        down_book=book("down", 1_000_000, bids=((0.48, 100),), asks=((0.50, 100),)),
        up_fee=fee,
        down_fee=fee,
        now_ms=1_000_000,
    )
    assert result is not None
    assert result.strategy == ARB_BUY_MERGE
    assert result.net_profit_usdc > 0
    assert result.capital_usdc > 0
    assert result.net_roi > 0


def test_p31_buy_merge_rejects_nonpositive(tmp_path):
    s = settings(tmp_path)
    engine = CompleteSetArbitrageEngine(s)
    fee = FeeSchedule(rate_bps=0, source="TEST")
    result = engine.evaluate_buy_merge(
        condition_id="cond",
        combo_key="BTC:5m",
        up_book=book("up", 1_000_000, bids=((0.5, 100),), asks=((0.55, 100),)),
        down_book=book("down", 1_000_000, bids=((0.5, 100),), asks=((0.55, 100),)),
        up_fee=fee,
        down_fee=fee,
        now_ms=1_000_000,
    )
    assert result is None


def test_p31_split_sell_positive_complete_set(tmp_path):
    s = settings(tmp_path)
    engine = CompleteSetArbitrageEngine(s)
    fee = FeeSchedule(rate_bps=0, source="TEST")
    result = engine.evaluate_split_sell(
        condition_id="cond",
        combo_key="BTC:5m",
        up_book=book("up", 1_000_000, bids=((0.56, 100),), asks=((0.58, 100),)),
        down_book=book("down", 1_000_000, bids=((0.48, 100),), asks=((0.50, 100),)),
        up_fee=fee,
        down_fee=fee,
        now_ms=1_000_000,
    )
    assert result is not None
    assert result.strategy == ARB_SPLIT_SELL
    assert result.net_profit_usdc > 0


def test_p31_respects_capital_and_quantity_caps(tmp_path):
    s = settings(tmp_path, max_quantity_shares=5, max_capital_per_cycle_usdc=3)
    engine = CompleteSetArbitrageEngine(s)
    fee = FeeSchedule(rate_bps=0, source="TEST")
    result = engine.evaluate_buy_merge(
        condition_id="cond",
        combo_key="BTC:5m",
        up_book=book("up", 1_000_000, bids=((0.38, 100),), asks=((0.40, 100),)),
        down_book=book("down", 1_000_000, bids=((0.48, 100),), asks=((0.50, 100),)),
        up_fee=fee,
        down_fee=fee,
        now_ms=1_000_000,
    )
    assert result is not None
    assert result.quantity_shares <= 5 + 1e-9
    assert result.capital_usdc <= 3 + 1e-9


def test_p32_recorder_deduplicates_opportunity_and_tracks_window(tmp_path):
    s = settings(tmp_path)
    engine = CompleteSetArbitrageEngine(s)
    fee = FeeSchedule(rate_bps=0, source="TEST")
    opportunity = engine.evaluate_buy_merge(
        condition_id="cond",
        combo_key="BTC:5m",
        up_book=book("up", 1_000_000, bids=((0.38, 100),), asks=((0.40, 100),)),
        down_book=book("down", 1_000_000, bids=((0.48, 100),), asks=((0.50, 100),)),
        up_fee=fee,
        down_fee=fee,
        now_ms=1_000_000,
    )
    assert opportunity is not None
    recorder = P3Recorder(s.p3_db_path)
    first = recorder.record_opportunity(opportunity)
    second = recorder.record_opportunity(opportunity)
    assert first is not None
    assert second is None
    assert recorder.conn.execute("SELECT COUNT(*) FROM p3_opportunities").fetchone()[0] == 1
    recorder.close()


def test_p33_scanner_finds_complete_set_and_isolates_database(tmp_path):
    s = settings(tmp_path)
    seed_p26(Path(s.p26_db_path))
    scanner = StructuralArbScanner(s)
    stats = scanner.scan_once(now_ms=1_000_000)
    assert stats.conditions == 1
    assert stats.valid_pairs == 1
    assert stats.positive_buy_merge == 1
    assert stats.inserted == 1
    scanner.close()
    p3 = sqlite3.connect(s.p3_db_path)
    try:
        assert p3.execute("SELECT COUNT(*) FROM p3_opportunities").fetchone()[0] == 1
    finally:
        p3.close()


def test_p33_scanner_rejects_stale_or_skewed_pair(tmp_path):
    s = settings(tmp_path, max_book_age_ms=100, max_source_skew_ms=50)
    store = BookSnapshotStore(s.p26_db_path)
    store.ensure_schema()
    store.insert(
        condition_id="cond", combo_key="BTC:5m", side="UP",
        snapshot=book("up", 900_000, bids=((0.38, 10),), asks=((0.40, 10),)),
    )
    store.insert(
        condition_id="cond", combo_key="BTC:5m", side="DOWN",
        snapshot=book("down", 1_000_000, bids=((0.48, 10),), asks=((0.50, 10),)),
    )
    store.close()
    scanner = StructuralArbScanner(s)
    stats = scanner.scan_once(now_ms=1_000_000)
    assert stats.valid_pairs == 0
    assert stats.stale_book > 0 or stats.source_skew > 0
    scanner.close()


# ---------------------------------------------------------------------------
# P3.4 replay regressions
# ---------------------------------------------------------------------------

def test_p34_replay_both_fill(tmp_path):
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
        snapshot=book("down", 1_000_100, bids=((0.48, 10),), asks=((0.50, 10),)),
    )
    books.close()
    replay = P3ReplayEngine(s)
    result = replay.replay_one(opp_id, 100)
    assert result.both_fill is True
    assert result.outcome == "BOTH_FILLED"
    assert result.cycle_net_pnl_usdc > 0
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
    # DRY research/core files must stay execution-client-free. LIVE integration is
    # explicitly isolated in p3_live_*.py and disabled by default in p3_config.py.
    core_paths = [
        path
        for path in Path(".").glob("p3_*.py")
        if not path.name.startswith("p3_live_")
        and path.name not in {"p3_daemon.py", "p3_web.py", "p3_config.py"}
    ]
    core_source = "\n".join(path.read_text(encoding="utf-8") for path in core_paths)
    for token in (
        "py_clob_client",
        "post_orders(",
        "create_order(",
        "place_order(",
        "POLYMARKET_PRIVATE_KEY",
    ):
        assert token not in core_source

    config = Path("p3_config.py").read_text(encoding="utf-8")
    assert "live_feature_enabled: bool = Field(default=False" in config
    assert "live_auto_execute_enabled: bool = Field(" in config
    assert "default=False, alias=\"P3_LIVE_AUTO_EXECUTE_ENABLED\"" in config
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
    for script in (
        "deploy_p3.sh",
        "scripts/status_p3.sh",
        "scripts/stop_p3.sh",
        "scripts/smoke_p3.sh",
    ):
        result = subprocess.run(["bash", "-n", script], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
