"""Shared evidence validation for the fabricated-number hooks (ADR-013).

Law 1 of CLAUDE.md, in one place. Both the preventive `PreToolUse` hook and the verifying
`PostToolUse` hook import from here — the logic is never duplicated, because two copies of
a rule are two rules that will eventually disagree.

**Stdlib only.** Hooks execute under `#!/usr/bin/env python3`, which is the system
interpreter, not the project virtualenv. Importing anything third-party here would make the
hook fail to start — and a validator that cannot start is a validator that is not running.

The decision model, from ADR-013. Four states, two of which allow:

    OUTSIDE      readable path, not under a watched directory        -> allow
    WATCHED      content reconstructed, every number has provenance  -> allow
    WATCHED      content reconstructed, a number does not            -> deny, naming values
    UNDETERMINED path unknown, or content unreadable/unverifiable    -> deny, naming why

Denial is scoped to watched paths. A hook that can block every write in the repository
gets removed the first time it misfires, so the blast radius is deliberately small.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

WATCHED_PREFIXES = ("02-experiments/", "10-reports/")
WATCHED_SUFFIX = ".md"
REGISTRY_NAME = "registry.sqlite"

# ── exemptions: text that is structure, not evidence ────────────────────────────
FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
URL_RE = re.compile(r"https?://\S+")
ID_RE = re.compile(r"\b[A-Z]{3,4}-\d{6}\b")
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b")
VERSION_RE = re.compile(r"\bv?\d+\.\d+\.\d+\b")

#: Order matters: frontmatter and fences are stripped before inline code, so a backtick
#: inside a fenced block cannot terminate an inline span and leak the rest of the document.
EXEMPTIONS = (
    FRONTMATTER_RE, FENCE_RE, COMMENT_RE, INLINE_CODE_RE,
    URL_RE, ID_RE, ISO_DATE_RE, VERSION_RE,
)

NUMBER_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w])")


class PathClass(Enum):
    OUTSIDE = "outside"
    WATCHED = "watched"
    UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class Decision:
    allow: bool
    classification: PathClass = PathClass.OUTSIDE
    reason: str = ""
    unsupported: tuple[str, ...] = ()
    sources: tuple[str, ...] = field(default_factory=tuple)


def _allow(classification: PathClass = PathClass.OUTSIDE) -> Decision:
    return Decision(True, classification)


def _deny(classification: PathClass, reason: str, *,
          unsupported: tuple[str, ...] = (), sources: tuple[str, ...] = ()) -> Decision:
    return Decision(False, classification, reason, unsupported, sources)


# ── path classification ─────────────────────────────────────────────────────────
def classify_path(project: Path, raw_path: object) -> tuple[PathClass, Path | None, str]:
    """Resolve a payload's `file_path` into one of three classes.

    Returns the class, the resolved path when there is one, and a diagnostic naming what
    could not be determined. The diagnostic must be specific: a blanket message makes a
    hook bug indistinguishable from a genuine catch.
    """
    if raw_path is None:
        return PathClass.UNDETERMINED, None, "tool_input has no file_path field"
    if not isinstance(raw_path, str):
        return (PathClass.UNDETERMINED, None,
                f"tool_input.file_path is {type(raw_path).__name__}, expected str")
    if not raw_path.strip():
        return PathClass.UNDETERMINED, None, "tool_input.file_path is empty"

    try:
        path = Path(raw_path).resolve()
    except (OSError, ValueError) as exc:
        return (PathClass.UNDETERMINED, None,
                f"tool_input.file_path could not be resolved: {exc}")

    try:
        rel = path.relative_to(project.resolve()).as_posix()
    except ValueError:
        return PathClass.OUTSIDE, path, ""       # outside the repository entirely

    if path.suffix != WATCHED_SUFFIX or not rel.startswith(WATCHED_PREFIXES):
        return PathClass.OUTSIDE, path, ""
    return PathClass.WATCHED, path, ""


# ── number extraction ───────────────────────────────────────────────────────────
def strip_exempt(text: str) -> str:
    for pattern in EXEMPTIONS:
        text = pattern.sub(" ", text)
    return text


def is_result_shaped(token: str) -> bool:
    """Does this token look like evidence rather than structure?

    Anything with a decimal point, plus integers of four digits or more. Years are
    excluded — a four-digit number in 1900..2100 is far more often a date than a result.
    """
    cleaned = token.replace(",", "").lstrip("-")
    if "." in cleaned:
        return True
    if cleaned.isdigit():
        if 1900 <= int(cleaned) <= 2100:
            return False
        return len(cleaned) >= 4
    return False


def extract_claimed(text: str) -> dict[float, str]:
    """Every result-shaped number in the prose, keyed by value, showing it as written."""
    claimed: dict[float, str] = {}
    for token in NUMBER_RE.findall(strip_exempt(text)):
        if is_result_shaped(token):
            try:
                claimed[float(token.replace(",", ""))] = token
            except ValueError:
                continue
    return claimed


def harvest(obj: object, sink: set[float]) -> None:
    """Every number appearing anywhere in a JSON artifact, at any depth."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        sink.add(float(obj))
    elif isinstance(obj, dict):
        for value in obj.values():
            harvest(value, sink)
    elif isinstance(obj, list):
        for value in obj:
            harvest(value, sink)
    elif isinstance(obj, str):
        for match in NUMBER_RE.findall(obj):
            try:
                sink.add(float(match.replace(",", "")))
            except ValueError:
                continue


def supported(value: float, known: set[float]) -> bool:
    """Does this value derive from something code produced?

    Exact matches, roundings to any sensible precision, and the two presentation changes
    that are the same evidence rather than a new claim: a fraction shown as a percentage,
    and a fraction shown in basis points.
    """
    for candidate in known:
        if abs(candidate - value) <= 1e-9:
            return True
        for places in range(0, 7):
            if abs(round(candidate, places) - value) <= 1e-9:
                return True
        if candidate and abs(round(candidate * 100, 4) - value) <= 1e-9:
            return True
        if candidate and abs(round(candidate * 10_000, 4) - value) <= 1e-9:
            return True
    return False


# ── provenance ──────────────────────────────────────────────────────────────────
class RegistryUnavailable(RuntimeError):
    """The registry could not be consulted, so provenance cannot be established."""


def trusted_artifacts(project: Path, rel: str) -> set[Path]:
    """Artifacts a sealed, successful trial produced. Nothing else.

    Provenance, not proximity. A JSON file that happens to sit in the artifacts directory
    — including one an agent wrote itself, and including the headline metrics.json — is
    not evidence unless a successful registered trial claims it by path.

    Raises `RegistryUnavailable` rather than returning an empty set: an empty set would
    make "no artifacts exist" indistinguishable from "the registry could not be read",
    and the second must deny.
    """
    registry = project / REGISTRY_NAME
    if not registry.exists():
        raise RegistryUnavailable(f"{REGISTRY_NAME} not found at {registry}")

    trusted: set[Path] = set()
    try:
        conn = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise RegistryUnavailable(f"{REGISTRY_NAME} could not be opened: {exc}") from exc

    try:
        if rel.startswith("02-experiments/"):
            parts = rel.split("/")[1].split("-")
            experiment_id = f"{parts[0]}-{parts[1]}" if len(parts) >= 2 else ""
            rows = conn.execute(
                "SELECT metrics_path FROM trial WHERE experiment_id = ? "
                "AND metrics_path IS NOT NULL AND outcome = 'ok'",
                (experiment_id,),
            ).fetchall()
            # artifacts/metrics.json is DELIBERATELY NOT trusted. It is a convenience copy
            # of one sealed trial's artifact, so it carries no evidence of its own — but it
            # is not itself registered, which made "the file exists" the only thing standing
            # between an edit to it and an authorised number. Anyone who could write that
            # file could launder any value. Its contents still validate, via the registered
            # per-trial artifact they were copied from.
        else:
            rows = conn.execute(
                "SELECT metrics_path FROM trial WHERE metrics_path IS NOT NULL "
                "AND outcome = 'ok'"
            ).fetchall()
        for row in rows:
            candidate = project / row["metrics_path"]
            if candidate.exists():
                trusted.add(candidate)
    except sqlite3.Error as exc:
        raise RegistryUnavailable(f"{REGISTRY_NAME} query failed: {exc}") from exc
    finally:
        conn.close()
    return trusted


def known_values(artifacts: set[Path], project: Path) -> tuple[set[float], list[str]]:
    known: set[float] = set()
    sources: list[str] = []
    for artifact in sorted(artifacts):
        try:
            harvest(json.loads(artifact.read_text(encoding="utf-8")), known)
        except (OSError, ValueError):
            continue
        try:
            sources.append(artifact.relative_to(project).as_posix())
        except ValueError:
            sources.append(str(artifact))
    return known, sources


# ── edit reconstruction ─────────────────────────────────────────────────────────
class EditImpossible(RuntimeError):
    """The prospective document cannot be built from this payload."""


def reconstruct_edit(current: str, old: str, new: str, replace_all: bool) -> str:
    """Apply an Edit in memory. Never writes.

    A fragment is not a document: validating `new_string` alone would both miss a
    fabrication elsewhere in the file and false-positive on one whose supporting context
    lives outside the replaced region. So the whole prospective file is built and checked.
    """
    if not old:
        raise EditImpossible("old_string is empty, so the substitution is not defined")
    occurrences = current.count(old)
    if occurrences == 0:
        raise EditImpossible("old_string does not occur in the target file")
    if occurrences > 1 and not replace_all:
        raise EditImpossible(
            f"old_string is ambiguous: it occurs {occurrences} times and replace_all is "
            f"not set, so the prospective document is not knowable"
        )
    return current.replace(old, new) if replace_all else current.replace(old, new, 1)


# ── the decision ────────────────────────────────────────────────────────────────
def validate_content(project: Path, path: Path, content: str) -> Decision:
    """Check a complete prospective document against registry-vouched evidence."""
    rel = path.relative_to(project.resolve()).as_posix()

    claimed = extract_claimed(content)
    if not claimed:
        # Nothing evidence-shaped in the prose, so there is nothing to fabricate. Consulting
        # the registry here would deny ordinary writing whenever it happened to be locked.
        return _allow(PathClass.WATCHED)

    try:
        artifacts = trusted_artifacts(project, rel)
    except RegistryUnavailable as exc:
        return _deny(
            PathClass.UNDETERMINED,
            f"cannot establish provenance for {rel}: {exc}. Fail-closed: not knowing "
            f"whether a number is evidence is a denial.",
        )

    known, sources = known_values(artifacts, project)
    unsupported = {value: token for value, token in claimed.items()
                   if not supported(value, known)}
    if not unsupported:
        return _allow(PathClass.WATCHED)

    shown = tuple(sorted(unsupported.values(), key=lambda t: (len(t), t))[:12])
    where = ", ".join(sources) if sources else "none — no sealed trial has produced one"
    return _deny(
        PathClass.WATCHED,
        f"fabricated numbers in {rel}: {', '.join(shown)} do not appear in any artifact "
        f"a sealed trial produced. Artifacts consulted: {where}. Law 1 — never write a "
        f"number you did not read from a file produced by code. Fix by running the "
        f"experiment and quoting its values, moving the figure inside a code fence if it "
        f"is illustrative, or linking to the artifact instead.",
        unsupported=shown,
        sources=tuple(sources),
    )


def decide(project: Path, tool_name: str, tool_input: object) -> Decision:
    """The whole contract, for both hooks.

    `tool_input` is deliberately typed loosely: it arrives from JSON and may be anything.
    """
    if not isinstance(tool_input, dict):
        return _deny(PathClass.UNDETERMINED,
                     f"tool_input is {type(tool_input).__name__}, expected an object")

    classification, path, why = classify_path(project, tool_input.get("file_path"))
    if classification is PathClass.UNDETERMINED:
        return _deny(classification,
                     f"{why}, so it cannot be determined whether this write is watched. "
                     f"Fail-closed.")
    if classification is PathClass.OUTSIDE or path is None:
        return _allow(PathClass.OUTSIDE)

    if tool_name == "Write":
        content = tool_input.get("content")
        if not isinstance(content, str):
            return _deny(PathClass.UNDETERMINED,
                         f"watched Write to {path.name} has no readable content field "
                         f"(got {type(content).__name__})")
        return validate_content(project, path, content)

    if tool_name == "Edit":
        old = tool_input.get("old_string")
        new = tool_input.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return _deny(PathClass.UNDETERMINED,
                         f"watched Edit to {path.name} is missing a string old_string or "
                         f"new_string")
        try:
            current = path.read_text(encoding="utf-8")
        except OSError as exc:
            return _deny(PathClass.UNDETERMINED,
                         f"watched Edit target {path.name} cannot be read: {exc}")
        try:
            merged = reconstruct_edit(current, old, new, bool(tool_input.get("replace_all")))
        except EditImpossible as exc:
            return _deny(PathClass.UNDETERMINED,
                         f"watched Edit to {path.name} cannot be reconstructed: {exc}")
        return validate_content(project, path, merged)

    # An unrecognised tool reaching a watched Markdown path is not a state this hook
    # understands, and understanding is the precondition for allowing.
    return _deny(PathClass.UNDETERMINED,
                 f"unrecognised tool {tool_name!r} targeting watched path {path.name}")
