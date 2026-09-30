# User-directed support swing research

On September 30 the user clarified the objective: support-based swing entries
with favorable upside/downside, a +5% target, and room to develop over three to
four days. The 1.5% fixed stop and 48h horizon were earlier research constraints,
not immutable user requirements. This experiment changes those constraints
explicitly while retaining a 60% acceptance target and net profitability.

Two fixed benchmarks are compared: trend pullback to support/resistance, and a
level sweep followed by a reclaim/rejection. Levels are computed from closed 4h
history, entry confirmation from the completed 1h candle, and execution at the
next hourly open. Green/red candle counts alone never trigger a trade.

Stops are placed beyond the reference level and rejection extreme, with a 0.25
4h ATR buffer. Reject trades with less than 1.5 gross reward/risk for the 5%
target, excessive entry-gap risk, insufficient historic room to resistance or
support, low trailing dollar volume, or strong adverse BTC daily movement.
These are predeclared research rules, not an assertion they are profitable.

Both benchmarks are evaluated on development data ONLY, in aggregate and three
chronological periods. A candidate must have >=100 executed simulations, >=60%
target-before-stop wins, positive net mean and profit factor >1, plus >=20 trades
and positive mean in each period, before a final-test design is considered.
The original unseen final period remains excluded. No live promotion occurs.

Costs assume .12% trading fees/slippage and .12% four-day funding reserve; the
report also stresses .50% total cost. No reconstructed funding claim is made.
One position per direction and a 24h coin cooldown constrain repeated/correlated
exposure. Returns and drawdown in R normalize by each trade's initial stop risk;
closed-trade drawdown is not account mark-to-market drawdown.

Hourly candles make simultaneous target/stop hits ambiguous, so those count as
losses. Any promising strategy still needs finer execution data, actual funding,
and forward paper validation. Waiting four days never guarantees a profit.

The workflow reuses the frozen contract universe but fetches only development
hourly archives, with checksums. It preserves hourly data and concise reports as
separate artifacts so further research can reuse them without another backfill.
