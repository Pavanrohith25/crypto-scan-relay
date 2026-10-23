# Futures entry model research v2

This branch builds the agreed selective continuation model. It does not enable
Telegram signals or change the running scanner. No 60% result is claimed until
the completed report passes the newest chronological holdout.

## Research contract

- Archive sample: 200 USD-M perpetual USDT contracts with archives before the
  first validation period; BTC/ETH included. Deterministic seed independent of
  current liquidity or future returns. Includes retained delisted archives.
- At least 150 usable histories; 365 calendar days requested. Newer contracts
  use their available history after warmup. Gaps invalidate affected windows.
- Checksum-verified 5m data; complete contiguous 15m, 1h and 4h derived candles.
- Top 30 active liquid contracts selected using each historical timestamp's
  trailing 24h volume and activity, with a $10m daily quote-volume floor.
- Existing 1h/4h feature logic plus 15m retest/confirmation eligibility,
  directional regime gate and anti-chase restrictions. This is a research entry
  gate, not the completed live two-scan retest state machine.
- Decision uses only completed bars; simulated fill is next 5m open. Target +5%
  before -1.5% within 48h. Additional -1%, -2.5%, -3% clean-path labels retained
  for diagnostics, not holdout optimization. Stop gap fills use worse open;
  same-bar ambiguity counts as loss.
- Three models: histogram gradient boosting, LightGBM, scaled logistic. Sigmoid
  calibration on a purged training tail. 72h gaps. No random split or default
  random early-stopping validation.
- Select model and probability threshold only on validation, maximizing equal
  notional net realized return among choices with >=60% precision and >=100
  alerts. Only the winning frozen choice is evaluated on the newest test.
- Evaluation models actual repeated-call suppression: 12h coin cooldown and
  one open position per direction, released at simulated exit time.
- Test gate: >=100 alerts, >=60% clean-win precision, positive average net
  return, profit factor >1. No qualifying validation model means **NO TRADE** and
  no test evaluation. A passing test qualifies for forward paper/shadow testing,
  not automatic live promotion.
- Cost assumptions: 0.12% round-trip fees/slippage plus 0.06% funding allowance;
  also report mean return at 0.50% total cost. These are explicit assumptions,
  not historical reconstructed fills/funding.

## Run

The research branch push starts `Futures ML Research v2 - 60pct holdout gate`.
The workflow can also be run manually from the selected branch. It preserves
coverage, the labeled dataset, calibrated model if one qualifies on validation,
the frozen holdout trade ledger and report as a 30-day GitHub artifact.

Local commands after installing the dependencies from the workflow:

```bash
python -m unittest discover -s research_tests -v
python historical_futures_backfill_v2.py
python historical_futures_dataset_v2.py
python train_swing_model_v2.py
```

The dataset is memory mapped by contract. Do not rerun/tune on the same newest
holdout until a desired result appears; that invalidates its untouched status.
For subsequent experiments use earlier walk-forward development folds and
reserve newly accrued dates for the next final evaluation.

## Evidence and limits

The report contains precision and Wilson intervals, net expected return, profit
factor, holding times, first-15m/60m adverse movement, month/regime/direction
slices of the SAME selected holdout trades, and equal-notional closed-trade
drawdown. Closed-trade drawdown is not account or mark-to-market drawdown.
Correlated trades make independent-trade confidence intervals optimistic.

Historical OI, funding features, liquidations, on-chain features and order books
are not available in these archives and are not invented. Archive membership
does not guarantee complete recovery of every delisted contract. This is a
price/volume/regime baseline requiring forward paper trading and live feature
parity checks before any entry-engine replacement. A 60% backtest is an observed
sample result, not a guaranteed future success rate.
