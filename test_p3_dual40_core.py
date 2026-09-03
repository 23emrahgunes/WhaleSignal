from p3_dual40_core import (
    Dual40Policy,
    MidPoint,
    evaluate_balanced_regime,
    matched_pair_pnl,
    next_ladder_state,
    realized_cycle_pnl,
)


def _points(values, *, step_ms=2_000):
    return [MidPoint(1_000_000 + i * step_ms, value) for i, value in enumerate(values)]


def test_balanced_two_way_market_passes():
    policy = Dual40Policy(lookback_sec=20.0)
    result = evaluate_balanced_regime(
        policy=policy,
        up_points=_points([0.50, 0.51, 0.49, 0.51, 0.50, 0.49, 0.51, 0.50, 0.49, 0.50, 0.50]),
        current_down_mid=0.50,
        current_up_spread=0.04,
        current_down_spread=0.04,
        current_up_ask=0.52,
        current_down_ask=0.52,
        market_age_sec=45.0,
        tte_sec=220.0,
    )
    assert result.eligible is True
    assert result.reason == "BALANCED_STABLE_TWO_WAY"
    assert result.score > 0.25


def test_one_way_market_is_rejected():
    policy = Dual40Policy(lookback_sec=20.0)
    result = evaluate_balanced_regime(
        policy=policy,
        up_points=_points([0.45, 0.46, 0.47, 0.48, 0.49, 0.50, 0.51, 0.52, 0.53, 0.54, 0.55]),
        current_down_mid=0.45,
        current_up_spread=0.04,
        current_down_spread=0.04,
        current_up_ask=0.57,
        current_down_ask=0.47,
        market_age_sec=45.0,
        tte_sec=220.0,
    )
    assert result.eligible is False
    assert result.reason in {"MID_RANGE_TOO_WIDE", "NET_DRIFT_TOO_HIGH", "ONE_WAY_SLOPE", "ONE_WAY_SEQUENCE"}


def test_post_only_cross_is_rejected():
    policy = Dual40Policy(lookback_sec=20.0)
    result = evaluate_balanced_regime(
        policy=policy,
        up_points=_points([0.50] * 11),
        current_down_mid=0.50,
        current_up_spread=0.04,
        current_down_spread=0.04,
        current_up_ask=0.40,
        current_down_ask=0.52,
        market_age_sec=45.0,
        tte_sec=220.0,
    )
    assert result.eligible is False
    assert result.reason == "POST_ONLY_WOULD_CROSS"


def test_exact_recovery_ladder_is_5_10_30_then_hard_stop():
    policy = Dual40Policy()

    # First 5-share single leg loses: -$2 -> 10 shares needed to make +$2.
    step2 = next_ladder_state(policy=policy, loss_pool_before=0.0, cycle_pnl=-2.0)
    assert step2.target_shares == 10.0
    assert step2.loss_pool == 2.0
    assert step2.hard_stopped is False

    # Second 10-share single leg loses: additional -$4, total $6 -> 30 shares.
    step3 = next_ladder_state(policy=policy, loss_pool_before=step2.loss_pool, cycle_pnl=-4.0)
    assert step3.target_shares == 30.0
    assert step3.loss_pool == 6.0
    assert step3.hard_stopped is False

    # Fully matched 30-share pair makes $6 and clears the pool.
    recovered = next_ladder_state(
        policy=policy,
        loss_pool_before=step3.loss_pool,
        cycle_pnl=matched_pair_pnl(price=0.40, matched_shares=30),
    )
    assert recovered.target_shares == 5.0
    assert recovered.loss_pool == 0.0
    assert recovered.hard_stopped is False

    # If the capped 30-share round is another single-leg loss, no 90-share rescue.
    stopped = next_ladder_state(policy=policy, loss_pool_before=6.0, cycle_pnl=-12.0)
    assert stopped.hard_stopped is True
    assert stopped.target_shares == 30.0
    assert stopped.loss_pool == 18.0


def test_partial_fill_pnl_uses_actual_fills_and_official_result():
    # 30 UP + 18 DOWN costs $19.20. If UP wins, payout is $30 -> +$10.80.
    assert realized_cycle_pnl(
        price=0.40,
        up_filled=30,
        down_filled=18,
        official_result="UP",
    ) == 10.8

    # Same inventory if DOWN wins pays $18 -> -$1.20.
    assert round(
        realized_cycle_pnl(
            price=0.40,
            up_filled=30,
            down_filled=18,
            official_result="DOWN",
        ),
        10,
    ) == -1.2
