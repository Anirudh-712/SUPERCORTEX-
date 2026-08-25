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

#: Registrable domains of trading-capable venues and brokers. Anything at or below one of
#: these — `binance.com`, `fapi.binance.com`, `api.kite.zerodha.com` — is off-limits.
#:
#: `data.binance.vision` is deliberately absent and must stay absent: it is the desk's
#: checksum-verified archive source, it shares only a label with `binance.com`, and the
#: matcher below keys on the full registrable domain so the two never collide.
#:
#: The Indian broker domains are here for FEMA reasons as much as security ones. See the
#: jurisdiction section of CLAUDE.md.
VENUE_DOMAINS = (
    "binance.com", "binance.us", "bybit.com", "okx.com", "deribit.com", "kraken.com",
    "coinbase.com", "bitmex.com", "kucoin.com", "gate.io", "mexc.com", "htx.com",
    "bitget.com",
    "zerodha.com", "dhan.co", "angelone.in", "upstox.com", "fyers.in",
)

# Host-based, not client-based. The rule this replaces was anchored on the literal token
# `curl`, so `python urllib.request.urlopen(...)` walked past it and pulled eight pages of
# funding history on 2026-08-25. A guard that inspects *how* a request is spelled rather
# than *where* it goes is documentation, not a control: there are unbounded ways to spell
# an HTTP call and a bounded number of venues. So this matches the host wherever it
# appears in the command, whatever surrounds it.
#
# The consequence is deliberate and worth stating: a command that merely *mentions* one of
# these hosts — an echo, a grep, a heredoc — is denied too. That is the fail-closed side of
# the trade, and it is the right side. See tests/test_venue_guard.py.
_VENUE = re.compile(
    r"(?:^|[^\w.-])(?:[\w-]+\.)*(?:"
    + "|".join(d.replace(".", r"\.") for d in VENUE_DOMAINS)
    + r")(?![\w-])",
    re.I,
)

RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsecurity\s+find-(generic|internet)-password\b"),
     "CREDENTIALS: the macOS Keychain is off-limits to agents. Live keys (T3) are read only "
     "by the live process, which the human operator starts."),
    (re.compile(r"\bop\s+(read|item|signin)\b"),
     "CREDENTIALS: 1Password CLI is off-limits to agents."),
    (re.compile(r"(cat|less|head|tail|bat|open|cp|mv)\s+[^|;&]*\.env"),
     ("CREDENTIALS: .env files are off-limits. Config lives in config.yaml; "
      "secrets live in the Keychain.")),
    (_VENUE,
     "VENUE: trading-capable venue and broker hosts are unreachable from an agent by "
     "design, whichever HTTP client is used. Public history comes from "
     "data.binance.vision, which stays open. A venue API capture that the desk genuinely "
     "needs is performed by the human operator and handed over as raw bytes."),
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
