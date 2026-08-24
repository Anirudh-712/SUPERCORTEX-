"""The golden fixture: a deterministic reference run with a hand-computable answer.

Architecture report §27 called this "the test that matters most and nobody writes", then it
was skipped, and a research hypothesis was invented to exercise the engine instead. This is
the correction. See [[DEC-000002]].

What it is for: freezing the *meaning* of a backtest. Any change to the engine, the bar
construction, the positions-report parsing, the cost model or the tax arithmetic that alters
the result will fail loudly here. Without it, a backtest silently changes meaning over months
and no historical result can be trusted.

Deliberate properties:

* **Synthetic prices.** A closed-form sawtooth, not market data. The expected answer is
  arithmetic, not a recording of whatever the engine happened to produce — a golden test
  seeded from its own output only freezes the bug.
* **A trivial strategy.** Enter on a fixed bar index, exit on another. No signal, no state,
  nothing to argue about.
* **Not a registered trial.** This is a test of the machine, not a look at market data, so
  it must never touch the registry or consume trial budget.
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

HOUR_MS = 3_600_000
EPOCH_START = 1_600_000_000_000  # a fixed, arbitrary instant; nothing depends on the date


def sawtooth(n_bars: int = 240, low: float = 100.0, high: float = 140.0,
             period: int = 40) -> pl.DataFrame:
    """A deterministic triangle wave. Every price is computable by hand.

    Rises linearly from `low` to `high` over half a period, falls back over the other half,
    and repeats. Bar `i` has open = close of bar `i-1`, so there are no gaps to argue about.
    """
    half = period // 2
    step = (high - low) / half
    closes: list[float] = []
    for i in range(n_bars + 1):
        phase = i % period
        value = low + step * phase if phase < half else high - step * (phase - half)
        closes.append(round(value, 1))

    rows = []
    for i in range(n_bars):
        o, c = closes[i], closes[i + 1]
        rows.append({
            "ts_ms": EPOCH_START + i * HOUR_MS,
            "open": o,
            "high": round(max(o, c), 1),
            "low": round(min(o, c), 1),
            "close": c,
            "volume": 1.0,
        })
    return pl.DataFrame(rows)


class GoldenStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    entry_bar: int = 10
    exit_bar: int = 30
    quantity: float = 1.0
    repeat_every: int = 60


class GoldenStrategy(Strategy):
    """Enters and exits on fixed bar indices. No signal, no state worth debating."""

    def __init__(self, config: GoldenStrategyConfig) -> None:
        super().__init__(config)
        self._seen = 0

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        self.subscribe_bars(self.config.bar_type)

    def on_bar(self, bar: Bar) -> None:
        phase = self._seen % self.config.repeat_every
        self._seen += 1
        if phase == self.config.entry_bar:
            self.submit_order(
                self.order_factory.market(
                    instrument_id=self.config.instrument_id,
                    order_side=OrderSide.BUY,
                    quantity=self.instrument.make_qty(self.config.quantity),
                )
            )
        elif phase == self.config.exit_bar:
            self.close_all_positions(self.config.instrument_id)


@dataclass(frozen=True)
class GoldenResult:
    n_trades: int
    gross_pnl: float
    net_of_costs: float
    net_after_tax: float
    profit_factor: float
    turnover: float
    reconciliation_diff: float


def run_golden() -> GoldenResult:
    """Run the fixture through the real pipeline and return the four numbers."""
    from supercortex.backtest.runner import BacktestSpec, run
    from supercortex.costs.model import CostModel, apply

    trades, diagnostics = run(
        BacktestSpec(
            strategy="supercortex.backtest.golden:GoldenStrategy",
            interval="1h",
            starting_balance=1_000_000.0,
            params={"entry_bar": 10, "exit_bar": 30, "quantity": 1.0, "repeat_every": 60},
            synthetic_bars=sawtooth(),
        )
    )
    costed = apply(trades, CostModel())
    return GoldenResult(
        n_trades=costed.n_trades,
        gross_pnl=round(costed.gross_pnl, 6),
        net_of_costs=round(costed.net_of_costs, 6),
        net_after_tax=round(costed.net_after_tax, 6),
        profit_factor=round(costed.profit_factor_gross, 6),
        turnover=round(costed.turnover, 6),
        reconciliation_diff=diagnostics["reconciliation_diff"],
    )
