---
id: LRN-000001
type: learning
title: Profit factor, not Sharpe, is the binding constraint for an Indian VDA desk
created: 2026-08-24
source: src/supercortex/costs/model.py
tags: [tax, india, strategy-selection, cost-model]
---

# LRN-000001 — Profit factor is the binding constraint

Reproduce: `uv run supercortex costs breakeven` and `uv run supercortex costs regimes`.

## The finding

Under s.115BBH every **winning** disposal is taxed at 30% plus cess, and every **losing**
one gives nothing back — no set-off against other VDA gains, no carry-forward. That makes
the survival threshold a function of the ratio of gross wins to gross losses, and the
relationship is closed-form:

```
PF_min = 1 / (1 - r)        r = 0.30 x 1.04 = 0.312
PF_min = 1.4535
```

**A strategy whose gross wins are less than ~1.45x its gross losses pays more tax than it
earns** — before one rupee of fees, spread, slippage or TDS.

Sharpe ratio does not reveal this. Two strategies with identical Sharpe can sit on opposite
sides of the threshold, because Sharpe is indifferent to the win/loss shape that the tax
code is entirely built around. **Report profit factor on every result, always.**

## The threshold is higher than the closed form

Frictions are charged per round trip regardless of how far price moved, so the real
requirement depends on holding horizon, not on trade count. With 17 bps of round-trip
friction:

| Horizon | Adverse move | VDA_STRICT | VDA_NO_TDS | BUSINESS_INCOME |
|---|---|---|---|---|
| HFT / market making | 0.05% | 15.44 | 11.35 | 10.90 |
| Scalping | 0.20% | 4.95 | 3.93 | 3.47 |
| Intraday | 0.50% | 2.85 | 2.44 | 1.99 |
| Swing (days) | 1.50% | 1.92 | 1.78 | 1.33 |
| Position (weeks) | 4.00% | 1.63 | 1.58 | 1.12 |
| Trend (months) | 10.00% | 1.52 | 1.50 | 1.05 |

**The requirement is scale-invariant in trade count.** With fixed notional per trade,
doubling the number of trades doubles gross PnL, costs, TDS and tax alike. The axis that
matters is move size relative to friction — which is a statement about *holding horizon*,
not about *frequency*.

## Two regimes, two different games

Read the table columns, not just the rows:

- **At short horizons the tax regime barely matters.** HFT needs PF 15.44 under the harsh
  regime and 10.90 under the permissive one. Both are unreachable. Frictions, not tax, are
  what kill it.
- **At long horizons the regime is the whole game.** Trend needs 1.52 vs 1.05. That gap is
  the difference between a hard strategy and an easy one.

So the unsettled tax question only bites where the desk is actually meant to operate.

## What this changes

1. **Anything below the swing horizon is off the table.** This is the quantitative form of
   the qualitative claim in the architecture report, and it is stronger than that report
   assumed: the report called multi-day swing "marginal" — it needs PF 1.92, which is
   genuinely hard.
2. **Target positive skew explicitly.** A high-win-rate, small-edge strategy is a tax trap
   by construction: it maximises the count of taxable winners while minimising the size of
   each. Low win rate with large winners is the tax-efficient shape.
3. **Profit factor joins the promotion gates.** A candidate that clears Sharpe and fails
   PF is not a near-miss; it is a strategy that loses money.

## Corrections this produced

- **The four-number ladder in the report was wrong.** It specified
  `gross -> net of costs -> net of TDS -> net of tax`, which assumed TDS is an additive
  cost. It is a creditable *prepayment*: inserting it as an intermediate step yields a
  ladder whose last rung is higher than its third. Corrected to
  `gross -> net of costs -> net of tax -> net of TDS drag`.
- **TDS is charged on losing disposals too.** s.194S applies to the transfer, not the gain.
  Sell at a loss and 1% still leaves the account, with no liability to credit it against,
  so it sits idle until the refund is assessed. A losing year has a financing cost even
  when it has no tax bill.
- **Unrecoverable TDS is a venue question, not a tax one.** On an FIU-registered Indian
  exchange issuing Form 16A it is creditable and merely a carry cost. On a foreign venue
  where the obligation falls on you and no certificate is issued, it becomes a permanent
  loss. That belongs in the Delta-vs-Binance decision in [[ADR-011]].

## Open

The whole table shifts by a column depending on an unsettled question — see [[ADR-012]].
A strategy that survives only under `BUSINESS_INCOME` is a bet on that question.
