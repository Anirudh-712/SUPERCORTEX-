---
id: LRN-000002
type: learning
title: Mean reversion is the wrong skew for an Indian VDA desk
created: 2026-08-24
source_experiment: EXP-000001
decision: DEC-000001
tags: [tax, skew, strategy-selection, falsification]
---

# LRN-000002 — Mean reversion is the wrong skew

The first hypothesis died. Three things are worth carrying forward, and only one of them
is about funding.

## 1. The tax code selects a strategy *shape*, not just a horizon

[[LRN-000001]] established the profit-factor threshold. EXP-000001 is the first concrete
demonstration of what that means in practice, and the mechanism is worth stating plainly:

Under s.115BBH every winning disposal is assessed and every losing one refunds nothing. A
strategy family is therefore penalised in proportion to **how many taxable events it
creates relative to the size of each**.

| Shape | Win rate | Winner size | Taxable events | Verdict |
|---|---|---|---|---|
| Mean reversion | high | small | many | taxed on all of them, refunded on none |
| Carry / funding harvest | very high | tiny | very many | worst possible profile |
| Trend / momentum | low | large | few | the shape the code rewards |

This inverts the usual intuition. A high win rate feels like a good strategy and is, in a
normal tax jurisdiction. Here it is a liability: **it maximises the count of assessments
while minimising the size of each**. Funding-harvest — the obvious "safe" crypto carry
trade — is the worst profile available, precisely because it wins small and often.

**Search positive skew. Not as a preference — it falls out of the arithmetic.**

## 2. A gross-profitable strategy can be deeply loss-making, and gross is what you see first

EXP-000001's gross figure looks like a working strategy. Execution frictions removed most
of it and tax removed the rest and more. Every number is in the generated `results.md`.

If the desk had reported gross — or even gross net of fees, which is what most backtesters
show by default — this would have looked like a candidate worth developing. It took the
cost model to see that it was never viable, and the cost model existed only because it was
built in week one rather than deferred.

**Consequence:** no result is ever quoted without all four rungs of the ladder and the
profit factor. A single number from a backtest is not a result, it is a fragment.

## 3. A probe that does not move the needle is not automatically a pass

The look-ahead probe shifted **bars**. The signal lived in **funding**. Shifting bars
perturbs fill prices roughly at random and cannot detect leakage in the channel that
carries the signal, so its silence says nothing.

This is a defect in the falsification battery, not in this strategy. The general form:

> **A shift-probe is only informative if the shifted quantity is the signal source.**
> Otherwise it produces a reassuring null that means nothing.

**Required before any strategy reaches falsification:** the probe must shift *every*
input series the strategy consumes, not just the price bars. Recorded as a gap; test 1
cannot be called passed on EXP-000001 or anything else until this exists.

## 4. Method notes

- **Killing early is cheap and correct.** Three trials of forty. The alternative — sweeping
  parameters to rescue a structurally wrong shape — would have burnt the budget and
  inflated the multiple-testing penalty for every later idea in that experiment.
- **Budgets do not transfer between hypotheses.** The thirty-seven unspent trials died with
  EXP-000001. That is deliberate: a budget is a pre-commitment about *one* idea, and
  letting it roll over would make it meaningless.
- **IDs must be allocated by the registry.** [[LRN-000001]] was hand-written as a file with
  an ID nobody issued, so the counter later re-issued that ID to a different note. Caught
  and reconciled by fixing the ledger rather than renumbering a document already cited by
  [[ADR-012]] and [[DEC-000001]] — but the rule is now unambiguous: no ID exists until the
  registry issues it.

## Carried into EXP-000002

Target the shape this analysis points at: **low win rate, large winners, weeks-to-months
holding period, few taxable events**. And note the design constraint that falls out of it —
continuous rebalancing is a tax cost, so position changes must be discrete and infrequent.
