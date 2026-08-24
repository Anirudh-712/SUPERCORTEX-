#!/usr/bin/env python3
"""PostToolUse validator: no result number may exist that code did not produce.

Law 1 of CLAUDE.md, enforced mechanically. For any Markdown file written under
02-experiments/ or 10-reports/, every *result-shaped* number in the prose must be
traceable to a JSON artifact produced by deterministic code.

Result-shaped means: has a decimal point, or is an integer of >= 4 digits.
Structural numbers (list markers, small counts, years, dates, IDs) are ignored.

Exit 2 => Claude is shown stderr and told to fix it. This is the point.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
WATCHED = ("02-experiments/", "10-reports/")

# Numbers that are structure, not evidence.
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
ID_RE = re.compile(r"\b[A-Z]{3,4}-\d{6}\b")
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b")
URL_RE = re.compile(r"https?://\S+")
VERSION_RE = re.compile(r"\bv?\d+\.\d+\.\d+\b")

NUMBER_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w])")


def strip_exempt(text: str) -> str:
    for pattern in (FRONTMATTER_RE, FENCE_RE, COMMENT_RE, INLINE_CODE_RE, URL_RE,
                    ID_RE, ISO_DATE_RE, VERSION_RE):
        text = pattern.sub(" ", text)
    return text


def is_result_shaped(token: str) -> bool:
    cleaned = token.replace(",", "").lstrip("-")
    if "." in cleaned:
        return True
    if cleaned.isdigit():
        if 1900 <= int(cleaned) <= 2100:   # a year
            return False
        return len(cleaned) >= 4
    return False


def harvest(obj, sink: set[float]) -> None:
    """Recursively collect every number appearing anywhere in a JSON artifact."""
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
                pass


def supported(value: float, known: set[float]) -> bool:
    """A number is supported if it matches a known value, or is a rounding of one."""
    for candidate in known:
        if abs(candidate - value) <= 1e-9:
            return True
        for places in range(0, 7):
            if abs(round(candidate, places) - value) <= 1e-9:
                return True
        if candidate and abs(round(candidate * 100, 4) - value) <= 1e-9:
            return True  # fraction reported as a percentage
        if candidate and abs(round(candidate * 10_000, 4) - value) <= 1e-9:
            return True  # fraction reported in basis points
    return False


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    raw_path = (payload.get("tool_input") or {}).get("file_path")
    if not raw_path:
        sys.exit(0)

    path = Path(raw_path).resolve()
    if path.suffix != ".md" or not path.exists():
        sys.exit(0)

    try:
        rel = path.relative_to(PROJECT).as_posix()
    except ValueError:
        sys.exit(0)
    if not rel.startswith(WATCHED):
        sys.exit(0)

    text = strip_exempt(path.read_text(encoding="utf-8", errors="replace"))
    claimed: dict[float, str] = {}
    for token in NUMBER_RE.findall(text):
        if is_result_shaped(token):
            try:
                claimed[float(token.replace(",", ""))] = token
            except ValueError:
                pass
    if not claimed:
        sys.exit(0)

    # Gather every JSON artifact in scope: this experiment's artifacts/, plus any
    # JSON sitting beside the document.
    known: set[float] = set()
    sources: list[str] = []
    search_dirs = [path.parent, path.parent / "artifacts", PROJECT / "data" / "artifacts"]
    if rel.startswith("02-experiments/"):
        exp_root = PROJECT / "02-experiments" / rel.split("/")[1]
        search_dirs += [exp_root, exp_root / "artifacts"]

    seen: set[Path] = set()
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for json_path in sorted(directory.glob("*.json")):
            if json_path in seen:
                continue
            seen.add(json_path)
            try:
                harvest(json.loads(json_path.read_text(encoding="utf-8")), known)
                sources.append(json_path.relative_to(PROJECT).as_posix())
            except Exception:
                continue

    unsupported = {v: t for v, t in claimed.items() if not supported(v, known)}
    if not unsupported:
        sys.exit(0)

    shown = ", ".join(sorted(unsupported.values(), key=len)[:12])
    where = ", ".join(sources) if sources else "NONE FOUND"
    print(
        f"BLOCKED — fabricated numbers in {rel}\n\n"
        f"These values do not appear in any JSON artifact: {shown}\n"
        f"Artifacts searched: {where}\n\n"
        "Law 1: never write a number you did not read from a file produced by code.\n"
        "Fix by one of:\n"
        "  - run the experiment so metrics.json exists, then quote its values exactly\n"
        "  - move the number inside a ``` code fence if it is illustrative, not a result\n"
        "  - delete the claim and link to the artifact path instead",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
