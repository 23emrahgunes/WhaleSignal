import pytest

from p3_dual40_store import (
    active_cycle,
    connect_dual40,
    create_cycle,
    ladder_state,
    reset_scope,
    set_ladder_state,
    update_cycle,
)


def test_dual40_state_persists_and_hard_stop_survives_reconnect(tmp_path):
    path = str(tmp_path / "p3.sqlite")
    conn = connect_dual40(path)
    set_ladder_state(
        conn,
        scope="LIVE",
        level_index=2,
        loss_pool_usdc=18.0,
        hard_stopped=True,
        hard_stop_reason="HARD_STOP_MAX_30_CANNOT_RECOVER",
    )
    conn.close()

    reopened = connect_dual40(path)
    state = ladder_state(reopened, "LIVE")
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
    state = ladder_state(conn, "PAPER")
    assert state["level_index"] == 0
    assert state["loss_pool_usdc"] == 0.0
    assert state["hard_stopped"] == 0
    conn.close()
