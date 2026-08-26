---
id: ADR-012
aliases: [ADR-012]
type: adr
title: The tax regime is a modelled input, not an assumption
status: ACCEPTED
created: 2026-08-24
tags: [tax, india, cost-model, falsification]
---

# ADR-012 — The tax regime is a modelled input, not an assumption

## Context

Two things are true about Indian crypto taxation as of 2026-08:

**Settled.** s.115BBH taxes gains on Virtual Digital Assets at 30% plus cess, permits no
deduction beyond cost of acquisition, and allows **no set-off of losses** — not against
other VDA gains, not against any other head, and no carry-forward.

**Unsettled.** Whether that regime, and the s.194S 1% TDS, apply to **cash-settled
derivatives** at all. Published guidance directly contradicts itself: some sources state
194S covers perpetual contracts, others that a derivative is not a "transfer of a VDA";
many practitioners treat derivative profits as business or speculative income instead.
INR-settled platforms market themselves on precisely this reading. There is no government
clarification.

The difference is not marginal. For a trend-following profile at PF 1.62, measured with
`supercortex costs regimes`:

| | VDA_STRICT | VDA_NO_TDS | BUSINESS_INCOME |
|---|---|---|---|
| Net of costs | 2,297 | 2,297 | 2,297 |
| **Net after tax** | **-1,682** | **-979** | **+1,049** |
| Kept | -73% | -43% | **+46%** |

The same trades, the same edge — profitable or ruinous depending on an open question.

## Decision

**The tax regime is a named, versioned input hashed into every trial**, not a constant
buried in the cost model. Three regimes are defined: `VDA_STRICT`, `VDA_NO_TDS`,
`BUSINESS_INCOME`.

1. **`VDA_STRICT` is the default** and the most conservative reading.
2. **Every result reports all three.** `compare_regimes()` exists so the default cannot
   quietly hide the spread.
3. **Promotion requires survival under `VDA_STRICT`.** A strategy that clears only under
   the permissive reading is not promoted — it is recorded as *contingent on the tax
   question* and parked.
4. **`cost_model_version` is recorded on every trial row.** Changing a rate without
   bumping the version makes two trials incomparable while looking identical.

## Why not just pick the likely answer

Because the desk's entire thesis is not fooling yourself, and picking here would be the
single highest-leverage place to do it. A constant in the code is invisible; a regime in
the config is hashed, diffable, and shows up in every report. If the law clarifies, one
line changes and every historical result can be re-derived under the new reading.

It also converts a legal uncertainty into a **measurable sensitivity**, which is the only
form in which it can enter the falsification battery.

## Consequences

- Strategies at short horizons are unaffected: frictions dominate there and all three
  regimes agree they are dead ([[LRN-000001]]).
- Strategies at the horizons this desk targets — weeks to months — are exactly where the
  regimes diverge most, so the question is live precisely where it hurts.
- A `contingent` outcome is now a legitimate falsification verdict, distinct from pass
  and fail.

## Required action, not deferrable

`UNVERIFIED` throughout. This is a modelling scaffold, not tax advice. Before any capital
is committed, put these three questions to a chartered accountant with VDA experience:

1. Does s.194S TDS apply to cash-settled crypto perpetual futures?
2. Does s.115BBH (30%, no set-off) apply to them, or are they business/speculative income?
3. Are exchange fees and funding paid deductible against VDA gains?

The answers select the regime. Until then the desk runs on `VDA_STRICT` and treats
anything that survives only elsewhere as unproven.
