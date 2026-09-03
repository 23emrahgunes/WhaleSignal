"""Capital-path math for the capped DUAL40 recovery ladder.

The initial arm floor protects the entire 5 -> 10 -> 30 path plus an operator
buffer. After a realized one-leg loss, requiring that same initial floor again would
make the recovery ladder impossible to continue. The required balance therefore
falls only by losses that have already occurred while preserving the original
buffer for every remaining stage.
"""
from __future__ import annotations

from p3_dual40_core import Dual40Policy


def required_live_collateral(
    *,
    policy: Dual40Policy,
    level_index: int,
    initial_arm_floor_usdc: float,
) -> float:
    """Return collateral needed to safely complete the remaining capped path.

    At 40 cents with ladder 5 -> 10 -> 30 and a $35 initial arm floor:

    * level 0 requires $35: possible $2 and $4 one-leg losses, then $24 pair;
    * level 1 requires $33: possible $4 one-leg loss, then $24 pair;
    * level 2 requires $29: the $24 final pair plus the original $5 buffer.
    """
    policy.validate()
    index = int(level_index)
    if index < 0 or index >= len(policy.ladder):
        raise ValueError("DUAL40 level_index is outside the configured ladder")

    price = float(policy.price)
    ladder = tuple(float(value) for value in policy.ladder)
    final_index = len(ladder) - 1

    # Every non-final remaining stage can lose one filled leg before advancing.
    possible_remaining_single_leg_losses = sum(
        price * ladder[i] for i in range(index, final_index)
    )
    final_pair_reserve = 2.0 * price * ladder[final_index]

    configured_floor = max(0.0, float(initial_arm_floor_usdc))
    initial_path_capital = float(policy.full_ladder_capital)
    operator_buffer = max(0.0, configured_floor - initial_path_capital)

    return (
        possible_remaining_single_leg_losses
        + final_pair_reserve
        + operator_buffer
    )
