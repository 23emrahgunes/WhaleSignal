import pytest

from p26_execution import OrderBookSnapshot, simulate_buy
from p26_fee import FeeSchedule, FeeScheduleStore, FeeScheduleUnavailable


def _book():
    return OrderBookSnapshot.from_levels(
        token_id="up-token", ts_ms=1000,
        bids=[(0.49, 10)], asks=[(0.50, 2), (0.60, 10)], sequence=1,
    )


def test_dynamic_fee_is_level_price_dependent_and_audited():
    schedule = FeeSchedule(
        condition_id="c", token_id="up-token", enabled=True,
        rate=0.07, exponent=1.0, taker_only=True,
        source="CLOB", source_ts_ms=900,
    )
    fill = simulate_buy(
        _book(), stake_usdc=2.5,
        fee_schedule=schedule, require_fee_schedule=True,
    )
    assert fill.complete
    assert fill.levels_consumed == 2
    assert fill.fee_usdc > 0
    assert fill.all_in_cost_per_share > fill.orderbook_vwap
    assert fill.fee_source == "CLOB"
    assert fill.fee_formula_version == schedule.formula_version


def test_missing_or_wrong_fee_schedule_fails_closed():
    with pytest.raises(FeeScheduleUnavailable):
        simulate_buy(_book(), stake_usdc=2.5, require_fee_schedule=True)
    wrong = FeeSchedule("c", "down-token", False, 0.0, 1.0, True, "CLOB", 1)
    with pytest.raises(ValueError, match="token"):
        simulate_buy(_book(), stake_usdc=2.5, fee_schedule=wrong)


def test_market_info_store_maps_up_down_and_fee_details(tmp_path):
    store = FeeScheduleStore(str(tmp_path / "p26.sqlite"))
    try:
        schedules = store.upsert_market_info(
            condition_id="c1", combo_key="BTC:5m", market_end_ts_ms=2000,
            payload={
                "fd": {"r": 0.07, "e": 1, "to": True},
                "t": [
                    {"t": "up", "o": "Up"},
                    {"t": "down", "o": "Down"},
                ],
            },
            source_ts_ms=1000,
        )
        assert set(schedules) == {"UP", "DOWN"}
        assert store.get("c1", "up").enabled
        assert store.get("c1", "down").rate == pytest.approx(0.07)
    finally:
        store.close()
