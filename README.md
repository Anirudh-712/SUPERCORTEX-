# Project Supercortex

An AI-native quantitative research desk for one person, one Mac Mini.
Crypto first. Local-first. **Research is autonomous; capital is not.**

- Design study, dated 2026-08-24, written before anything was built:
  [`ARCHITECTURE-RESEARCH-REPORT.md`](ARCHITECTURE-RESEARCH-REPORT.md). It records the
  reasoning behind the tool choices. Parts of the layout it sketches are still unbuilt —
  for what exists today, run `doctor` and `health` below.
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

The first thing the cost model told us: under s.115BBH the break-even **profit factor** is
1.4535, and once frictions are included a day-horizon strategy needs 1.92. Sharpe ratio
cannot see that constraint. Run `uv run supercortex costs breakeven`.

## Status

| Phase | State |
|---|---|
| 0 · Foundation | ✅ tree, CLAUDE.md, hooks, settings |
| 1 · Registry + second brain | ✅ registry, CLI, anti-fabrication hook, tests |
| 2 · Data | ✅ klines, funding, open interest — verified, carded, gap-inventoried |
| 3 · Cost model | ✅ India VDA model, 3 tax regimes, 100% covered |
| 4 · Backtest | ✅ Nautilus runner, registered trials sealed with hashed artifacts |
| 5 · Falsification engine | 🟡 partial — static battery entrypoint exists; dynamic/SPA tests and the first recorded verdict remain |
