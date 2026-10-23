# Matched V3.4/R6 comparison and loss audit

Authorized October 1, 2026. Freeze all rules before observing this comparison.
No optimization or live promotion. Reuse the same 191-contract development
archive and unchanged final-holdout exclusion.

Compare the existing feature_core V3.4 continuation transition with R6's 4h
volume-confirmed breakout. V3.4 is a candle-based futures reconstruction, not
an exact spot-discovery or Telegram replay. Its rolling ticker/top-N discovery,
daily anti-chase and historical Telegram filters are not reconstructed. R6's
original structural-risk signal gate remains; common execution stops replace
its original stop. Native hourly vs four-hour signal timing is retained.

Both arms: hourly execution candles, next-open fill, 5% target, fixed 3% stop
and fixed 6% stop as two separate predeclared matched comparisons. No forced
holding deadline. Common $10m trailing turnover floor, adverse BTC 4% daily
veto, 24h coin cooldown, one position per direction, no concurrent same coin.
Resolve simultaneous signals by higher trailing turnover then symbol, not the
strategy score or future outcome. No target/stop peeking in portfolio selection.

Same-bar ambiguity loses, stop gap fills use worse open. Gaps/data-end censor
unresolved trades; they remain in denominator and occupy slots through cutoff.
Costs: .12% fees/slippage plus .01% per started 8h; stress adds .38%. No actual
historical funding, leverage, or liquidation simulation. Open gap MTM is stale.

For stopped trades only, examine the next 96 complete hourly bars, excluding the
stop candle. Count original 5% target recovery, no target within that observation
window, and insufficient follow-up separately. Further movement to -2R is an
overlapping diagnostic. Recovery never converts a stopped trade into a winner.

Describe direction, BTC alignment, and entry extension beyond two 4h ATRs from
the hourly 20-close mean. These are subgroup observations, not causal conclusions
or approved filters. Three entry-date periods are descriptive, not independent
validation. Main report plus four trade ledgers are preserved as run artifacts.
