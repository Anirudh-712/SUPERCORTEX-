---
id: ADR-014
type: adr
title: Stay on NautilusTrader v1 and keep its mypy errors visible
status: ACCEPTED
created: 2026-08-24
decided_by: CIO
supersedes_part_of: none
tags: [typing, dependencies, nautilus, migration]
---

# ADR-014 — Stay on NautilusTrader v1 and keep its mypy errors visible

## Context

The mypy cleanup cleared the data, risk, falsification and lifecycle boundaries. What
remains is a single cluster, and it is not ours to fix: **23 errors across 5 files**, of
which 22 trace to NautilusTrader and 1 does not.

### v1 ships a `py.typed` marker it cannot honour

NautilusTrader 1.231.0 places `py.typed` at its package root, which tells a type checker
the package is typed. The file census says otherwise:

| Artifact | Count |
|---|---|
| `.py` (checkable) | 397 |
| `.so` (compiled Cython extensions) | 111 |
| `.pyi` (stub files) | 4 |

The four stubs are `common/messages.pyi`, `core/datetime.pyi`, `core/uuid.pyi` and
`core/nautilus_pyo3.pyi`. The last is large and covers the Rust/PyO3 layer. **None of them
covers a module this desk imports.**

That marker does, however, split our imports cleanly. Of the eleven NautilusTrader modules
we import, **six already type-check** because they are pure Python inside a `py.typed`
package: `backtest.models`, `config`, `model.currencies`, `model.enums`,
`model.instruments`, `test_kit.providers`.

**Five do not**, because each is a compiled extension with no sibling `.pyi`:

- `nautilus_trader.model.data` — `Bar`, `BarType`, `FundingRateUpdate`
- `nautilus_trader.model.identifiers` — `InstrumentId`, `Venue`
- `nautilus_trader.model.objects` — `Price`, `Quantity`, `Money`
- `nautilus_trader.backtest.engine` — `BacktestEngine`, `BacktestEngineConfig`
- `nautilus_trader.trading.strategy` — `Strategy`

That is **eleven imported symbols** in total, spread over 30 import lines in five files: 18
lines in `feed.py`, `runner.py` and `golden.py`, and 12 in the two strategy files.

So the problem is not "NautilusTrader is untyped". It is five compiled modules and eleven
symbols, one of which — `Strategy` — is subclassed three times and carries 197 public
names.

## Options considered

Each was measured against the real tree with `mypy --any-exprs-report`, from a throwaway
configuration. Nothing in the project was altered to run them.

### 1. Scoped `ignore_missing_imports` for `nautilus_trader.*`

Errors fall from 23 to 5. **Every `Any` count is unchanged**: `feed` 37, `runner` 178,
`golden` 12, `trend` 28, `funding_reversal` 27 — identical to the baseline, module for
module.

This is the option that looks like progress and is not. It suppresses the report of the
gap while preserving the gap exactly, which is the same failure mode the falsification
battery exists to prevent: a green result produced by not asking the question. Rejected.

### 2. Partial repository-owned stubs

Stubs were written for all five modules and eleven symbols and measured. Errors fall from
23 to 6, and the result **splits by layer**:

| Module | baseline `Any` | with stubs |
|---|---|---|
| `feed` | 37 | 17 |
| `runner` | 178 | 122 |
| `golden` | 12 | 37 |
| `trend` | 28 | 87 |
| `funding_reversal` | 27 | 106 |

The data layer improves substantially — `feed` sheds 54% of its `Any` expressions and
`runner` 31%, because `Bar`, `Price`, `Quantity` and `BarType` are small, stable value
types that are cheap to describe accurately.

The strategy layer gets **worse**, and the reason is structural rather than a flaw in the
draft. A minimal `Strategy` stub must declare `config`, `cache`, `log`, `portfolio` and
`order_factory` as `Any`, so every `self.config.x` becomes a counted `Any` expression. The
stub does not remove the `Any` — it relocates it and makes it visible. Removing it for real
means modelling `Cache`, `Portfolio`, `OrderFactory`, `Logger` and 197 methods, and then
keeping that model correct across upgrades.

Incompleteness also **manufactures false positives**. The draft produced three, each a
false accusation against working code:

- `"DiscreteTrend" has no attribute "stop"` — `Strategy.stop` exists at runtime
- `Argument 1 to "float" has incompatible type "Price"` — `Price.__float__` exists at
  runtime, twice

Paying those off means either growing the stub indefinitely or writing `type: ignore` into
CIO-owned strategy files. Rejected.

### 3. A typed Supercortex compatibility wrapper around NautilusTrader

A single module owning every NautilusTrader import, re-exporting the eleven names.

This was the recommendation put to the CIO and it was **rejected**, correctly. A wrapper
changes where the imports live; it does not change what they resolve to. With
`trading.strategy` still unresolved, the wrapper re-exports `Strategy` as `Any` and the
strategy modules inherit exactly the typing they have today. The wrapper would relocate the
problem into a new file and add a layer of indirection that must itself be maintained,
without typing anything that is not already typed. Its only genuine benefit is migration
blast radius, and that benefit is available later at lower cost — see below.

### 4. Wait for NautilusTrader v2

**NautilusTrader [v2.0.0rc3][rc3] is out.** Its release notes list, as the first breaking
change, "Removed legacy v1 Cython package and root build path; use the Rust + PyO3
package."

The date needs stating in both timezones or the official page and this desk's calendar look
like they disagree. The release notes read "Released on 20th August 2026 (UTC)"; GitHub
stamps the publication `2026-08-21T01:20:07Z`, which is **2026-08-21 06:50 IST**. So: 20
August UTC by the maintainers' own wording, 21 August on this desk's clock. Both are the
same event.

The v2 tree confirms what that means for typing: **0 `.pyx` files, 41 `.pyi` files**
alongside 49 `.py`, plus `py.typed`, under a relocated `python/nautilus_trader/`. The stubs
are machine-generated by `pyo3_stub_gen` — `model/__init__.pyi` is 261,667 bytes,
`trading/__init__.pyi` 38,918 and `backtest/__init__.pyi` 18,273. Every symbol this desk
uses appears in them with a real signature, and v2's `Bar.__init__` matches the call shape
already in `feed.py`.

[`MIGRATION_V2.md` at `v2.0.0rc3`][migration] states the contract directly: *"Use the
generated type stubs in `python/nautilus_trader/` as the supported Python contract."*

v2 also **flattens the module tree**: `nautilus_trader.backtest.engine.BacktestEngine`
becomes `nautilus_trader.backtest.BacktestEngine`, and all five of the module paths we
cannot resolve today cease to exist. Any stub written against those paths is therefore not
merely superseded at v2 — it describes deleted modules.

v2 is a release candidate published days before this decision. Its [roadmap issue][roadmap]
is open and its Known Limitations list real gaps. None of them touches how this desk drives
the engine, but an rc is not a foundation for a research desk this week.

## Decision

**Remain on NautilusTrader v1 and keep its mypy errors visible.**

1. No compatibility wrapper is built.
2. No local stubs, no `ignore_missing_imports`, no per-module mypy overrides, no
   `type: ignore` comments.
3. The dependency constraint becomes `nautilus-trader>=1.231.0,<2`, so a v2 release cannot
   arrive through a routine lockfile refresh and change the engine underneath a strategy
   without a deliberate decision.
4. Migration waits for a **stable** v2 and uses its official generated stubs.

The remaining errors stay in every `mypy src` run as an honest, permanent reminder that
this boundary is untyped. A suppressed error is a decision nobody revisits; a visible one
is a standing question. That is the same reasoning as "a test that cannot run is a FAIL".

## Consequences

`uv run mypy src` reports **23 errors in 5 files**, categorised as:

| Cause | Count |
|---|---|
| `import-not-found` — five compiled v1 modules, no stubs | 18 |
| `misc` — "Class cannot subclass `Strategy` (has type `Any`)", three subclasses | 3 |
| `no-any-return` in `funding_reversal.py:110`, downstream of `Strategy` being `Any` | 1 |
| `operator` in `trend.py:125` — unrelated to NautilusTrader | 1 |

The last one is **not** a NautilusTrader artefact and no stub decision will clear it.
`collections.deque.maxlen` is declared `int | None` because an unbounded deque returns
`None`; this instance is constructed with `deque(maxlen=config.lookback_days *
BARS_PER_DAY + 1)`, always an integer, so the comparison cannot raise here. It is a
**static proof gap**, not a demonstrated live defect, and it sits in CIO-owned strategy
code. It is recorded here and left untouched.

Every other module in `src/supercortex/` is clean under `strict = true`.

## Migration preconditions

None of these is optional. Migration does not begin until all of them hold.

1. **A stable v2 release** — not an rc. The Known Limitations list must not intersect this
   desk's usage.
2. **An isolated migration branch or git worktree.** The v1 and v2 packages cannot coexist
   in one environment, so the migration must not disturb the working environment that
   produces registered trials.
3. **Golden-backtest parity.** `supercortex.backtest.golden` is the synthetic fixture that
   exists to validate the engine without spending a hypothesis. Its results must match
   under v2 before any real experiment is re-run. A migration that changes the numbers is
   a migration that invalidates every sealed trial compared against them.
4. **Full verification** — the complete pytest suite, project-wide Ruff, project-wide mypy,
   `doctor` and data verification, all green on the migration branch.
5. **Explicit CIO approval before any strategy import changes.** 12 of the 30 NautilusTrader
   import lines are in `trend.py` and `funding_reversal.py`. Under law 6 those files are
   CIO territory and no agent edits them without being asked.

At that point the wrapper rejected above becomes cheap and unnecessary in equal measure:
the import paths change once, upstream's generated stubs supply the types, and the 22
stub-related errors disappear without this repository owning a single line of stub code.

## Evidence

Every number above was measured on 2026-08-24 against the installed package and the
official repository, not estimated:

- installed distribution metadata and the file census under `.venv/.../nautilus_trader`
- `mypy --any-exprs-report` runs from a throwaway configuration, baseline and per option
- `dir(Strategy)` for the 197-name public surface
- [`nautechsystems/nautilus_trader`][repo] at tag `v2.0.0rc3` for the v2 tree, the
  generated stub sizes, the [release notes][rc3] and the [migration guide][migration]

[rc3]: https://github.com/nautechsystems/nautilus_trader/releases/tag/v2.0.0rc3
[migration]: https://github.com/nautechsystems/nautilus_trader/blob/v2.0.0rc3/MIGRATION_V2.md
[roadmap]: https://github.com/nautechsystems/nautilus_trader/issues/4042
[repo]: https://github.com/nautechsystems/nautilus_trader

No third-party or unofficial stub package was installed or consulted.

## Related

- [[ADR-013]] — preventive number validation, the same principle applied to evidence
- [[DEC-000002]] — strategy origination belongs to the CIO, which is why the strategy
  import lines are gated
