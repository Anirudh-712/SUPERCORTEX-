---
id: HOME
type: home
title: Supercortex
created: 2026-08-26
tags: [navigation]
---

# Supercortex

**Research is autonomous; capital is not.**

## Today — the five-minute review

Read in this order. All three are **generated**: run `uv run supercortex dash render`, and
never edit them.

| | |
|---|---|
| **[Attention](00-dashboard/generated/attention.md)** | What needs a human right now. Empty is the good state. |
| **[Pipeline](00-dashboard/generated/pipeline.md)** | Where every experiment stands — trials, budget, lifecycle, verdicts. |
| **[Evidence](00-dashboard/generated/evidence.md)** | Is the data still what it claims to be? |

If those links 404, the cockpit has not been rendered in this checkout yet.
[Dashboard notes](00-dashboard/README.md) explains what each page holds.

## Judgment indexes

Identity and reasoning. **No operational numbers** — those are on the pages above.

- [Experiments](00-dashboard/views/experiments.base)
- [Decisions](00-dashboard/views/decisions.base)
- [Datasets](00-dashboard/views/datasets.base)
- [Learnings](00-dashboard/views/learnings.base)

## Architecture

[Supercortex canvas](00-dashboard/supercortex.canvas) — how data becomes evidence, where
the human gates sit, and what each guard actually stops.

## Start something

| Template | Use when |
|---|---|
| [hypothesis](templates/hypothesis.md) | Beginning a line of research. Write it **before** any result exists. |
| [decision](templates/decision.md) | Recording a choice and what would reverse it. |
| [validation](templates/validation.md) | Reviewing someone else's trial. Reviewer ≠ producer. |
| [learning](templates/learning.md) | Something changed how this desk works. |

Register before you run: `supercortex exp register` allocates the ID and declares the trial
budget. An unregistered backtest corrupts the trial count and invalidates every
significance test built on it.

## Judgment anchors

- [[DEC-000002]] — both experiments reclassified as engine validation
- [[DEC-000001]] — kill EXP-000001, funding reversal
- [[LRN-000004]] — the benchmark is the binding gate
- [[LRN-000003]] — validate the engine with a fixture, not a hypothesis
- [[LRN-000002]] — mean reversion is the wrong skew for an Indian VDA desk
- [[LRN-000001]] — profit factor, not Sharpe, is the binding constraint

## The rule that keeps this vault honest

**Markdown holds judgment. The registry holds facts.**

**A human-authored note may not carry operational facts.** It may argue, conclude and
change its mind, but a trial count, a row count or a verdict belongs in `registry.sqlite`
and in hashed artifacts. A number typed into a note is a second copy free to go stale,
and this vault has already been bitten by exactly that.

Generated reports are the exception, and deliberately so: the pages above *do* mirror
registry facts. That is safe because they are regenerated from the registry on every
render, never hand-edited, and ignored by git.

Two hooks enforce it on the way in: one refuses a write containing a number no artifact
supports, the other re-reads what landed. See [[ADR-013]].

## Constitution

[CLAUDE.md](CLAUDE.md) — the operating laws, the jurisdiction constraints, the definition of
done. `AGENTS.md` is a byte-identical copy for tools that read that name instead; a test
fails if they drift.
