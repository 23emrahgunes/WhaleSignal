from __future__ import annotations

import sqlite3
import time

from p3_config import P3Settings
from p3_live_executor_resolved import P3LiveExecutor
from p3_live_state import LiveState, MODE_LIVE_ARMED, MODE_LIVE_HALTED
from p3_models import ARB_BUY_MERGE
from p3_schema import connect_p3, ensure_p3_schema


def _settings(tmp_path) -> P3Settings:
    return P3Settings(
        p3_db_path=str(tmp_path / "p3.sqlite"),
        p26_db_path=str(tmp_path / "p26.sqlite"),
        live_feature_enabled=True,
        live_auto_execute_enabled=True,
        live_require_dry_validated=False,
        live_max_capital_per_cycle_usdc=1.0,
        live_max_quantity_shares=10.0,
        live_min_net_profit_usdc=0.01,
        live_min_net_roi=0.0025,
        live_control_host="127.0.0.1",
        live_control_port=18094,
        web_host="127.0.0.1",
        web_port=18093,
    )


def _insert_opp(conn, *, key: str, ts: int, profit: float = 0.20, capital: float = 1.80) -> int:
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
            key, ARB_BUY_MERGE, "cond-live", "BTC:5m", ts,
            1, 2, ts, ts, 0, 0, 2.0, 0.40, 0.50,
            0.0, 0.0, 0.10, profit, 0.0, profit, capital,
            profit / capital, 0.40, 0.50, 1, "OK", "{}", ts,
        ),
    )
    return int(cur.lastrowid)


def _seed_candidate(settings: P3Settings, state: LiveState) -> int:
    snap = state.snapshot()
    assert snap.armed_at_ms is not None
    t0 = int(snap.armed_at_ms) + 1
    t1 = t0 + int(settings.dry_entry_confirm_ms)
    conn = connect_p3(settings.p3_db_path)
    ensure_p3_schema(conn)
    try:
        opp0 = _insert_opp(conn, key="opp-open", ts=t0, profit=0.20)
        opp1 = _insert_opp(conn, key="opp-confirm", ts=t1, profit=0.20)
        cur = conn.execute(
            """
            INSERT INTO p3_windows(
                strategy,condition_id,combo_key,opened_ts_ms,last_seen_ts_ms,
                observations,peak_net_profit_usdc,peak_net_roi,peak_quantity_shares,
                peak_opportunity_id,status
            ) VALUES(?,?,?,?,?,2,?,?,?,?, 'OPEN')
            """,
            (
                ARB_BUY_MERGE, "cond-live", "BTC:5m", t0, t1,
                0.20, 0.20 / 1.80, 2.0, opp1,
            ),
        )
        window_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO p3_window_observations(
                window_id,opportunity_id,observed_ts_ms,created_at_ms
            ) VALUES(?,?,?,?)
            """,
            [
                (window_id, opp0, t0, t0),
                (window_id, opp1, t1, t1),
            ],
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
                condition_id TEXT NOT NULL,
                side TEXT NOT NULL,
                token_id TEXT NOT NULL
            )
            """
        )
        raw.executemany(
            "INSERT INTO p26_clob_books(condition_id,side,token_id) VALUES(?,?,?)",
            [
                ("cond-live", "UP", "up-old"),
                ("cond-live", "DOWN", "down-old"),
                ("cond-live", "UP", "up-new"),
                ("cond-live", "DOWN", "down-new"),
            ],
        )
        raw.commit()
    finally:
        raw.close()
    return window_id


def _armed_state() -> LiveState:
    state = LiveState(live_feature_enabled=True, auto_execute_enabled=True)
    state.arm({
        "ok": True,
        "checked_at_ms": int(time.time() * 1000),
        "reasons": [],
    })
    assert state.snapshot().mode == MODE_LIVE_ARMED
    return state


class _BaseGateway:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.calls: list[tuple] = []

    def buy_limit_capacity(self, *, token_id, limit_price):
        self.calls.append(("capacity", token_id, limit_price))
        return {"capacity_shares": 100.0, "min_order_size": 0.01}

    def collateral_balance_usdc(self):
        return 100.0

    def conditional_balance_shares(self, token_id, *, refresh=True):
        self.calls.append(("balance", token_id, refresh))
        return 0.0

    def post_two_leg_fok(self, **kwargs):
        self.calls.append(("post", kwargs))
        assert kwargs["up_token_id"] == "up-new"
        assert kwargs["down_token_id"] == "down-new"
        return {"up_order_id": "up-order", "down_order_id": "down-order"}


class _BothGateway(_BaseGateway):
    def wait_for_leg_deltas(self, **kwargs):
        q = float(kwargs["quantity_shares"])
        return {
            "up_verified": True,
            "down_verified": True,
            "up_delta": q,
            "down_delta": q,
        }

    def merge_positions(self, **kwargs):
        self.calls.append(("merge", kwargs))
        return {"verified": True, "transaction_hash": "0xmerge"}

    def wait_for_merge(self, **kwargs):
        self.calls.append(("merge_wait", kwargs))
        return {"verified": True}


class _OneLegGateway(_BaseGateway):
    unwind_verified = True

    def wait_for_leg_deltas(self, **kwargs):
        q = float(kwargs["quantity_shares"])
        return {
            "up_verified": True,
            "down_verified": False,
            "up_delta": q,
            "down_delta": 0.0,
        }

    def unwind_fok(self, *, token_id, shares):
        self.calls.append(("unwind", token_id, shares))
        return {"order_id": "unwind-order"}

    def wait_for_unwind(self, **kwargs):
        self.calls.append(("unwind_wait", kwargs))
        return {"verified": bool(self.unwind_verified), "residual": 0.0 if self.unwind_verified else 1.0}


class _FailedUnwindGateway(_OneLegGateway):
    unwind_verified = False


def _factory(cls, holder):
    def create(settings):
        gateway = cls(settings)
        holder.append(gateway)
        return gateway

    return create


def _cycle_status(settings: P3Settings, window_id: int) -> str:
    conn = connect_p3(settings.p3_db_path)
    try:
        row = conn.execute(
            "SELECT status FROM p3_live_cycles WHERE window_id=? ORDER BY id DESC LIMIT 1",
            (window_id,),
        ).fetchone()
        assert row is not None
        return str(row["status"])
    finally:
        conn.close()


def test_two_verified_legs_are_merged_and_cycle_is_not_repeated(tmp_path) -> None:
    settings = _settings(tmp_path)
    state = _armed_state()
    window_id = _seed_candidate(settings, state)
    gateways = []
    executor = P3LiveExecutor(settings, state, gateway_factory=_factory(_BothGateway, gateways))

    result = executor.process_once()
    assert result["status"] == "MERGED_VERIFIED"
    assert _cycle_status(settings, window_id) == "MERGED_VERIFIED"
    assert state.snapshot().mode == MODE_LIVE_ARMED
    assert any(call[0] == "merge" for call in gateways[0].calls)

    second = executor.process_once()
    assert second["status"] == "NO_CONFIRMED_WINDOW"


def test_verified_single_leg_is_unwound_without_halting(tmp_path) -> None:
    settings = _settings(tmp_path)
    state = _armed_state()
    window_id = _seed_candidate(settings, state)
    gateways = []
    executor = P3LiveExecutor(settings, state, gateway_factory=_factory(_OneLegGateway, gateways))

    result = executor.process_once()
    assert result["status"] == "ONE_LEG_UNWOUND_VERIFIED"
    assert _cycle_status(settings, window_id) == "ONE_LEG_UNWOUND_VERIFIED"
    assert state.snapshot().mode == MODE_LIVE_ARMED
    assert any(call[0] == "unwind" for call in gateways[0].calls)


def test_unverified_unwind_halts_live_fail_closed(tmp_path) -> None:
    settings = _settings(tmp_path)
    state = _armed_state()
    window_id = _seed_candidate(settings, state)
    gateways = []
    executor = P3LiveExecutor(
        settings,
        state,
        gateway_factory=_factory(_FailedUnwindGateway, gateways),
    )

    result = executor.process_once()
    assert result["status"] == "HALTED_UNWIND_NOT_VERIFIED"
    assert _cycle_status(settings, window_id) == "HALTED_UNWIND_NOT_VERIFIED"
    assert state.snapshot().mode == MODE_LIVE_HALTED
    assert state.can_auto_execute() is False
