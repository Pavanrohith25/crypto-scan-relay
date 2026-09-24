# Scanner v3.2 Shadow Build

This build is deliberately **shadow-only**.

It does not replace v3.1, does not send Telegram trade alerts, and does not change live entry decisions. It records what a next-generation engine would have done so that the rules can be judged on real outcomes before promotion.

## What changed

- Removed the concept of `90+ = instant entry`.
- 90+ is only `high priority`.
- Added three regimes:
  - `TREND_CONTINUATION`
  - `COUNTERTREND`
  - `COUNTERTREND_DEEP`
- Deep countertrend setups are blocked from entry.
- Added 4h/24h context so short-term squeeze data cannot overpower a strongly opposing larger trend.
- Added persistence that can survive a modest score dip (`82 -> 76`) while underlying spot/OI structure remains healthy.
- Added a real price follow-through trigger before confirmation.
- Added anti-chase logic for already-extended 1h moves.
- Added low-OI liquidity requirements.
- Exact duplicate evidence does not count as another independent confirmation.
- Added false-start detection.
- Added a shadow position manager:
  - `HOLD_STRONG`
  - `HOLD`
  - `HOLD_PROTECT`
  - `REDUCE`
  - `EXIT_THESIS_BROKEN`
- The position manager emphasizes spot + 4h/24h context rather than tiny short-term price noise.

## Historical sanity checks included

The unit tests encode three lessons from the archived scans:

1. LSK: 82 -> 76 does not kill the setup; it waits for actual price follow-through and then can confirm.
2. ONE: the historical 90 setup is blocked because the 24h/4h context was deeply countertrend.
3. STEEM: the historical low-OI / thin-spot setup is not armed.

## Rollout

Recommended rollout:

1. Keep v3.1 live and unchanged.
2. Run v3.2 in shadow for at least 30-50 meaningful opportunities.
3. Measure:
   - +3% before -1%
   - +4% before -1.5%
   - +4% before -2%
   - MFE / MAE
   - time to +3% / +4%
   - false-start rate
   - missed continuation rate
4. Promote only if shadow results are materially better and not merely based on a few trades.

This is not self-learning or autonomous reweighting. Rules remain fixed until deliberately changed.
