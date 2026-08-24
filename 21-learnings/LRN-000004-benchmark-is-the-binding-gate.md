---
id: LRN-000004
type: learning
title: Beating the benchmark is a harder gate than any statistic
created: 2026-08-24
source_experiment: EXP-000002
tags: [falsification, benchmark, beta, tax]
---

# LRN-000004 — The benchmark gate binds harder than any statistic

## Where this came from

During engine validation ([[DEC-000002]]) a fixture strategy produced a result that cleared
every bar the desk had at the time: profit factor 2.30 against a 1.45 break-even, positive
skew, a 34% win rate, few taxable events, and survival under all three tax regimes. Read
in isolation it looked like exactly the shape [[LRN-000002]] said to hunt for.

Then the benchmark was computed. **Buying once and holding kept roughly five times more
after tax.** The asset had risen eightfold over the window; the strategy captured a third
of the move and paid 44 rounds of frictions and assessments to do it.

Not one of the statistical tests could see this. Profit factor, Sharpe, PBO, the Deflated
Sharpe — every one of them evaluates the strategy against *itself*. None asks whether the
same money would have done better sitting still.

## Why this is worse under s.115BBH than elsewhere

The tax code makes the benchmark unusually hard to beat, because the two are charged so
differently:

| | Frictions | Taxable disposals |
|---|---|---|
| Buy and hold | **one** round trip | **one** |
| A strategy | one per round trip | **one per round trip** |

A directional strategy is therefore not competing with the asset's return. It is competing
with the asset's return **plus every cost and assessment it avoided by not trading**. In a
market that rose eightfold, an enormous edge is needed just to draw level, and profit factor
is blind to the comparison.

The corollary is uncomfortable and worth stating plainly: **for a long-biased strategy in a
bull market, the correct null hypothesis is not "no edge" — it is "buy and hold".**

## What was built

`falsify/benchmark.py` and the `G3-benchmark` gate. Two comparisons, both reported:

- **absolute** — net after tax must exceed buy-and-hold's. **Required for promotion.**
- **risk-adjusted** — return per unit of maximum drawdown. Informational only.

A strategy that loses on absolute while winning on risk-adjusted is flagged as a
**portfolio argument**: the claim that it could be levered up to match. That may be true,
and it is a legitimate conversation — but it is bounded by the risk engine's leverage
limits and it is a *separate decision*. It deliberately does not satisfy the gate, because
an escape hatch on a benchmark test is how the benchmark stops mattering.

## The general form

> **Every statistic in the battery measures a strategy against itself. Only a benchmark
> measures it against the alternative of not bothering.**

Any battery without one can hand you a rigorous, well-evidenced, thoroughly falsified
verdict on a strategy that a savings account would have beaten.

## Consequence for what the CIO should supply

This shifts what "a good strategy" means for this desk. Anything long-biased in crypto now
carries a benchmark burden that scales with how much the asset rose. The families where the
gate is naturally satisfiable are those whose returns do **not** come from being long the
asset: market-neutral, cross-sectional, basis and carry structures, or strategies that
genuinely sit out drawdowns rather than merely participating less.
