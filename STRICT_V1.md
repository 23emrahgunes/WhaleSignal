# Direction Engine STRICT V1

Strategy cohort: `INDEP_PTB_BINANCE_STRICT_5M_V1`

This cohort is intentionally separate from earlier paper history.

## Entry contract

- 5m only.
- Entry creation only at T-75..T-60 seconds.
- Independent alpha only; Polymarket prices are not alpha inputs.
- Fresh `OFFICIAL_CURRENT` reference is mandatory.
- `P(UP) <= 0.33` => DOWN candidate; `P(UP) >= 0.67` => UP candidate; otherwise no trade.
- `|z_terminal| >= 0.45`.
- Raw PTB distance must already be on the selected side.
- Counter-direction Binance correction may not exceed 0.10 remaining sigma.
- Volatility percentile < 0.92.
- Flip rate <= 0.55.
- Volatility acceleration <= 1.80.
- Direction must remain continuously stable for >=3 seconds with observation gaps <=1.5s.
- Value layer is direction locked: only the alpha-selected side is eligible.
- Ask range 5c..22c.
- Minimum forecast edge 0.30.
- Minimum value multiple 2.75x.
- P2.6 book age <=750ms.
- Executable depth inside paper fill price must be >=1.5x requested shares.
- Paper stake remains $1.00.

## LIVE safety

Normal STRICT deployment always starts XRP LIVE unarmed. If explicitly armed later,
LIVE must use the exact same strategy version and can only be reached after a strict
paper OPEN row is created. Existing $1.10 LIVE notional and 10% price-drift caps stay
in force.

Deploy with:

```bash
bash deploy_p25_strict.sh
```
