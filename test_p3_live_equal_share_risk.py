from __future__ import annotations

import sqlite3
import time

import pytest

from p3_config import P3Settings
from p3_live_executor_v2 import P3LiveExecutorV2
from p3_live_ledger import (
    create_live_ledger_row,
    ensure_live_ledger_schema,
    finalize_live_ledger_row,
)
from p3_live_sizing import consume_depth, select_equal_share_quantity
from p3_live_state import LiveState, MODE_LIVE_ARMED, MODE_LIVE_HALTED
from p3_models import ARB_BUY_MERGE
from p3_schema import connect_p3, ensure_p3_schema


def _settings(tmp_path, **overrides) -> P3Settings:
    values = {
        "p3_db_path": str(tmp_path / "p3.sqlite"),
        "p26_db_path": str(tmp_path / "p26.sqlite"),
        "live_feature_enabled": True,
        "live_auto_execute_enabled": True,
        "live_require_dry_validated": False,
        "live_target_quantity_shares": 5.0,
        "live_max_quantity_shares": 10.0,
        "live_max_capital_per_cycle_usdc": 0.0,
        "live_min_collateral_to_arm_usdc": 5.0,
        "live_max_single_leg_notional_usdc": 5.25,
        "live_max_projected_unwind_loss_usdc": 0.25,
        "live_emergency_unwind_loss_usdc": 0.50,
        "live_min_edge_to_unwind_loss_ratio": 0.10,
        "live_halt_after_one_leg": True,
        "live_rolling_24h_gross_loss_limit_usdc": 2.0,
        "web_host": "127.0.0.1",
        "web_port": 18093,
        "web_auth_required": True,
        "web_password": "test-password-12345",
    }
    values.update(overrides)
    return P3Settings(**values)


def _armed() -> LiveState:
    state = LiveState(live_feature_enabled=True, auto_execute_enabled=True)
    state.arm({"ok": True, "checked_at_ms": int(time.time() * 1000), "reasons": []})
    assert state.snapshot().mode == MODE_LIVE_ARMED
    return state


def _insert_opp(conn, *, key: str, ts: int, q: float = 20.0) -> int:
    capital = q * 0.90
    profit = q * 0.10
    cur = conn.execute(
        """
        INSERT INTO p3_opportunities(
            opportunity_key,strategy,condition_id,combo_key,detected_ts_ms,
            up_book_id,down_book_id,up_book_ts_ms,down_book_ts_ms,
            source_skew_ms,max_book_age_ms,quantity_shares,up_vwap,down_vwap,
            up_fee_usdc,down_fee_usdc,gross_edge_per_share,gross_profit_usdc,
            execution_buffer_usdc,net_profit_usdc,capital_usdc,net_roi,
            up_limit_price,down_limit_price,fee_lineage_ok,quality_status,
            payload_json,created_at_ms
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            key, ARB_BUY_MERGE, "cond-live-v2", "BTC:5m", ts,
            1, 2, ts, ts, 0, 0, q, 0.40, 0.50,
            0.0, 0.0, 0.10, profit, 0.0, profit, capital,
            profit / capital, 0.40, 0.50, 1, "OK", "{}", ts,
        ),
    )
    return int(cur.lastrowid)


def _seed(settings: P3Settings, state: LiveState) -> int:
    snap = state.snapshot()
    assert snap.armed_at_ms is not None
    t0 = int(snap.armed_at_ms) + 1
    t1 = t0 + int(settings.dry_entry_confirm_ms)
    conn = connect_p3(settings.p3_db_path)
    ensure_p3_schema(conn)
    try:
        opp0 = _insert_opp(conn, key=f"open-{t0}", ts=t0)
        opp1 = _insert_opp(conn, key=f"confirm-{t1}", ts=t1)
        cur = conn.execute(
            """
            INSERT INTO p3_windows(
                strategy,condition_id,combo_key,opened_ts_ms,last_seen_ts_ms,
                observations,peak_net_profit_usdc,peak_net_roi,peak_quantity_shares,
                peak_opportunity_id,status
            ) VALUES(?,?,?,?,?,2,?,?,?,?, 'OPEN')
            """,
            (ARB_BUY_MERGE, "cond-live-v2", "BTC:5m", t0, t1, 2.0, 2.0 / 18.0, 20.0, opp1),
        )
        window_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO p3_window_observations(
                window_id,opportunity_id,observed_ts_ms,created_at_ms
            ) VALUES(?,?,?,?)
            """,
            [(window_id, opp0, t0, t0), (window_id, opp1, t1, t1)],
        )
        conn.commit()
    finally:
        conn.close()

    raw = sqlite3.connect(settings.p26_db_path)
    try:
        raw.execute(
            """
            CREATE TABLE p26_clob_books(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT NOT NULL,side TEXT NOT NULL,token_id TEXT NOT NULL
            )
            """
        )
        raw.executemany(
            "INSERT INTO p26_clob_books(condition_id,side,token_id) VALUES(?,?,?)",
            [
                ("cond-live-v2", "UP", "up-old"),
                ("cond-live-v2", "DOWN", "down-old"),
                ("cond-live-v2", "UP", "up-new"),
                ("cond-live-v2", "DOWN", "down-new"),
            ],
        )
        raw.execute(
            """
            CREATE TABLE p26_fee_schedules(
                condition_id TEXT NOT NULL,token_id TEXT NOT NULL,enabled INTEGER NOT NULL,
                rate REAL NOT NULL,exponent REAL NOT NULL,taker_only INTEGER NOT NULL,
                source TEXT NOT NULL,source_ts_ms INTEGER NOT NULL
            )
            """
        )
        raw.executemany(
            "INSERT INTO p26_fee_schedules VALUES(?,?,?,?,?,?,?,?)",
            [
                ("cond-live-v2", "up-new", 0, 0.0, 1.0, 1, "TEST", t1),
                ("cond-live-v2", "down-new", 0, 0.0, 1.0, 1, "TEST", t1),
            ],
        )
        raw.commit()
    finally:
        raw.close()
    return window_id


class _Gateway:
    mode = "both"
    unsafe_bids = False
    first_unwind_fails = False
    second_unwind_fails = False
    emergency_residual = 0.0
    final_collateral = 100.45

    def __init__(self, settings) -> None:
        self.settings = settings
        self.calls: list[tuple] = []
        self.unwind_verifications = 0
        bid_up = 0.39 if not self.unsafe_bids else 0.20
        bid_down = 0.49 if not self.unsafe_bids else 0.20
        self.up_book = {
            "asks": [{"price": "0.40", "size": "50"}],
            "bids": [{"price": str(bid_up), "size": "50"}],
            "min_order_size": "1",
        }
        self.down_book = {
            "asks": [{"price": "0.50", "size": "50"}],
            "bids": [{"price": str(bid_down), "size": "50"}],
            "min_order_size": "1",
        }

    @staticmethod
    def _levels(book, name):
        return [(float(x["price"]), float(x["size"])) for x in book[name]]

    def fetch_pair_books(self, *, up_token_id, down_token_id):
        assert up_token_id == "up-new" and down_token_id == "down-new"
        self.calls.append(("pair_books",))
        return self.up_book, self.down_book

    def buy_capacity_from_book(self, book, *, max_price):
        levels = self._levels(book, "asks")
        return {
            "capacity_shares": sum(s for p, s in levels if p <= max_price),
            "min_order_size": float(book["min_order_size"]),
        }

    def quote_buy_from_book(self, book, *, shares, max_price):
        return consume_depth(
            self._levels(book, "asks"), shares=shares, buy=True,
            price_limit=max_price, min_order_size=float(book["min_order_size"]),
        )

    def quote_sell_from_book(self, book, *, shares, min_price=None):
        return consume_depth(
            self._levels(book, "bids"), shares=shares, buy=False,
            price_limit=min_price, min_order_size=float(book["min_order_size"]),
        )

    def quote_sell(self, *, token_id, shares, min_price=None):
        book = self.up_book if token_id == "up-new" else self.down_book
        return self.quote_sell_from_book(book, shares=shares, min_price=min_price)

    def collateral_balance_usdc(self, *, refresh=False):
        return 100.0

    def conditional_balance_shares(self, token_id, *, refresh=True):
        return 0.0

    def post_two_leg_fok(self, **kwargs):
        self.calls.append(("post", kwargs))
        assert kwargs["quantity_shares"] == pytest.approx(5.0)
        assert kwargs["up_limit_price"] == pytest.approx(0.40)
        assert kwargs["down_limit_price"] == pytest.approx(0.50)
        return {"up_order_id": "u", "down_order_id": "d"}

    def wait_for_leg_deltas(self, **kwargs):
        q = float(kwargs["quantity_shares"])
        if self.mode == "both":
            return {"up_verified": True, "down_verified": True, "up_delta": q, "down_delta": q}
        if self.mode == "one":
            return {"up_verified": True, "down_verified": False, "up_delta": q, "down_delta": 0.0}
        return {"up_verified": False, "down_verified": False, "up_delta": 0.0, "down_delta": 0.0}

    def merge_positions(self, **kwargs):
        self.calls.append(("merge", kwargs))
        return {"verified": True, "transaction_hash": "0xmerge"}

    def wait_for_merge(self, **kwargs):
        return {"verified": True}

    def unwind_limit_fok(self, **kwargs):
        self.calls.append(("limit_unwind", kwargs))
        return {"order_id": f"limit-{len(self.calls)}", "kind": "LIMIT_FOK"}

    def emergency_unwind_fak(self, **kwargs):
        self.calls.append(("fak", kwargs))
        return {"order_id": "emergency-fak", "kind": "MARKET_FAK_EMERGENCY"}

    def wait_for_unwind(self, **kwargs):
        self.unwind_verifications += 1
        if self.unwind_verifications == 1 and self.first_unwind_fails:
            return {"verified": False, "residual": 5.0}
        if self.unwind_verifications == 2 and self.second_unwind_fails:
            return {"verified": False, "residual": 5.0}
        if self.unwind_verifications >= 3 and self.emergency_residual > 0:
            return {"verified": False, "residual": self.emergency_residual}
        return {"verified": True, "residual": 0.0}

    def wait_for_collateral_stable(self):
        return float(self.final_collateral)


def _factory(cls, holder):
    def create(settings):
        g = cls(settings)
        holder.append(g)
        return g
    return create


def test_equal_share_selector_never_uses_dollar_ratio() -> None:
    assert select_equal_share_quantity(
        strict_optimal_shares=20,
        target_shares=5,
        hard_max_shares=10,
        up_capacity_shares=100,
        down_capacity_shares=100,
        min_order_size=1,
    ) == pytest.approx(5.0)
    assert select_equal_share_quantity(
        strict_optimal_shares=20,
        target_shares=5,
        hard_max_shares=10,
        up_capacity_shares=3.5,
        down_capacity_shares=4,
        min_order_size=1,
    ) == pytest.approx(3.5)


def test_executor_posts_five_equal_shares_and_records_realized_pnl(tmp_path) -> None:
    settings = _settings(tmp_path)
    state = _armed()
    window_id = _seed(settings, state)
    holder = []
    executor = P3LiveExecutorV2(settings, state, gateway_factory=_factory(_Gateway, holder))
    result = executor.process_once()
    assert result["status"] == "MERGED_VERIFIED"
    assert result["quantity_shares_each_leg"] == pytest.approx(5.0)
    assert result["realized_pnl_usdc"] == pytest.approx(0.45)
    assert state.snapshot().mode == MODE_LIVE_ARMED
    post = next(x for x in holder[0].calls if x[0] == "post")[1]
    assert post["quantity_shares"] == pytest.approx(5.0)

    conn = connect_p3(settings.p3_db_path)
    try:
        row = conn.execute(
            "SELECT quantity_shares,capital_usdc,status FROM p3_live_cycles WHERE window_id=?",
            (window_id,),
        ).fetchone()
        assert row["quantity_shares"] == pytest.approx(5.0)
        assert row["capital_usdc"] == pytest.approx(4.5)
        assert row["status"] == "MERGED_VERIFIED"
        ledger = conn.execute("SELECT * FROM p3_live_ledger").fetchone()
        assert ledger["realized_pnl_usdc"] == pytest.approx(0.45)
    finally:
        conn.close()


def test_entry_is_rejected_when_single_leg_cannot_be_unwound_cheaply(tmp_path) -> None:
    class Unsafe(_Gateway):
        unsafe_bids = True

    settings = _settings(tmp_path)
    state = _armed()
    _seed(settings, state)
    holder = []
    result = P3LiveExecutorV2(settings, state, gateway_factory=_factory(Unsafe, holder)).process_once()
    assert result["status"] == "SKIPPED_PROJECTED_UNWIND_LOSS"
    assert not any(call[0] == "post" for call in holder[0].calls)
    assert state.snapshot().mode == MODE_LIVE_ARMED


def test_one_leg_flattens_then_halts_for_review(tmp_path) -> None:
    class One(_Gateway):
        mode = "one"
        final_collateral = 99.95

    settings = _settings(tmp_path)
    state = _armed()
    _seed(settings, state)
    holder = []
    result = P3LiveExecutorV2(settings, state, gateway_factory=_factory(One, holder)).process_once()
    assert result["status"] == "ONE_LEG_UNWOUND_VERIFIED_HALTED"
    assert result["realized_pnl_usdc"] == pytest.approx(-0.05)
    assert state.snapshot().mode == MODE_LIVE_HALTED
    assert any(call[0] == "limit_unwind" for call in holder[0].calls)


def test_failed_bounded_exits_use_emergency_fak_and_halt(tmp_path) -> None:
    class Emergency(_Gateway):
        mode = "one"
        first_unwind_fails = True
        second_unwind_fails = True
        final_collateral = 99.80

    settings = _settings(tmp_path)
    state = _armed()
    _seed(settings, state)
    holder = []
    result = P3LiveExecutorV2(settings, state, gateway_factory=_factory(Emergency, holder)).process_once()
    assert result["status"] == "ONE_LEG_UNWOUND_VERIFIED_HALTED"
    assert any(call[0] == "fak" for call in holder[0].calls)
    assert state.snapshot().mode == MODE_LIVE_HALTED


def test_residual_exposure_halts_fail_closed(tmp_path) -> None:
    class Residual(_Gateway):
        mode = "one"
        first_unwind_fails = True
        second_unwind_fails = True
        emergency_residual = 1.0
        final_collateral = 98.0

    settings = _settings(tmp_path)
    state = _armed()
    _seed(settings, state)
    holder = []
    result = P3LiveExecutorV2(settings, state, gateway_factory=_factory(Residual, holder)).process_once()
    assert result["status"] == "HALTED_RESIDUAL_EXPOSURE"
    assert result["residuals"][0]["residual"] == pytest.approx(1.0)
    assert state.snapshot().mode == MODE_LIVE_HALTED


def test_rolling_24h_gross_loss_halts_before_new_order(tmp_path) -> None:
    settings = _settings(tmp_path, live_rolling_24h_gross_loss_limit_usdc=2.0)
    state = _armed()
    _seed(settings, state)
    conn = connect_p3(settings.p3_db_path)
    ensure_p3_schema(conn)
    ensure_live_ledger_schema(conn)
    try:
        # Create a synthetic completed historical cycle solely for the loss-budget test.
        now = int(time.time() * 1000)
        opp = conn.execute("SELECT id FROM p3_opportunities ORDER BY id LIMIT 1").fetchone()[0]
        win = conn.execute("SELECT id FROM p3_windows ORDER BY id LIMIT 1").fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO p3_live_cycles(
                session_id,window_id,opportunity_id,strategy,condition_id,combo_key,
                entry_ts_ms,quantity_shares,capital_usdc,up_token_id,down_token_id,
                up_limit_price,down_limit_price,status,details_json,created_at_ms,updated_at_ms
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("old", win, opp, ARB_BUY_MERGE, "cond-live-v2", "BTC:5m", now,
             5.0, 4.5, "u", "d", 0.4, 0.5, "DONE", "{}", now, now),
        )
        cycle = int(cur.lastrowid)
        conn.commit()
        create_live_ledger_row(
            conn, cycle_id=cycle, session_id="old", window_id=win, combo_key="BTC:5m",
            quantity_shares=5, planned_capital_usdc=4.5, planned_net_profit_usdc=0.5,
            planned_net_roi=0.1, projected_worst_unwind_loss_usdc=0.1,
            collateral_before_usdc=100.0,
        )
        finalize_live_ledger_row(
            conn, cycle_id=cycle, outcome="LOSS", collateral_after_usdc=98.0,
            one_leg_event=True, unwind_attempts=1,
        )
    finally:
        conn.close()

    holder = []
    result = P3LiveExecutorV2(settings, state, gateway_factory=_factory(_Gateway, holder)).process_once()
    assert result["status"] == "HALTED_ROLLING_24H_LOSS_LIMIT"
    assert state.snapshot().mode == MODE_LIVE_HALTED
    assert holder == []
