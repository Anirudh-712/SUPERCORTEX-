"""The Bash venue guard — written after a real bypass, not from imagination.

On 2026-08-25 an agent (me) reached `fapi.binance.com/fapi/v1/fundingRate` and pulled
eight pages of funding history. `.claude/hooks/guard_bash.py` had a rule intended to stop
exactly that. It did not fire, because the rule was:

    curl[^|;&]*\\b(api|fapi|dapi)\\.(binance|bybit|okx)\\.com

which requires the literal token `curl`, with no `|`, `;` or `&` between it and the host.
The capture used `python urllib.request.urlopen`. The word `curl` never appeared.

That is not a near-miss. It means the guard checked *how* the request was spelled rather
than *where* it was going, and every one of the shapes below walks straight past it. A
control that depends on the caller's choice of HTTP client is documentation, not a
control.

So the guarantee under test is host-based and client-agnostic:

    no Bash command may name a trading-capable venue host, by any spelling.

The negative cases matter just as much. `data.binance.vision` is the desk's checksum-
verified archive source and ordinary research hosts must stay reachable; a guard that
achieves safety by blocking the data pipeline has traded one failure for another.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / ".claude" / "hooks" / "guard_bash.py"


def decision(command: str) -> str | None:
    """The hook's denial reason for `command`, or None when it allows the command."""
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return None
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    # A low-disk denial would make every case below pass for the wrong reason.
    assert not reason.startswith("DISK FLOOR"), reason
    return reason


@pytest.fixture(autouse=True)
def _disk_is_not_the_reason() -> None:
    free_gb = shutil.disk_usage(REPO).free / 1024**3
    if free_gb < 2.0:
        pytest.skip(f"disk floor would mask the result ({free_gb:.2f} GB free)")


# ── the shapes that actually got through ────────────────────────────────────────
# Each of these is a real evasion of the original curl-anchored rule.

EVASIONS = [
    pytest.param(
        'uv run python -c "import urllib.request;'
        ' urllib.request.urlopen(\'https://fapi.binance.com/fapi/v1/fundingRate\')"',
        id="python-urllib-the-actual-bypass",
    ),
    pytest.param(
        "H=fapi.binance.com; curl -sS \"https://$H/fapi/v1/time\"",
        id="host-held-in-a-shell-variable",
    ),
    pytest.param(
        "printf %s https://fapi.binance.com/fapi/v1/time | xargs curl -sS",
        id="host-piped-into-curl-so-it-precedes-the-token",
    ),
    pytest.param(
        "openssl s_client -quiet -connect fapi.binance.com:443",
        id="no-http-client-at-all",
    ),
    pytest.param(
        "wget -qO- https://api.binance.com/api/v3/ping",
        id="wget-instead-of-curl",
    ),
    pytest.param(
        "curl -sS https://eapi.binance.com/eapi/v1/time",
        id="subdomain-outside-the-api-fapi-dapi-grid",
    ),
    pytest.param(
        "curl -sS https://www.binance.com/en/futures/BTCUSDT",
        id="bare-www-host",
    ),
    pytest.param(
        "curl -sS https://api.kraken.com/0/public/Time",
        id="venue-absent-from-the-original-three",
    ),
    pytest.param(
        "curl -sS https://www.deribit.com/api/v2/public/get_time",
        id="deribit",
    ),
    pytest.param(
        "curl -sS https://api.kite.zerodha.com/session/token",
        id="indian-broker-fema-relevant",
    ),
]


@pytest.mark.parametrize("command", EVASIONS)
def test_venue_host_is_denied_however_it_is_spelled(command: str) -> None:
    reason = decision(command)
    assert reason is not None, f"reached a venue host unguarded: {command}"
    assert reason.startswith("VENUE:"), reason


@pytest.mark.parametrize("command", [
    "curl -sS https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT",
    "curl -sS https://api.bybit.com/v5/market/time",
    "curl -sS https://api.okx.com/api/v5/public/time",
])
def test_the_original_curl_shape_is_still_denied(command: str) -> None:
    """The regressions the old rule did catch must keep being caught."""
    reason = decision(command)
    assert reason is not None and reason.startswith("VENUE:"), reason


def test_denial_names_the_archive_alternative() -> None:
    """A refusal that does not say where the data legitimately comes from invites a retry."""
    reason = decision("curl -sS https://api.binance.com/api/v3/ping")
    assert reason is not None
    assert "data.binance.vision" in reason, reason


# ── what must keep working ──────────────────────────────────────────────────────

ALLOWED = [
    pytest.param(
        "curl -sS -o BTCUSDT-1m-2024-01.zip https://data.binance.vision/data/futures/um/"
        "monthly/markPriceKlines/BTCUSDT/1m/BTCUSDT-1m-2024-01.zip",
        id="checksum-verified-archive-download",
    ),
    pytest.param(
        "curl -sS https://data.binance.vision/data/futures/um/monthly/markPriceKlines/"
        "BTCUSDT/1m/BTCUSDT-1m-2024-01.zip.CHECKSUM",
        id="published-checksum",
    ),
    pytest.param("curl -sS https://export.arxiv.org/abs/1503.02531", id="research-source"),
    pytest.param("curl -sS https://api.openalex.org/works", id="literature-api"),
    pytest.param("uv run pytest tests/test_mark_price.py", id="ordinary-command"),
    pytest.param("git status --porcelain", id="git"),
]


@pytest.mark.parametrize("command", ALLOWED)
def test_legitimate_commands_are_not_blocked(command: str) -> None:
    assert decision(command) is None, command


def test_archive_host_is_not_caught_by_the_binance_pattern() -> None:
    """`data.binance.vision` shares a label with `binance.com` and must not be swept up.

    This is the single most costly false positive available: it would silently break the
    only authenticated data source the desk has.
    """
    assert decision("curl -sS https://data.binance.vision/") is None


# ── the loopback gateway rule must survive the rewrite ──────────────────────────

def test_broker_gateway_on_loopback_is_still_denied() -> None:
    reason = decision("curl -sS http://127.0.0.1:5000/api/v1/placeorder")
    assert reason is not None and reason.startswith("BROKER GATEWAY:"), reason


def test_unregistered_backtest_rule_survives() -> None:
    reason = decision('python -c "from nautilus_trader.backtest.node import BacktestNode"')
    assert reason is not None and reason.startswith("UNREGISTERED BACKTEST:"), reason


# ── the three layers must name the same hosts ───────────────────────────────────
# A venue host is blocked three times over: `guard_bash.py` stops it being spelled into a
# Bash command, the WebFetch deny rules stop it reaching the fetch tool, and
# `sandbox.network.deniedDomains` stops the socket opening at all. Three layers only help
# while they agree, and the realistic failure is a venue added to one list and forgotten
# in the others. `guard_bash.py` is the source of truth; nothing below re-types its list.


def _venue_domains() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location("_guard_bash_under_test", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.VENUE_DOMAINS


SETTINGS = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
VENUE_DOMAINS = _venue_domains()
#: Every venue entry both config layers must carry, bare and wildcard.
VENUE_ENTRIES = set(VENUE_DOMAINS) | {f"*.{d}" for d in VENUE_DOMAINS}
ARCHIVE_HOST = "data.binance.vision"
#: Denied to WebFetch but deliberately absent from the sandbox list — see
#: `test_loopback_webfetch_denies_are_preserved`.
LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}


def test_sandbox_denies_exactly_the_venue_entries() -> None:
    assert set(SETTINGS["sandbox"]["network"]["deniedDomains"]) == VENUE_ENTRIES


def test_webfetch_denies_exactly_the_venue_entries_and_loopback() -> None:
    """Equality, not containment: a bare rule without its wildcard leaves every subdomain
    of a venue reachable, and an unexplained extra rule means the list has drifted."""
    denied = {
        rule.removeprefix("WebFetch(domain:").removesuffix(")")
        for rule in SETTINGS["permissions"]["deny"]
        if rule.startswith("WebFetch(domain:")
    }
    assert denied == VENUE_ENTRIES | LOOPBACK_HOSTS


def test_archive_host_survives_every_layer() -> None:
    """The one false positive that would silently break the desk's only data source."""
    network = SETTINGS["sandbox"]["network"]
    assert ARCHIVE_HOST in network["allowedDomains"]
    assert ARCHIVE_HOST not in network["deniedDomains"]
    assert f"WebFetch(domain:{ARCHIVE_HOST})" not in SETTINGS["permissions"]["deny"]
    # Outside every venue family, so no wildcard added later can sweep it up either.
    assert not any(d == ARCHIVE_HOST or ARCHIVE_HOST.endswith(f".{d}") for d in VENUE_DOMAINS)


def test_no_venue_host_is_allowed_and_denied_at_once() -> None:
    """Deny is evaluated before allow, so an overlap is a silent contradiction in review."""
    assert not set(SETTINGS["sandbox"]["network"]["allowedDomains"]) & VENUE_ENTRIES


def test_loopback_webfetch_denies_are_preserved() -> None:
    """Loopback stays a WebFetch deny and stays out of the sandbox list: the gateway rule
    in `guard_bash.py` is port-specific (:5000/:8765) so local tooling keeps working."""
    deny = set(SETTINGS["permissions"]["deny"])
    for host in LOOPBACK_HOSTS:
        assert f"WebFetch(domain:{host})" in deny, host
        assert host not in SETTINGS["sandbox"]["network"]["deniedDomains"], host
