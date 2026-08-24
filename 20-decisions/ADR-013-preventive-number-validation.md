---
id: ADR-013
type: adr
title: Move the fabricated-number validator from detective to preventive
status: PROPOSED — not implemented in this commit
created: 2026-08-24
supersedes_part_of: none
tags: [governance, hooks, anti-fabrication]
---

# ADR-013 — Move the fabricated-number validator from detective to preventive

## Context

`no_fabricated_numbers.py` runs on **`PostToolUse`**. The sequence today is:

```
Claude writes the file  ->  file lands on disk  ->  hook reads it  ->  exit 2  ->  Claude told to fix
```

It works — it has already blocked a laundered Sharpe ratio, and it caught the author writing
a starting balance that no artifact contained. But its position in the lifecycle carries
three defects that no amount of tuning fixes.

### 1. The bad content exists on disk, however briefly

Between the write and the rejection, a file containing fabricated numbers is a real file. A
concurrent process, a file watcher, an editor with the file open, an Obsidian sync, or a
`git add -A` in another terminal can all observe or capture it. The window is small and it
is not zero. For a control whose entire purpose is that a fabricated number never becomes
evidence, "briefly true" is the wrong guarantee.

### 2. Rejection is advisory, not binding

`PostToolUse` exit 2 shows stderr to Claude. It does **not** undo the write. Recovery
depends on the model reading the message and choosing to correct the file. That is the same
class of guarantee as an instruction in CLAUDE.md — and the whole reason hooks exist is that
instructions are not enforcement. A `PreToolUse` `permissionDecision: "deny"` is refused by
the client regardless of what the model decides.

### 3. A partially-corrected file can pass on the second attempt

If the first write contains five fabricated numbers and the fix removes four, the hook
re-runs on the corrected file and evaluates only what remains. Nothing checks that the
intermediate state was ever discarded. The audit trail shows a clean final file with no
record that an unclean one existed.

## Proposed design

Move validation to **`PreToolUse`** on `Write|Edit`, and validate the *proposed* content
rather than the written file.

| | Today (`PostToolUse`) | Proposed (`PreToolUse`) |
|---|---|---|
| Reads | the file on disk | `tool_input.content` / `new_string` |
| Bad content on disk | yes, briefly | **never** |
| Enforcement | stderr, model may ignore | `permissionDecision: "deny"`, client refuses |
| Failure mode | detective | **preventive** |

### What changes mechanically

1. **Input source.** `PreToolUse` supplies the intended content in `tool_input`, so the hook
   parses that string instead of re-reading the path. For `Write` that is
   `tool_input.content`; for `Edit` it is `tool_input.new_string`.

2. **Edit needs the merged result, not the fragment.** An `Edit` replaces a substring, so
   `new_string` alone is not the document. Validating the fragment in isolation would miss
   a fabricated number that survives *outside* the edited region, and would also produce
   false positives on a fragment whose supporting context sits elsewhere in the file. The
   hook must reconstruct the prospective document: read the current file, apply the
   `old_string` → `new_string` substitution in memory, and validate that. Never write it.

3. **Output contract.** Emit
   `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
   "permissionDecisionReason": "..."}}` on stdout with exit 0, instead of writing to stderr
   with exit 2.

4. **Provenance lookup is unchanged.** The trusted-artifact query against `registry.sqlite`
   already works on paths rather than content, so it carries over as-is.

### Why this was not done in this commit

The redesign touches the one control the desk relies on most, and a bug in it fails in the
worse direction — a `PreToolUse` hook that errors could deny every write, or worse, an
overly permissive rewrite could silently stop denying anything. It needs its own commit with
its own tests, specifically:

- a `Write` whose content contains a fabricated number is denied and **the file does not
  appear on disk afterwards**
- an `Edit` whose fabrication lies outside the replaced fragment is still caught
- an `Edit` whose replaced fragment is legitimate but whose surrounding document is not is
  handled correctly and the reasoning is stated
- a legitimate write is not denied
- **a malformed or unreadable payload on a watched path is DENIED with a diagnostic**

## Failure semantics: fail closed on anything watched

The current `PostToolUse` hook exits 0 — silently allowing — whenever it cannot parse its
input. That was tolerable for a detective control that only ever reported. It is **not**
tolerable for a preventive one, because "the hook broke" and "the content was clean" would
become indistinguishable, and the failure would be silent in exactly the direction that
loses the guarantee.

The preventive hook resolves every write into one of four states, and only two of them
allow:

| State | Decision |
|---|---|
| Positively identified as **outside** the watched paths | **allow** |
| Watched, content reconstructed, every number traced to a registry-vouched artifact | **allow** |
| Watched, content reconstructed, a number is unsupported | **deny** — naming the values |
| **Cannot determine** whether it is watched, or cannot reconstruct or validate the content | **deny** — naming what could not be determined |

The fourth row is the change. Unparseable JSON, a missing `file_path`, an `old_string` that
does not occur in the target file, an unreadable target, an unreachable registry — every
one of these means the hook does not know whether it is about to let a fabricated number
onto disk. **Not knowing is a denial.**

Two constraints follow, and both are load-bearing:

1. **The diagnostic must be specific enough to fix immediately.** A blanket "validation
   failed" would make a hook bug indistinguishable from a genuine catch, and the fastest
   route past that is to disable the hook. The reason must name the state that could not be
   resolved — which field was missing, which path could not be read, which substitution did
   not apply.

2. **Denial is scoped to watched paths only.** A parse failure must not deny a write to
   `src/` or a settings file. If the payload is so malformed that the path itself cannot be
   read, that is row four and it denies — but a *readable* path outside
   `02-experiments/` and `10-reports/` is row one and allows. Scoping the blast radius is
   what makes a fail-closed control survivable; a hook that can brick every write in the
   repository will be removed the first time it misfires.

### One property that is lost, and the mitigation

`PostToolUse` sees the file exactly as written, including anything the tool layer normalised.
`PreToolUse` sees the intent. If a tool ever transformed content between intent and disk, a
preventive hook would validate the pre-transform version.

For `Write` and `Edit` on Markdown this does not currently happen. The mitigation is to keep
the existing `PostToolUse` hook **in place as a second layer** rather than replacing it:
`PreToolUse` prevents, `PostToolUse` verifies what actually landed. Two hooks, one
guarantee, and the detective layer becomes a check on the preventive one rather than the
only line of defence.

## Decision

Adopt the preventive design, implement it in a separate commit with the tests above, and
retain the `PostToolUse` hook as a verification layer rather than removing it.
