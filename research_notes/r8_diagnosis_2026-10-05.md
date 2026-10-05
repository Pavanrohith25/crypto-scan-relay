# R8 trade audit — 2026-10-05

Source: workflow 37143726881, artifact 11280903910. This is a post-test
diagnosis, not a new backtest or evidence of improved performance.

## Findings

- The archive universe was not crypto-only. Of ten test entries, QQQUSDT
  (ETF-linked), MUUSDT (Micron), and MSFTUSDT (Microsoft) were traditional
  asset contracts. QQQ and MU stopped out; MSFT remained unresolved.
  The development ledger also contains INTCUSDT, MSFTUSDT, and XAUUSDT.
  Therefore these aggregate results cannot be described as crypto-only.
- The nine resolved test positions produced five +5% targets and four -6%
  stops. Their summed gross returns were +1.00 percentage point, assumed
  fees/slippage 1.08 points, and assumed funding 1.08 points. Net sum was
  -1.16 points, or -0.1289% per resolved position. These sums are equal-notional
  trade-return arithmetic, not compounded account performance. Funding was
  modeled, not measured; even the fees/slippage allowance alone exceeds
  the gross edge in this sample.
- Average holding time was 63.2 hours for wins and 135 hours for losses.
  The MSFT position stayed unresolved for 488 hours (20 days 8 hours), blocking
  the only portfolio slot for that interval. It accounted for 36.3% of the
  1,344 total position-hours. Its terminal marked return was -3.4366% gross,
  -4.1666% after modeled costs. It is neither a realized stop nor a win.
- None of the test losses were caused by a candle hitting both barriers.
  All four resolved losses filled at the specified 6% stop. Thus ambiguity
  and gap-through-stop handling did not explain this test's losses.
- Ten entries (July 17–September 29, 2026 UTC evaluation window) are too few
  for a reliable >50% claim. Two CHZ wins and two ONDO entries also mean the
  sample includes repeated exposure to the same assets. A decline from
  development performance is descriptive, not proof of its causal mechanism.

## Required correction before another performance claim

1. Classify the entire frozen universe using documented underlying-asset
   identity, independent of P&L. Admit verified crypto contracts only; block
   unknown classifications. Do not infer identities solely from ticker text
   or use today's listings to erase historical delisted contracts.
2. Replay the full corrected candidate stream and portfolio. Deleting QQQ,
   MU, and MSFT rows from the selected ledger does NOT give a corrected win
   rate: freeing occupied slots changes subsequent selected trades.
3. Preserve R8's failed result. A replay of this now-seen test period is a
   diagnostic sensitivity check, not fresh validation.
4. Confirm CoinGlass/archive agreement on closed-bar timestamps, OHLC,
   dollar-volume fields and signal decisions before live use. The successful
   BTC 4h entitlement probe establishes access only, not strategy parity.
5. Freeze any resulting rules and select genuinely unused dates before
   evaluating them. No stop widening, time-limit tuning or position-cap
   changes are justified as validated improvements by this ten-trade ledger.

Telegram remains paused. No replacement win rate has been established.

## Official underlying-asset references

- QQQ: https://academy.binance.com/ur-PK/articles/etf-contracts-you-can-trade-on-binance-futures
- MU: https://www.binance.com/en/support/announcement/detail/80549fadb3e447d1a859b8cedd0ccd69
- MSFT: https://www.binance.com/es/support/announcement/detail/16fe15060947481ab240fb398230a3ee
