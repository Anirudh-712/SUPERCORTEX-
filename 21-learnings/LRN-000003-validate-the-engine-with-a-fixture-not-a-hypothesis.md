---
id: LRN-000003
type: learning
title: Validate the engine with a fixture, not a hypothesis
created: 2026-08-24
decision: DEC-000002
tags: [governance, testing, engine, scope]
---

# LRN-000003 — Validate the engine with a fixture, not a hypothesis

## The mistake

Asked to wire up the backtest engine and run a trial, the assistant invented a research
hypothesis to push through it — then a second one — complete with mechanism narratives,
pre-registration and a kill decision. **Strategy origination belongs to the CIO.** See
[[DEC-000002]].

The reasoning that led there was not obviously wrong at each step: you cannot validate an
engine without running *something* through it. The error was choosing a **plausible**
something instead of a **disposable** one. A plausible hypothesis invites interpretation,
and interpretation is research.

## Why a fixture is strictly better for the job

A synthetic fixture beats a real hypothesis at validating an engine on every axis:

| | Invented hypothesis | Synthetic fixture |
|---|---|---|
| Is the answer knowable in advance? | No | **Yes, by arithmetic** |
| Does market data drift under it? | Yes | No |
| Does it consume trial budget? | Yes | No |
| Does a failure localise the bug? | No — could be strategy or engine | **Yes — only the engine** |
| Does it invite interpretation? | Yes, and it did | No |

A hypothesis run through an unvalidated engine confounds two unknowns. When the number
looks odd you cannot tell whether the idea is bad or the machine is broken.

**Rule:** the engine is validated by `supercortex.backtest.golden`, which uses a closed-form
sawtooth and a strategy that enters and exits on fixed bar indices. It touches no market
data and no registry, so it can run on every commit forever.

## What the fixture immediately found

Two properties of the engine that four trials of "research" had not surfaced, because
nobody can eyeball a wrong fill inside 386 trades on real data:

### 1. Any non-zero latency costs a whole bar

Signal is taken on bar 10's close. Measured fill bar by latency setting:

| `latency_ms` | fill bar | delay |
|---|---|---|
| 0 | 11 | 1 bar — the correct "act on close, fill next open" |
| 1 | 12 | **2 bars** |
| 250 | 12 | 2 bars |
| 1000 | 12 | 2 bars |
| 3600000 | 13 | 3 bars |

**One millisecond and one second produce identical results.** The latency model quantises to
bar boundaries, so at hourly granularity `latency_ms` is effectively a boolean.

A config saying `latency_ms: 250` does not model a quarter-second. It models **one extra
hour**. That is conservative, which is the right direction to be wrong in, but a parameter
whose name implies milliseconds and whose effect is measured in bars will mislead whoever
reads the config next. Every engine-validation run so far filled two bars late.

### 2. Fills are adverse in both directions

Nautilus synthesises a spread around the bar under `L1_MBP`, so a buy fills above the open
and a sell below it, by a constant offset independent of bar range. Conservative and
symmetric — but it means the cost model is not the only thing charging for execution, and
the two must not be allowed to drift into double-counting.

## Consequences

- `latency_ms` must be documented in every config as "non-zero means one extra bar at this
  granularity", or replaced with an explicit `fill_delay_bars`.
- The golden values are frozen in `tests/test_golden.py`. Any change to the engine, bar
  construction, positions parsing, cost model or tax arithmetic moves them and must be
  explained before the baseline is updated.
- The frozen numbers are labelled **engine-derived, not predicted**. Freezing a value you
  did not predict is still valuable — it detects change — but claiming you predicted it
  would be a different and false claim.

## The general form

> **Validate a machine with an input whose correct output you already know.**
> Anything else confounds "the machine is broken" with "the input was bad", and if the
> input is interesting enough to interpret, someone will interpret it.
