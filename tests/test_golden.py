"""The golden backtest: freezing what a backtest MEANS.

Architecture report §27 called this the test that matters most and that nobody writes.
Without it the engine, bar construction, positions parsing, cost model or tax arithmetic can
drift, and every historical result silently changes meaning with no failure anywhere.

Structure is asserted by hand. Exact fill prices are frozen as an engine-derived baseline —
they come from Nautilus's synthetic-spread model, and the point of freezing them is to
notice when they change, not to claim they were predicted.
"""
from __future__ import annotations

import pytest

from supercortex.backtest.golden import run_golden, sawtooth
from supercortex.backtest.runner import BacktestSpec, run

HOUR_MS = 3_600_000


# ── the synthetic series is arithmetic ──────────────────────────────────────────
def test_sawtooth_is_hand_computable():
    f = sawtooth(n_bars=240, low=100.0, high=140.0, period=40)
    assert f.height == 240
    assert f["open"][0] == 100.0          # phase 0
    assert f["open"][10] == 120.0         # rising: 100 + 2*10
    assert f["open"][20] == 140.0         # peak
    assert f["open"][30] == 120.0         # falling: 140 - 2*10
    assert f["open"][40] == 100.0         # trough, one full period later


def test_sawtooth_bars_are_contiguous():
    """Bar i closes where bar i+1 opens. No gaps to argue about."""
    f = sawtooth()
    closes = f["close"].to_list()[:-1]
    opens = f["open"].to_list()[1:]
    assert closes == opens


def test_sawtooth_ohlc_is_internally_valid():
    f = sawtooth()
    assert (f["high"] >= f["low"]).all()
    assert (f["high"] >= f["open"]).all()
    assert (f["high"] >= f["close"]).all()
    assert (f["low"] <= f["open"]).all()
    assert (f["low"] <= f["close"]).all()


# ── structure, verified by hand ─────────────────────────────────────────────────
def test_trade_structure_is_as_designed():
    """4 round trips, entering and exiting on the pre-declared bar phases."""
    result = run_golden()
    assert result.n_trades == 4
    assert result.reconciliation_diff == 0.0


def test_trades_are_symmetric_by_construction():
    """The sawtooth is symmetric and the entry phases alternate rising/falling, so the
    four trades must pair into two equal-and-opposite magnitudes."""
    f = sawtooth()
    trades, _ = run(BacktestSpec(
        strategy="supercortex.backtest.golden:GoldenStrategy", interval="1h",
        starting_balance=1_000_000.0,
        params={"entry_bar": 10, "exit_bar": 30, "quantity": 1.0, "repeat_every": 60},
        synthetic_bars=f,
    ))
    pnls = sorted(round(t.gross_pnl, 6) for t in trades)
    assert pnls[0] == pnls[1]
    assert pnls[2] == pnls[3]
    assert pnls[0] < 0 < pnls[2]


# ── frozen baseline ─────────────────────────────────────────────────────────────
#: Engine-derived, not predicted. Any change to the engine, bar construction, positions
#: parsing, cost model or tax arithmetic moves these and must be explained before the
#: numbers are updated.
GOLDEN = {
    "n_trades": 4,
    "gross_pnl": -0.6,
    "net_of_costs": -1.416,
    "net_after_tax": -6.65019,
    "profit_factor": 0.96319,
    "turnover": 960.0,
    "reconciliation_diff": 0.0,
}


@pytest.mark.parametrize("field", sorted(GOLDEN))
def test_golden_values_are_unchanged(field):
    actual = getattr(run_golden(), field)
    assert actual == pytest.approx(GOLDEN[field], abs=1e-6), (
        f"golden backtest moved: {field} was {GOLDEN[field]}, now {actual}. "
        "Something changed the meaning of a backtest. Explain it before updating."
    )


# ── engine characterisation: the latency model quantises to bars ────────────────
@pytest.mark.parametrize(
    ("latency_ms", "expected_fill_bar"),
    [(0, 11), (1, 12), (250, 12), (1000, 12)],
)
def test_any_nonzero_latency_costs_a_whole_bar(latency_ms, expected_fill_bar):
    """Measured, and it is not what the parameter name suggests.

    Signal is taken on bar 10's close. With zero latency the fill lands on bar 11 — the
    correct "act on close, fill at next open". With ONE MILLISECOND of latency it lands on
    bar 12, identically to 1000 ms. The latency model quantises to bar boundaries, so at
    hourly granularity `latency_ms` is effectively a boolean: zero, or one extra bar.

    Consequence: a `latency_ms: 250` config does not model 250 ms. It models one extra hour.
    Any strategy's measured edge depends on which of these it got.
    """
    f = sawtooth()
    t0 = f["ts_ms"][0]
    trades, _ = run(BacktestSpec(
        strategy="supercortex.backtest.golden:GoldenStrategy", interval="1h",
        starting_balance=1_000_000.0, latency_ms=latency_ms,
        params={"entry_bar": 10, "exit_bar": 30, "quantity": 1.0, "repeat_every": 60},
        synthetic_bars=f,
    ))
    assert trades
    fill_bar = (trades[0].entry_ts_ms - t0) // HOUR_MS
    assert fill_bar == expected_fill_bar


def test_fills_are_adverse_in_both_directions():
    """Nautilus synthesises a spread around the bar, so a buy fills above the open and a
    sell below it. Conservative, which is what we want, but it must be known and frozen."""
    f = sawtooth()
    t0 = f["ts_ms"][0]
    trades, _ = run(BacktestSpec(
        strategy="supercortex.backtest.golden:GoldenStrategy", interval="1h",
        starting_balance=1_000_000.0,
        params={"entry_bar": 10, "exit_bar": 30, "quantity": 1.0, "repeat_every": 60},
        synthetic_bars=f,
    ))
    for t in trades:
        entry_bar = (t.entry_ts_ms - t0) // HOUR_MS
        exit_bar = (t.exit_ts_ms - t0) // HOUR_MS
        assert t.entry_price > f["open"][entry_bar]   # bought above the open
        assert t.exit_price < f["open"][exit_bar]     # sold below it
