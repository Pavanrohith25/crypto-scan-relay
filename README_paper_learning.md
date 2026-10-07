# Prospective paper learning v1

This is an adaptive **spot-proxy research system**, not the R9 futures strategy,
not a validated trading model, and not connected to Telegram or orders.

The existing five-minute workflow runs `paper_learning.py`. The first successful
run establishes a real collection boundary; older signals are never backfilled.
Fresh V3.4 continuation triggers are admitted only for the frozen, explicitly
verified crypto subset. Missing required features block admission. One active
observation per symbol and a 24-hour symbol cooldown limit repeated examples.

Before entry, the append-only journal saves features, signal/recording times,
model revision/hash, and the candidate score. Entry is the next five-minute spot
candle open after recording, never the old scanner price. Source features are
hourly scanner snapshots, not tick-time features. Shorts are hypothetical spot
price paths: these are not executable spot shorts or futures performance.

Closed five-minute candles label +5% target before -6% stop with no holding limit.
Both barriers in the same candle count as a loss unless the opening price
establishes ordering. Stop gaps use the worse open; target gaps are capped at
+5%. Missing intervals are retried, never skipped. Open positions remain open.
MAE/MFE include the full exit candle and are bounds, not exact pre-exit paths.
Costs assume .12% round trip plus .01% per started eight hours; stress adds .38%.
These are sensitivity assumptions, not actual borrow, funding, or liquidation.

Separate long/short online logistic models use six fixed, clipped/scaled features.
Each newly observed resolution updates its model once. Predictions cannot change
retroactively. First 30 resolutions per side are warmup and publish no score.
Later scores are **uncalibrated model outputs**, not reliable win probabilities.
No feature selection or threshold optimization is performed. The fixed .60
selection panel is diagnostic. Report prequential Brier loss versus fixed .50,
all resolved outcomes, selected stressed returns, and unresolved/error counts.
Signals are not a capital-constrained portfolio; correlation and censoring limit
inference. Missing-bar/API errors prevent labels, so monitor coverage bias.

Artifacts: `paper_learning/journal.jsonl`, `state.json`, `report.json`, versioned
by Git. Invalid state is fatal rather than silently resetting learning. The workflow
commits state and journal together. Requests are bounded to 12 per run, oldest
checked first, and 1000 bars per request; outages catch up incrementally.

**Promotion is always BLOCKED in v1.** A future version needs executable futures
feed parity, a frozen challenger evaluated on untouched later data, calibration,
positive stressed expectancy, uncertainty/sample-size checks, an open-position
risk panel, and explicit review before connecting alerts. No R9 reserved dates
or failed development results are repurposed as fresh evidence.

Run tests: `python -m unittest discover -s tests -p 'test_*.py'`.
