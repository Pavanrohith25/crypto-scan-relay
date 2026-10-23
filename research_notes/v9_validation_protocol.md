# V3.4 crypto-only replay and future validation protocol

Frozen on 2026-10-05 before the corrected replay results are inspected.

## Diagnostic replay

Use the R7 V3.4 candle proxy unchanged: 1h signal, 4h trend context,
transition-only continuation triggers, $10M trailing quote-volume minimum,
BTC 24h short veto above +4%, next hourly open, +5% target, -6% stop,
one open short, 24h per-symbol entry cooldown, no deadline. Same-bar
ambiguity loses and unresolved positions occupy the slot until data end.
Costs remain .12% round trip plus .01% per started eight hours, with an
additional .38% stressed cost panel. These are assumptions, not actual fees
or signed funding settlements. No leverage or liquidation simulation.

The identity registry admits 130 documented crypto contracts from the
original frozen 191. Three identities are confirmed non-crypto and 58 remain
unverified and blocked. Unverified does not mean non-crypto. This is an
incomplete, verified subset, with possible coverage bias. No filters depend
on individual trade P&L. Delisted/unsupported tokens with evidence stay
eligible while their historical futures candles exist. Asset identity is
reviewed retrospectively, not inferred from current trading status.

Recreate the original mixed-book R7 result (247 trades/140 wins; short
subgroup 140 trades/89 wins) as a mandatory parity check. Compare original
short-only and verified-crypto short-only portfolios by replaying all entry
candidates, not by deleting completed trade rows.

The development window and July 17–September 29 R8 test window have already
been inspected. Both R9 panels are diagnostic. Do not publish either as
fresh validation. Changing any classification or trading rule after looking
at outcomes creates a new version and requires a new evaluation boundary.

## Fresh evaluation

No sufficiently large unused interval was identified inside the original
dataset. Reserve 2026-10-06 00:00 UTC through 2027-01-04 00:00 UTC (90 days)
for a prospective test of this exact committed strategy and identity list.
Warmup candles before the start may build indicators but cannot create
positions or count as trades. Do not retune within the reserved period.
Report open positions separately at the end; do not force an exit and call
it a stop/target. The sample may still be too small after 90 days.

Predeclared evidence screen: at least 50 resolved trades; wins/all entries
above 50%; descriptive Wilson lower bound above 50%; positive resolved
stressed mean and all-position marked mean after modeled costs. Passing
this screen does not prove future profitability or authorize automatic
live promotion. Correlated trades weaken the binomial interval assumption.

This document reserves dates; no scheduled future collection job or Telegram
activation is installed by the diagnostic workflow. Before live use, confirm
an authorized 1h futures feed, archive/provider candle and signal parity,
and executable entry timing. The current CoinGlass 4h-only entitlement does
not satisfy the unchanged V3.4 1h requirement.
