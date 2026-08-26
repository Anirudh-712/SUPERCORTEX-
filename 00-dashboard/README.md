---
id: DASH-README
type: dashboard
title: Cockpit
created: 2026-08-26
tags: [dashboard, navigation]
---

# Cockpit

The five-minute review. This page is **human-owned and committed**; everything it links to
is machine-generated, local and disposable.

```bash
uv run supercortex dash render
```

## The three views

| View | Answers | Read it when |
|---|---|---|
| [Attention](generated/attention.md) | What needs a human right now? | **First, every time.** Empty is the good state. |
| [Pipeline](generated/pipeline.md) | Where does every experiment stand? | After Attention, to see what is moving. |
| [Evidence](generated/evidence.md) | Is the data still what it claims to be? | Before trusting any result built on it. |

**Attention** lists over-budget experiments, unsealed trials, falsification tests that did
not pass, dataset hash failures, and failed **hook-enforcement** health checks — the checks
that confirm Law 1 is enforced rather than merely written down. It is not the full `doctor`
report. Each line names something to decide or repair. When there is nothing, it says so
explicitly rather than rendering an empty page.

Schema drift never reaches this page: a registry that does not match `schema.sql` makes
`dash render` refuse outright, and the diagnostic goes to the terminal. There is no
dashboard to read in that situation, which is the point.

**Pipeline** carries each experiment's identity and status, trials consumed against
declared budget, the split across ok / abandoned / unsealed, the latest lifecycle
transition, and any falsification verdict. Where a fact has never been recorded it says so
in words — never a zero standing in for the unknown.

**Evidence** carries each registered dataset's identity, coverage window, row count, gap
inventory and a **freshly computed** hash check of its Parquet bytes against the registry.
Two things are deliberately absent: *completeness*, because the registry stores a gap
inventory rather than a completeness figure, and *staleness*, because no dataset-freshness
threshold is configured — the only threshold that exists is a live-tick kill-switch bound
and means nothing for an archive-derived research dataset.

## What these files are

`registry.sqlite` and the hashed dataset bytes are **authoritative**. The generated pages
are a **mirror**: derived, ignored by git, and safe to delete at any moment — `dash render`
reconstructs them byte-for-byte from unchanged inputs.

A mirror necessarily restates facts, so these pages do repeat what the registry holds. What
never happens is the reverse: **nothing reads a fact back out of generated Markdown**, and
no generated fact is copied into a hand-editable note's frontmatter.

Each generated page begins at byte zero with the `<!-- GENERATED` marker, which is what
makes `guard_write.py` refuse to let an agent edit one.

## Notes

- `generated/` is a symlink into `.runtime/generations/<page-digest>/`. Both are
  gitignored.
- Two digests, deliberately distinct. Each page header shows a **registry-state**
  digest of the collected facts. The generation directory is named by a digest of the
  **rendered page bytes**, so a change to the renderer produces a new generation
  rather than colliding with an old one.
- No clock is read anywhere, so re-rendering unchanged state produces byte-identical
  files and reuses the same generation.
- Publishing swaps the symlink atomically: a reader sees the previous dashboard or the new
  one, never a half-written page. This is visibility under normal interruption, not
  power-loss durability.
- The command is read-only with respect to the registry, datasets, artifacts, notes,
  lifecycle state and risk configuration. It never opens the trial artifact tree.
- If the links above 404, the cockpit has not been rendered in this checkout yet. Run the
  command.
- Reconstruction is byte-for-byte **only against unchanged inputs** — all three of
  them: the registry facts, the hashed dataset bytes, and the hook-enforcement
  configuration that Attention reports on. Change any one and the pages change, which
  is the point. These pages are a snapshot, not an archive: an older generation cannot
  be regenerated once its inputs have moved on, and nothing here is a substitute for
  the registry as the record.
