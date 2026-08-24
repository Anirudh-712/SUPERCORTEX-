#!/usr/bin/env python3
"""PreToolUse guard for Write|Edit.

Blocks, regardless of what the model decides:
  1. any write to 06-risk/limits.yaml            (CIO-only file)
  2. any edit to a file marked <!-- GENERATED     (machine-owned)
  3. any write under data/raw/                    (L0 is immutable)
  4. any write at all when free disk < FLOOR_GB   (prevents mid-write corruption)

Contract: stdin = hook JSON, stdout = permissionDecision JSON, exit 0.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

FLOOR_GB = 2.0
PROJECT = Path(__file__).resolve().parents[2]


def deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # never break the session on a malformed payload

    tool_input = payload.get("tool_input") or {}
    raw_path = tool_input.get("file_path")
    if not raw_path:
        sys.exit(0)

    path = Path(raw_path).resolve()

    # --- 4. disk floor (checked first: it applies to every write) -------------
    free_gb = shutil.disk_usage(PROJECT).free / 1024**3
    if free_gb < FLOOR_GB:
        deny(
            f"DISK FLOOR: only {free_gb:.2f} GB free (floor {FLOOR_GB} GB). "
            "Writing now risks corrupting a Parquet file or registry.sqlite mid-write. "
            "Free space before continuing: `du -sh ~/.npm ~/.cache`."
        )

    try:
        rel = path.relative_to(PROJECT)
    except ValueError:
        sys.exit(0)  # outside the project; other deny rules cover it
    rel_posix = rel.as_posix()

    # --- 1. risk limits ------------------------------------------------------
    if rel_posix == "06-risk/limits.yaml":
        deny(
            "RISK LIMITS ARE CIO-ONLY. Agents may propose a diff in a DEC-*.md note, "
            "but only the human operator commits changes to 06-risk/limits.yaml."
        )

    # --- 3. immutable raw layer ---------------------------------------------
    if rel_posix.startswith("data/raw/"):
        deny(
            "L0 IS IMMUTABLE. data/raw/ holds exactly what the venue served and is never "
            "edited. Rebuild the canonical layer instead: `supercortex data rebuild`."
        )

    # --- 2. generated files --------------------------------------------------
    if path.exists() and path.suffix == ".md":
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:200]
        except OSError:
            head = ""
        if "<!-- GENERATED" in head:
            deny(
                f"{rel_posix} is GENERATED from registry.sqlite and will be overwritten. "
                "Edit the source of truth (the registry, or the prose file beside it) instead."
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
