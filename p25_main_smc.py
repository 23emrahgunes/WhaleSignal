"""SMC Selective V3 entrypoint.

Uses the normal P2.5 runtime, but first installs the five-minute-only runtime
hardening, then enables structural SMC confirmation, and finally replaces historical
SQLite-backed state generation with a zero-blocking in-memory operational snapshot.
The guarded ALL-5m LIVE controller remains DRY-first and starts unarmed after every
deploy.
"""
from __future__ import annotations

import p25_main
from p25_smc_patch import SMC_STRATEGY, enable_smc_v3
from p25_smc_runtime import install_smc_v3_runtime_hardening
from p25_smc_state import install_zero_blocking_operational_state


def main() -> None:
    # Order matters: enable_smc_v3 captures FeatureEngine.update. Install the
    # bounded-history wrapper first so base 5m features and SMC both use only the
    # history they require.
    install_smc_v3_runtime_hardening()
    enable_smc_v3()

    # enable_smc_v3 installs its compatibility state patch first. Override the core
    # snapshot afterwards so /api/state never performs historical SQLite scans.
    install_zero_blocking_operational_state()

    # Controller selection is exact-strategy locked. V3 is a separate paper cohort
    # but uses the same guarded BTC/ETH/SOL/XRP 5m executor.
    p25_main._DIRECTIONAL_ALL5M_STRATEGY = SMC_STRATEGY
    p25_main.main()


if __name__ == "__main__":
    main()
