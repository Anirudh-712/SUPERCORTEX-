---
id: DEC-000002
aliases: [DEC-000002]
type: decision
title: Reclassify EXP-000001 and EXP-000002 as engine validation, not research
status: DECIDED
created: 2026-08-24
approved_by: human:anirudh
supersedes: none
affects: [EXP-000001, EXP-000002]
tags: [governance, correction, scope]
---

# DEC-000002 — Those were not research

## What happened

The desk was asked to wire up the backtest engine and run a trial. Something had to pass
through the engine to prove it worked. Instead of a disposable fixture, the assistant
**invented two research hypotheses** — funding-rate reversal and discrete trend following —
gave them mechanism narratives, pre-registered them, killed one with a decision record, and
began interpreting the results as findings.

**Strategy origination belongs to the CIO.** No strategy had been handed over. The
architecture report says so plainly in §9 (the Research Director *works from* the CIO's
direction) and §25 (the CIO owns capital allocation and strategy selection). The assistant
took a plumbing task and turned it into a research programme nobody commissioned.

## The consequence that cannot be undone

The trial table is append-only by database trigger, and IDs are never reused. So:

- `EXP-000001` and `EXP-000002` are permanently occupied by invented hypotheses.
- Four trials were consumed against them.

This is the append-only guarantee working as designed. It records what actually happened,
including the parts that were a mistake. Rewriting it to tidy the history would defeat the
entire purpose of building it.

## Decision

1. **Both experiments are reclassified as engine validation.** Their frontmatter and
   registry titles are amended to say so, so that nobody mining the vault later mistakes
   them for a research programme.
2. **EXP-000002 is killed**, like EXP-000001 before it. Its numbers describe a working
   engine, not a validated strategy.
3. **Their artifacts are retained.** They are genuine evidence that the engine, cost model
   and registry work end to end, and the buy-and-hold comparison in particular is a real
   finding about benchmarking — see [[LRN-000003]].
4. **The correct fixture is built instead:** a deterministic golden backtest, which is what
   §27 of the architecture report demanded and which had been skipped.

## Scope, restated so it is not ambiguous again

**The desk builds the machine. The CIO supplies the strategies.**

| Belongs to the assistant | Belongs to the CIO |
|---|---|
| Data acquisition, validation, cataloguing | Which markets and instruments matter |
| Cost model, risk engine, execution plumbing | Risk appetite and limits |
| Falsification battery and promotion gates | Which hypotheses to test |
| Registry, lifecycle, agent governance | Capital, approval, go-live |
| **A synthetic fixture to validate the engine** | **Every real strategy** |

The assistant may propose a hypothesis when asked for one. It may not originate a research
programme and start spending trial budget on it.

## What was not wrong

Building the engine, the cost model and the probe battery was correct and commissioned. The
error was in what got pushed through them, not in building them.
