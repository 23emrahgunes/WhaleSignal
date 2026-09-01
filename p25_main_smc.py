"""SMC Selective V3 entrypoint.

Uses the normal P2.5 runtime but enables the structural SMC confirmation patch and
selects the guarded ALL-5m LIVE controller for the V3 paper cohort.
"""
from __future__ import annotations

import p25_main
from p25_smc_patch import SMC_STRATEGY, enable_smc_v3


def main() -> None:
    enable_smc_v3()
    # p25_main controller selection is intentionally exact-strategy locked.  V3 is a
    # separate paper cohort, but uses the same guarded BTC/ETH/SOL/XRP 5m executor.
    p25_main._DIRECTIONAL_ALL5M_STRATEGY = SMC_STRATEGY
    p25_main.main()


if __name__ == "__main__":
    main()
