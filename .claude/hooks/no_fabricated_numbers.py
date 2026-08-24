#!/usr/bin/env python3
"""PostToolUse: verify what actually landed on disk (ADR-013).

The second layer. `no_fabricated_numbers_pre.py` prevents a fabricated number from ever
being written; this one re-reads the file afterwards and confirms that what arrived is what
was approved. `PreToolUse` sees intent, `PostToolUse` sees bytes — if anything ever
transformed content between the two, this is what would notice.

All validation lives in `supercortex.validation.numbers`. Two copies of a rule are two
rules that will eventually disagree, so there is exactly one.

Exit 2 shows stderr to Claude. It does not undo the write — which is precisely why the
preventive hook exists and why this one is not the only line of defence.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def project_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env).resolve() if env else Path(__file__).resolve().parents[2]


def block(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(2)


def looks_watched(payload_text: str) -> bool:
    """Bootstrap-only scope check for when the validator cannot be imported. See the
    preventive hook for why this is deliberately crude rather than the real classifier."""
    return ".md" in payload_text and (
        "02-experiments/" in payload_text or "10-reports/" in payload_text
    )


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        block(f"POST-CHECK FAILED — hook payload is not valid JSON ({exc}), so the target "
              f"path cannot be determined. Fail-closed.")
        return

    if not isinstance(payload, dict):
        block(f"POST-CHECK FAILED — hook payload parsed as {type(payload).__name__}, "
              f"expected a JSON object, so the target path cannot be determined. "
              f"Fail-closed.")
        return

    project = project_root()
    sys.path.insert(0, str(project / "src"))
    try:
        from supercortex.validation.numbers import (
            PathClass,
            classify_path,
            validate_content,
        )
    except Exception as exc:  # any import failure must be handled here, not propagated
        # Classification is impossible without the validator, so the crude scope check is
        # the only tool left. See the preventive hook for why it is deliberately blunt.
        if looks_watched(raw):
            block(f"POST-CHECK FAILED — the evidence validator could not be loaded "
                  f"({type(exc).__name__}: {exc}). Fail-closed.")
        sys.exit(0)

    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        classification, path, why = classify_path(project, tool_input.get("file_path"))
    else:
        classification, path, why = (
            PathClass.UNDETERMINED, None,
            f"tool_input is {type(tool_input).__name__}, expected an object",
        )

    if classification is PathClass.UNDETERMINED:
        block(f"POST-CHECK FAILED — {why}, so it cannot be confirmed whether a watched "
              f"file was just written. Fail-closed.")
        return
    if classification is PathClass.OUTSIDE or path is None:
        sys.exit(0)          # readable and definitely outside: allow immediately, silently

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        block(f"POST-CHECK FAILED — watched file {path.name} cannot be read back after "
              f"the write: {exc}. Fail-closed.")
        return

    decision = validate_content(project, path, content)
    if decision.allow:
        sys.exit(0)
    block(f"BLOCKED — {decision.reason}")


if __name__ == "__main__":
    main()
