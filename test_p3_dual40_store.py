import pytest

from p3_dual40_store import (
    active_cycle,
    connect_dual40,
    create_cycle,
    ladder_state,
    market_decisions,
    reset_scope,
    set_ladder_state,
    update_cycle,
    upsert_market_decision,
)


def test_dual40_state_persists_and_hard_stop_survives_reconnect(tmp_path):
    path = str(tmp_path / "p3.sqlite")
    conn = connect_dual40(path)
    set_ladder_state(
        conn,
        scope="LIVE",
        asset="BTC",
        level_index=2,
        loss_pool_usdc=18.0,
        hard_stopped=True,
        hard_stop_reason="HARD_STOP_MAX_30_CANNOT_RECOVER",
    )
    conn.close()

    reopened = connect_dual40(path)
    state = ladder_state(reopened, "LIVE", "BTC")
    assert state["level_index"] == 2
    assert state["loss_pool_usdc"] == 18.0
    assert state["hard_stopped"] == 1
    assert state["hard_stop_reason"] == "HARD_STOP_MAX_30_CANNOT_RECOVER"
    reopened.close()


def test_active_cycle_blocks_reset_until_terminal(tmp_path):
    path = str(tmp_path / "p3.sqlite")
    conn = connect_dual40(path)
    cycle_id = create_cycle(
        conn,
        scope="PAPER",
        asset="BTC",
        session_id=None,
        condition_id="0xcondition",
        combo_key="BTC:5m",
        market_end_ts_ms=1_900_000_000_000,
        level_index=0,
        target_shares=5.0,
        maker_price=0.40,
        status="PAPER_RESTING",
        gate={"eligible": True},
        up_token_id="up",
        down_token_id="down",
        loss_pool_before_usdc=0.0,
    )
    assert active_cycle(conn)["id"] == cycle_id

    with pytest.raises(RuntimeError, match="active"):
        reset_scope(conn, scope="PAPER")

    update_cycle(conn, cycle_id, status="NO_FILL", realized_pnl_usdc=0.0)
    assert active_cycle(conn) is None
    reset_scope(conn, scope="PAPER")
    state = ladder_state(conn, "PAPER", "BTC")
    assert state["level_index"] == 0
    assert state["loss_pool_usdc"] == 0.0
    assert state["hard_stopped"] == 0
    conn.close()


def test_clean_paper_reset_discards_only_paper_history(tmp_path):
    path = str(tmp_path / "p3.sqlite")
    conn = connect_dual40(path)
    for scope in ("PAPER", "LIVE"):
        cycle_id = create_cycle(
            conn,
            scope=scope,
            asset="BTC",
            session_id=None,
            condition_id=f"{scope.lower()}-condition",
            combo_key="BTC:5m",
            market_end_ts_ms=1_900_000_000_000,
            level_index=1,
            target_shares=10.0,
            maker_price=0.40,
            status=f"{scope}_RESTING",
            gate={"eligible": True},
            up_token_id=f"{scope}-up",
            down_token_id=f"{scope}-down",
            loss_pool_before_usdc=2.0,
        )
        set_ladder_state(
            conn,
            scope=scope,
            asset="BTC",
            level_index=1,
            loss_pool_usdc=2.0,
            hard_stopped=False,
            hard_stop_reason=None,
            last_cycle_id=cycle_id,
        )
        upsert_market_decision(
            conn,
            scope=scope,
            asset="BTC",
            combo_key="BTC:5m",
            condition_id=f"{scope.lower()}-condition",
            market_start_ts_ms=1_899_999_700_000,
            market_end_ts_ms=1_900_000_000_000,
            decision="OPENED",
            opened_cycle_id=cycle_id,
        )

    reset_scope(
        conn,
        scope="PAPER",
        clear_cycles=True,
        clear_decisions=True,
        discard_active_paper=True,
    )

    assert active_cycle(conn, scope="PAPER") is None
    assert market_decisions(conn, scope="PAPER") == []
    paper = ladder_state(conn, "PAPER", "BTC")
    assert paper["level_index"] == 0
    assert paper["loss_pool_usdc"] == 0.0
    assert paper["last_cycle_id"] is None

    assert active_cycle(conn, scope="LIVE") is not None
    assert len(market_decisions(conn, scope="LIVE")) == 1
    live = ladder_state(conn, "LIVE", "BTC")
    assert live["level_index"] == 1
    assert live["loss_pool_usdc"] == 2.0
    conn.close()


def test_active_live_cycle_can_never_be_discarded_by_reset(tmp_path):
    path = str(tmp_path / "p3.sqlite")
    conn = connect_dual40(path)
    create_cycle(
        conn,
        scope="LIVE",
        asset="BTC",
        session_id=None,
        condition_id="live-condition",
        combo_key="BTC:5m",
        market_end_ts_ms=1_900_000_000_000,
        level_index=0,
        target_shares=5.0,
        maker_price=0.40,
        status="LIVE_RESTING",
        gate={"eligible": True},
        up_token_id="live-up",
        down_token_id="live-down",
        loss_pool_before_usdc=0.0,
    )
    with pytest.raises(ValueError, match="only be discarded for PAPER"):
        reset_scope(
            conn,
            scope="LIVE",
            clear_cycles=True,
            discard_active_paper=True,
        )
    assert active_cycle(conn, scope="LIVE") is not None
    conn.close()
