"""Regression tests for P2.6 sidecar task and SQLite ownership rules."""
from __future__ import annotations

import asyncio
import inspect

import pytest

import p26_dataset_daemon
import p26_oracle_daemon
from chainlink_feed import ChainlinkFeed
from p26_config import P26Settings
from p26_oracle_daemon import OracleBatchWriter, OracleRTDSSidecar
from p26_oracle_store import OracleTick, OracleTickStore


def _settings(tmp_path) -> P26Settings:
    return P26Settings(
        p25_db_path=str(tmp_path / "p25.sqlite"),
        p26_db_path=str(tmp_path / "p26.sqlite"),
        backup_root=str(tmp_path / "backups"),
        model_dir=str(tmp_path / "models"),
        reports_dir=str(tmp_path / "reports"),
        oracle_queue_max=20,
        oracle_batch_size=10,
        oracle_flush_interval_ms=10,
    )


def _tick(source_ts_ms: int = 1_800_000_000_000) -> OracleTick:
    return OracleTick(
        asset="BTC",
        source="POLYMARKET_RTDS_CHAINLINK",
        value_text="64000.25",
        value_real=64000.25,
        source_ts_ms=source_ts_ms,
        recv_ts_ms=source_ts_ms + 5,
        payload_sha256="a" * 64,
    )


@pytest.mark.asyncio
async def test_oracle_batch_writer_persists_using_owner_thread(tmp_path):
    settings = _settings(tmp_path)
    store = OracleTickStore(settings.p26_db_path)
    writer = OracleBatchWriter(settings, store)
    stop = asyncio.Event()
    try:
        await writer.enqueue(_tick())
        stop.set()
        await writer.run(stop)
        assert store.stats()["ticks"] == 1
        assert writer.inserted == 1
        assert writer.flushes == 1
        assert writer.queue.empty()
    finally:
        store.close()


def test_sidecars_do_not_send_persistent_sqlite_connections_to_worker_threads():
    oracle_source = inspect.getsource(p26_oracle_daemon)
    dataset_source = inspect.getsource(p26_dataset_daemon)
    assert "asyncio.to_thread(self.store.insert_many" not in oracle_source
    assert "asyncio.to_thread(builder.sync" not in dataset_source
    assert "inserted = self.store.insert_many(batch)" in oracle_source
    assert "result = builder.sync()" in dataset_source


def test_p26_subscription_matches_proven_p25_chainlink_wire_contract(tmp_path):
    settings = _settings(tmp_path)
    store = OracleTickStore(settings.p26_db_path)
    try:
        writer = OracleBatchWriter(settings, store)
        assert OracleRTDSSidecar.subscribe_message() == ChainlinkFeed.subscribe_message()
    finally:
        store.close()


def test_oracle_run_supervises_writer_and_websocket_tasks():
    source = inspect.getsource(p26_oracle_daemon.run)
    assert "asyncio.FIRST_COMPLETED" in source
    assert "P2.6 oracle task crashed" in source
    assert "return_exceptions=True" in source
