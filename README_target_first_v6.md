# Target-before-stop research

The user removed timeframe and holding-period requirements on September 30.
Objective: at least 60% of entered trades reach a 5% underlying-price target
before their predefined stop, with positive net returns and controlled downside.

Six fixed development comparisons: 1h/4h confirmation crossed with trend support
pullback, sweep/reclaim, and volume-confirmed trend breakout. Reuse v4/v5 support
rules. Breakouts cross the prior 18 closed 4h bar extreme in the 20/50 SMA trend,
with signal volume at least 1.5 times its preceding 20-bar mean. Stops sit beyond
the level/signal extreme plus 0.25 4h ATR. Retain risk of 0.25% to 3.333%, $10m
24h turnover, BTC adverse 4% veto, next-open entries, one slot per direction,
no concurrent same-coin positions, and 24h per-coin cooldown.

No forced time exit. Observe until first barrier or available development data
ends. Data gaps censor outcomes. Unresolved positions retain their portfolio
slots through the development cutoff. Wins divided by ALL entries is the main
conservative observed target fraction. Report resolved-only rate separately,
bounds with unresolved outcomes, duration, and marked-to-market open positions.

Estimated costs: 0.12% fees/slippage plus 0.01% per started 8h holding interval;
stress adds 0.38%. Funding is not reconstructed. No leverage/liquidation model.
Simultaneous hourly barriers count as losses. MAE includes full exit candle.

Screen requires 100 trades, observed target fraction >=60%, unresolved <=5%,
positive resolved and all-position marked-to-market means, and 20 trades with
positive resolved means in each entry-date third. These are development screens,
not statistical proof or live validation. Report ALL six configurations. The
previously used development data cannot be called unseen. Original final holdout
remains excluded by the unchanged v4 cutoff. No production changes.
