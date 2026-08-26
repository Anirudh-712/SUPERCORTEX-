---
id: DEC-000001
aliases: [DEC-000001]
type: decision
title: Kill EXP-000001 — BTC perp funding-rate reversal
status: DECIDED
created: 2026-08-24
approved_by: human:anirudh
experiment: EXP-000001
tags: [kill, funding, mean-reversion]
---

# DEC-000001 — Kill EXP-000001

## Decision

**Kill it.** Not park, not iterate. The evidence is in
[[EXP-000001]] `results.md`, generated from sealed trial 2.

## Evidence

The full numbers are in `02-experiments/EXP-000001-btc-perp-funding-rate-reversal/results.md`
and are not restated here — the results document is generated from the sealed artifact and
independently re-checked against the registry, so it is the authoritative statement.

The three facts that decide it:

1. **Profit factor is below the break-even threshold.** Not marginally. The outcome was
   determined by the win/loss shape before any tax arithmetic ran.
2. **It fails under all three tax regimes**, including the permissive business-income
   reading. So it is not *contingent* on the unsettled question in [[ADR-012]] — there is
   no reading of the law under which this makes money.
3. **Execution frictions alone consume most of the gross.** Even at a zero tax rate the
   remainder is thin.

## Why not iterate

The obvious move is a parameter sweep — widen the entry band, lengthen the hold, chase a
higher profit factor. Rejected, for two reasons:

**The gap is structural, not parametric.** The required profit factor at this holding
horizon is documented in [[LRN-000001]]. The observed figure is not near it. Closing a gap
that size by tuning is not optimisation, it is fitting.

**Every sweep burns trials and inflates the multiple-testing penalty.** The registry counts
every look at the data. Spending twenty trials to rescue an idea that is structurally the
wrong shape would make the Deflated Sharpe Ratio of every *later* idea in this experiment
worse, for nothing. The budget is better spent on a hypothesis with the right shape.

## The deeper reason: skew

This is the finding worth carrying forward. Funding-reversal is a **mean-reversion** shape:
many small wins, occasional large losses. Under s.115BBH that is close to the worst
possible profile, because the tax code charges you on every winner and refunds nothing on
the losers. A strategy family that maximises the *count* of taxable events while minimising
the *size* of each is a tax trap by construction.

The desk should be hunting **positive skew** — low win rate, large winners — which is the
opposite shape. That is not a preference; it falls out of the arithmetic in [[LRN-000001]].

## Caveat recorded honestly

Falsification test 1 (look-ahead probe) was run and did not detect leakage, **but it is weak
evidence for this strategy**: the signal lives in the funding series and the probe shifted
bars. This does not change the decision — the strategy fails on economics regardless of
whether it also leaks — but the battery has a gap and that gap is recorded in
`results.md` and carried into [[LRN-000002]].

## Consequences

- EXP-000001 → `KILLED`. Three trials consumed of a forty-trial budget; the unspent
  thirty-seven are **not** transferable. Budgets are per-hypothesis for a reason.
- Successor registered as EXP-000002, targeting the horizon and skew this analysis points
  at.
- Before EXP-000002 reaches falsification, the funding-shift probe must exist.
