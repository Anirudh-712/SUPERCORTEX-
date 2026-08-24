# Project Supercortex — operating rules

A one-person, AI-native quantitative research desk. Crypto first. Local-first.
**Research is autonomous; capital is not.**

Architecture rationale: `ARCHITECTURE-RESEARCH-REPORT.md`. Read it before proposing changes.

## The five laws

1. **NEVER write a number into a document unless you read it from a file produced by code.**
   No estimated Sharpe ratios. No "approximately". Cite the artifact path. A `PostToolUse` hook
   enforces this and will reject your write.
2. **NEVER run a backtest that has not been registered.** `supercortex exp register` first.
   Unregistered runs corrupt the trial count and silently invalidate every significance test.
3. **NEVER touch `06-risk/limits.yaml`, `.env*`, or the Keychain.** Ask the CIO.
4. **NEVER promote a strategy by editing a file.** Use `supercortex lifecycle promote`.
   The state machine decides, not you.
5. **If a result looks good, your next job is to try to kill it, not to celebrate it.**

## Jurisdiction — this changes what is worth researching

The operator is an **Indian resident**. This is a hard constraint on strategy design:

- Crypto (VDA): **30% flat tax on gains, no loss offset, no expense deduction, 1% TDS on
  transfers** (s.194S). High-turnover strategies are structurally unprofitable.
  **Target 12–50 round-trips/year, positive skew.** A 55%-win-rate small-edge strategy is a tax trap.
- **Offshore retail forex/CFD is a FEMA violation.** FX is **research-only** until counsel says
  otherwise. Do not write FX execution code.
- Never present a strategy result without four numbers: **gross → net of costs → net of TDS → net of tax.**

## Definition of done for a research task

- `hypothesis.md` written **before** results exist (pre-registration; enforced via git history)
- `config.yaml` is the only source of parameters — never pass params on the command line
- `code_commit` and `data_snapshot` recorded in the registry; git tree clean or the run refuses
- Falsification verdict recorded
- An **independent** reviewer agent has written `validation.md` (reviewer ≠ producer)

## Layout

| Path | Holds | Authority |
|---|---|---|
| `01-research/` … `21-learnings/` | Markdown prose — judgment | Human/agent written |
| `registry.sqlite` | Experiments, trials, verdicts — facts | **Machine written only** |
| `data/` | Parquet. Gitignored. `raw/` is immutable | Code written |
| `src/supercortex/` | All logic | Code |
| `06-risk/limits.yaml` | Risk limits | **CIO only — hook-denied to agents** |

Files starting with `<!-- GENERATED` must never be hand-edited.

## Commands

```
uv run supercortex --help          all operations
uv run pytest                      tests
uv run ruff check . && uv run mypy src
```

## Style

- Python 3.12, type-hinted, ruff-formatted. **Polars over pandas.** No Jupyter — marimo (`.py`) only.
- Markdown: frontmatter always. Link with `[[wikilinks]]`. Short paragraphs.
- IDs are immutable and zero-padded to 6 digits: `EXP-000042`. Never renumber, never reuse.

## When unsure

Write the uncertainty into the document as `UNVERIFIED:` and continue. **Never guess a number.**
