# Direction Engine — Directional Edge V2

Strategy cohort: `INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2`

This cohort fixes the design conflict in STRICT V1 where a strong directional alpha
was still forced through a 5-22c deep-value price band. V2 keeps the strict alpha and
risk gates, but prices the selected direction by probability edge rather than by an
artificially cheap contract requirement.

## Alpha / timing gates kept from STRICT

- 5m only.
- Entry creation only at T-75..T-60 seconds.
- Independent PTB + Binance alpha only; Polymarket price is not an alpha input.
- Fresh `OFFICIAL_CURRENT` reference is mandatory.
- `P(UP) <= 0.33` => DOWN candidate; `P(UP) >= 0.67` => UP candidate; otherwise no trade.
- `|z_terminal| >= 0.45`.
- Raw PTB distance must already be on the selected side.
- Counter-direction Binance correction may not exceed 0.10 remaining sigma.
- Volatility percentile < 0.92.
- Flip rate <= 0.68.
- Volatility acceleration <= 1.80.
- Direction must remain continuously stable for >=3 seconds with observation gaps <=1.5s.
- Value layer is direction locked: only the alpha-selected side is eligible.

## Directional value gates

- Absolute selected-side ask range: 5c..75c.
- Minimum probability edge after paper slippage: 8 percentage points.
- Minimum value multiple: 1.12x.
- Effective max ask is dynamic:
  `min(0.75, P(selected) - 0.08 - paper_slippage)`.
- P2.6 book age <=750ms.
- Executable depth inside paper fill price must be >=1.5x requested shares.
- Paper stake remains $1.00.

Examples:

- `P(DOWN)=0.93`, DOWN ask `0.68`, fill about `0.685` => edge about `24.5pt`: eligible if all other gates pass.
- `P(UP)=0.70`, UP ask `0.61`, fill about `0.615` => edge about `8.5pt`: eligible if all other gates pass.
- `P(UP)=0.67`, UP ask `0.60`, fill about `0.605` => edge about `6.5pt`: rejected.

## LIVE safety

Normal deployment always starts XRP LIVE unarmed. The existing LIVE absolute price
cap remains 25.5c, intentionally narrower than the paper V2 range. Paper V2 must be
validated before widening any LIVE envelope.

Deploy with:

```bash
bash deploy_p25_strict.sh
```
