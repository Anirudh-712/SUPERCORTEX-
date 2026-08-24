#!/usr/bin/env python3
"""PreToolUse: a fabricated number never reaches disk (ADR-013).

This is the preventive half of Law 1. It validates the *proposed* content carried in
`tool_input` and returns `permissionDecision: "deny"`, which the client refuses regardless
of what the model decides. The `PostToolUse` hook remains in place as a second layer that
verifies what actually landed.

All validation lives in `supercortex.validation.numbers`. This file is transport: read
stdin, ask, emit a decision.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOK_EVENT = "PreToolUse"


def project_root() -> Path:
    """Claude Code sets CLAUDE_PROJECT_DIR; fall back to the layout for direct runs."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env).resolve() if env else Path(__file__).resolve().parents[2]


def deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": HOOK_EVENT,
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


def looks_watched(payload_text: str) -> bool:
    """Bootstrap-only check, used when the validator itself cannot be imported.

    Deliberately crude and deliberately *not* the real classifier — it exists solely to
    scope a startup failure. Denying every write in the repository because one import
    broke would get the hook removed within a day; denying only the paths that might be
    watched keeps the blast radius survivable while still failing closed where it counts.
    """
    return ".md" in payload_text and (
        "02-experiments/" in payload_text or "10-reports/" in payload_text
    )


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        deny(
            f"hook payload is not valid JSON ({exc}), so it cannot be determined whether "
            f"this write targets a watched path. Fail-closed."
        )
        return

    sys.path.insert(0, str(project_root() / "src"))
    try:
        from supercortex.validation.numbers import decide
    except Exception as exc:  # any import failure must be handled here, not propagated
        if looks_watched(raw):
            deny(
                f"the evidence validator could not be loaded ({type(exc).__name__}: {exc}), "
                f"so a watched write cannot be checked. Fail-closed. Repair "
                f"src/supercortex/validation/numbers.py."
            )
        sys.exit(0)

    if not isinstance(payload, dict):
        deny(
            f"hook payload parsed as {type(payload).__name__}, expected a JSON object, so "
            f"neither the tool nor the target path can be read. Fail-closed."
        )
        return

    decision = decide(
        project_root(),
        payload.get("tool_name") or "",
        payload.get("tool_input"),
    )
    if decision.allow:
        sys.exit(0)
    deny(decision.reason)


if __name__ == "__main__":
    main()
