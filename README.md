# Project Supercortex

An AI-native quantitative research desk for one person, one Mac Mini.
Crypto first. Local-first. **Research is autonomous; capital is not.**

- Architecture and rationale: [`ARCHITECTURE-RESEARCH-REPORT.md`](ARCHITECTURE-RESEARCH-REPORT.md)
- Operating rules for agents: [`CLAUDE.md`](CLAUDE.md)

## Quick start

```bash
uv sync
uv run supercortex doctor      # verify the guarantees hold
uv run supercortex health      # one-screen status
uv run pytest                  # the registry's guarantees as tests
```

## The idea in one paragraph

Finding strategies is easy and getting easier; agents make it trivial. The hard problem
is not fooling yourself. So the spine of this system is a registry that counts **every**
backtest ever run — including the abandoned ones — because the Deflated Sharpe Ratio is
only honest if that count is honest. Around it sits a falsification engine whose job is
to kill strategies, a cost model that knows India taxes gains at 30% with no loss offset,
and a set of hooks that make an agent physically unable to write a number it did not
compute.

## Status

| Phase | State |
|---|---|
| 0 · Foundation | ✅ tree, CLAUDE.md, hooks, settings |
| 1 · Registry + second brain | ✅ registry, CLI, anti-fabrication hook, tests |
| 2 · Data | ⬜ next |
| 3 · Backtest + cost model | ⬜ |
| 4 · Falsification engine | ⬜ |
