---
id:
type: experiment
title:
created:
tags: []
---

# Hypothesis

> Write this **before** any result exists. Pre-registration is enforced through git
> history: a trial refuses to run until the hypothesis is committed.

## Claim

One sentence. What do you believe is true, about what, over what period?

## Why it might be true

The mechanism. If you cannot name one, you have a pattern, not a hypothesis.

## Falsifier

**What observation would make you abandon this?** State it now, in advance, in numbers you
will read off an artifact — not in numbers you type here today.

## Skew and turnover

Indian VDA taxation constrains which strategy *shapes* are viable at all: it penalises
high turnover and small per-trade edge, independently of Sharpe. Favour few, large,
positively-skewed round trips. The rates themselves change and are modelled in code —
see [[ADR-012]] and [[LRN-000001]], and read the current figures from
`supercortex costs breakeven`. Do not restate them here.

## Trial budget

Declared before research begins. Overruns are allowed but recorded, and they inflate the
multiple-testing penalty. **Register it with `supercortex exp register`** — the number
lives in the registry, not in this frontmatter.

## Data required

Which registered datasets, and what would make them unfit?
