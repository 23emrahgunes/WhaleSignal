from p26_book_store import BookSnapshotStore
from p26_execution import OrderBookSnapshot


def test_identical_reconnect_snapshot_refreshes_recv_without_duplicate(tmp_path):
    db = str(tmp_path / "p26.sqlite")
    store = BookSnapshotStore(db)
    try:
        snapshot = OrderBookSnapshot.from_levels(
            token_id="up",
            ts_ms=1_000,
            bids=[(0.49, 10)],
            asks=[(0.51, 10)],
        )
        assert store.insert(
            condition_id="cond",
            combo_key="BTC:5m",
            side="UP",
            snapshot=snapshot,
            recv_ts_ms=1_100,
        )
        assert store.insert(
            condition_id="cond",
            combo_key="BTC:5m",
            side="UP",
            snapshot=snapshot,
            recv_ts_ms=5_000,
        )
        row = store.conn.execute(
            "SELECT COUNT(*) AS n,MAX(recv_ts_ms) AS recv FROM p26_clob_books"
        ).fetchone()
        assert int(row["n"]) == 1
        assert int(row["recv"]) == 5_000
    finally:
        store.close()
