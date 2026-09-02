"""SMC Selective V3 entrypoint.

Uses the normal P2.5 runtime, but first installs the five-minute-only runtime
hardening and then enables structural SMC confirmation.  The guarded ALL-5m LIVE
controller remains DRY-first and starts unarmed after every deploy.
"""
from __future__ import annotations

import p25_main
from p25_smc_patch import SMC_STRATEGY, enable_smc_v3
from p25_smc_runtime import install_smc_v3_runtime_hardening


def main() -> None:
    # Order matters: enable_smc_v3 captures FeatureEngine.update.  Install the
    # bounded-history wrapper first so both the base 5m features and SMC operate on
    # only the history they actually require.
    install_smc_v3_runtime_hardening()
    enable_smc_v3()

    # Controller selection is exact-strategy locked.  V3 is a separate paper cohort
    # but uses the same guarded BTC/ETH/SOL/XRP 5m executor.
    p25_main._DIRECTIONAL_ALL5M_STRATEGY = SMC_STRATEGY
    p25_main.main()


if __name__ == "__main__":
    main()
