#!/usr/bin/env python3
"""PreToolUse guard for Bash. The rules that must hold even if the model is wrong.

Blocks:
  1. credential access          (Keychain, 1Password, .env, private venue endpoints)
  2. live-trading entrypoints   (only the human starts live)
  3. backtests that bypass the registry  (protects the trial count -> DSR)
  4. destructive deletes of the data lake or registry
  5. any command when free disk < FLOOR_GB
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

FLOOR_GB = 2.0
PROJECT = Path(__file__).resolve().parents[2]

RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsecurity\s+find-(generic|internet)-password\b"),
     "CREDENTIALS: the macOS Keychain is off-limits to agents. Live keys (T3) are read only "
     "by the live process, which the human operator starts."),
    (re.compile(r"\bop\s+(read|item|signin)\b"),
     "CREDENTIALS: 1Password CLI is off-limits to agents."),
    (re.compile(r"(cat|less|head|tail|bat|open|cp|mv)\s+[^|;&]*\.env"),
     ("CREDENTIALS: .env files are off-limits. Config lives in config.yaml; "
      "secrets live in the Keychain.")),
    (re.compile(r"curl[^|;&]*\b(api|fapi|dapi)\.(binance|bybit|okx)\.com"),
     "VENUE: authenticated venue endpoints are unreachable from an agent by design. "
     "Public history comes from data.binance.vision."),
    # ADR-011: the broker gateway holds T3 credentials and listens on loopback. A network
    # allowlist that only considers external hosts would leave this wide open.
    (re.compile(r"(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\]):(5000|8765)\b"),
     "BROKER GATEWAY: 127.0.0.1:5000 / :8765 is the OpenAlgo broker gateway. It holds live "
     "broker credentials (T3), so it is off-limits to agents even though it is loopback. "
     "See 20-decisions/ADR-011."),
    (re.compile(r"\bsupercortex\s+live\b|\blive_trading\b|--live\b"),
     "LIVE TRADING: only the human operator starts a live process. See section 25 of the report."),
    (re.compile(r"python[^|;&]*\b(nautilus_trader|BacktestEngine|BacktestNode)\b"),
     "UNREGISTERED BACKTEST: every run must go through `supercortex exp run`, which increments "
     "the trial counter atomically. An unregistered run silently invalidates the Deflated Sharpe "
     "Ratio for this experiment. This is law 2."),
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*[^|;&]*(data/raw|registry\.sqlite|\.git\b)"),
     "DESTRUCTIVE: data/raw is immutable, registry.sqlite is the audit trail, .git is the history. "
     "None may be deleted by an agent."),
    (re.compile(r"\bsqlite3\b[^|;&]*registry\.sqlite[^|;&]*\b(DELETE|UPDATE|DROP)\b", re.I),
     "APPEND-ONLY: the trial table takes no DELETE and no UPDATE. Rewriting trial history is how "
     "a Deflated Sharpe Ratio becomes fiction."),
]


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
        sys.exit(0)

    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command:
        sys.exit(0)

    free_gb = shutil.disk_usage(PROJECT).free / 1024**3
    if free_gb < FLOOR_GB:
        deny(f"DISK FLOOR: {free_gb:.2f} GB free (floor {FLOOR_GB} GB). Free space first.")

    for pattern, reason in RULES:
        if pattern.search(command):
            deny(reason)

    sys.exit(0)


if __name__ == "__main__":
    main()
