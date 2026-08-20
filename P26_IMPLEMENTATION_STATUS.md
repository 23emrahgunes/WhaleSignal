# P2.6 Implementation Status

P2.6 is an isolated research challenger. P2.5 SHADOW/PAPER remains the rollback
baseline and its modules/tables are not rewritten by P2.6.

| Phase | Scope | Status |
|---|---|---|
| P2.6.0 | Baseline freeze + operational hardening | LOCAL_PASS / CI_PENDING |
| P2.6.1 | Oracle persistence + canonical dataset | LOCAL_PASS / CI_PENDING |
| P2.6.2 | External-only frozen fair-value model | LOCAL_PASS / CI_PENDING |
| P2.6.3 | Purged nested OOS + latency/alpha replay | LOCAL_PASS / CI_PENDING |
| P2.6.4 | OOS calibration + Wilson uncertainty | LOCAL_PASS / CI_PENDING |
| P2.6.5 | Depth/latency/alpha-aware Paper V2 | LOCAL_PASS / CI_PENDING |
| P2.6.6 | Promotion / rejection evaluation | LOCAL_PASS / CI_PENDING |

Safety invariants: no private key, no signing, no order submission, no execution.
