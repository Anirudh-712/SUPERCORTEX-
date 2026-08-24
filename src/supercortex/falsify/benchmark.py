"""The benchmark gate: did the strategy beat doing nothing?

This test is not in the original eleven. It was added after engine validation produced a
result that cleared every statistical bar — profit factor 2.30 against a 1.45 threshold,
surviving all three tax regimes — and was still beaten five-fold by buying once and
holding. See [[LRN-000004]].

The asymmetry that makes this bind so hard under s.115BBH:

    buy-and-hold  ->  ONE set of frictions, ONE taxable disposal
    a strategy    ->  frictions and a disposal on EVERY round trip

So a directional strategy is not merely competing with the asset's return. It is competing
with the asset's return *plus* all the tax and frictions it avoided by not trading. In a
market that rose eightfold, that is a very high bar, and profit factor cannot see it.

Two gates are computed and both are reported. Only the absolute one blocks promotion:

* **absolute** — net-after-tax must exceed buy-and-hold's. Required.
* **risk-adjusted** — return per unit of drawdown. Informational.

A strategy that loses on absolute but wins on risk-adjusted is making a *portfolio*
argument: that it could be levered to match. That may well be true, and it is a separate
decision constrained by the risk engine's leverage limits. It is deliberately not allowed
to satisfy this gate quietly, because an escape hatch on a benchmark test is how the
benchmark stops mattering.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from supercortex.costs.model import CostedResult, CostModel, Trade, apply
from supercortex.falsify.statistics import max_drawdown


@dataclass(frozen=True)
class BenchmarkComparison:
    strategy_net: float
    benchmark_net: float
    strategy_max_drawdown: float
    benchmark_max_drawdown: float
    strategy_return_per_drawdown: float
    benchmark_return_per_drawdown: float
    beats_absolute: bool
    beats_risk_adjusted: bool
    excess: float

    @property
    def portfolio_argument(self) -> bool:
        """Loses outright, wins per unit of risk. A leverage question, not a pass."""
        return not self.beats_absolute and self.beats_risk_adjusted


def buy_and_hold(
    prices: np.ndarray,
    timestamps: np.ndarray,
    starting_equity: float,
    model: CostModel,
) -> tuple[CostedResult, float]:
    """The null every directional strategy must clear: buy once, sell at the end.

    Returns the costed result and the maximum drawdown of simply holding — which is
    usually brutal in crypto and is exactly why the risk-adjusted figure is reported.
    """
    prices = np.asarray(prices, dtype=float)
    if prices.size < 2:
        raise ValueError("need at least two prices to buy and hold")

    entry, exit_price = float(prices[0]), float(prices[-1])
    quantity = starting_equity / entry
    trade = Trade(
        entry_ts_ms=int(timestamps[0]),
        exit_ts_ms=int(timestamps[-1]),
        direction=1,
        qty=quantity,
        entry_price=entry,
        exit_price=exit_price,
    )
    equity_curve = starting_equity * (prices / entry)
    return apply([trade], model), max_drawdown(equity_curve)


def strategy_equity_curve(trades: list[Trade], starting_equity: float,
                          model: CostModel) -> np.ndarray:
    """Equity after each closed trade, net of execution costs.

    Trade-by-trade rather than mark-to-market: the strategy is flat between trades, so
    intra-trade excursions are not equity the desk ever had at risk of being realised.
    """
    if not trades:
        return np.array([starting_equity])
    ordered = sorted(trades, key=lambda t: t.exit_ts_ms)
    pnls = np.array([model.net_of_costs(t) for t in ordered], dtype=float)
    return np.concatenate([[starting_equity], starting_equity + np.cumsum(pnls)])


def compare(
    trades: list[Trade],
    prices: np.ndarray,
    timestamps: np.ndarray,
    starting_equity: float,
    model: CostModel,
) -> BenchmarkComparison:
    """Run both gates. Only `beats_absolute` may block promotion."""
    strategy = apply(trades, model)
    benchmark, benchmark_dd = buy_and_hold(prices, timestamps, starting_equity, model)
    strategy_dd = max_drawdown(strategy_equity_curve(trades, starting_equity, model))

    def per_drawdown(net: float, drawdown: float) -> float:
        # A strategy with no drawdown and a positive result is not infinitely good; it is
        # under-sampled. Floor the denominator rather than dividing by zero.
        return net / max(drawdown, 1e-6)

    strategy_ratio = per_drawdown(strategy.net_after_tax, strategy_dd)
    benchmark_ratio = per_drawdown(benchmark.net_after_tax, benchmark_dd)

    return BenchmarkComparison(
        strategy_net=strategy.net_after_tax,
        benchmark_net=benchmark.net_after_tax,
        strategy_max_drawdown=strategy_dd,
        benchmark_max_drawdown=benchmark_dd,
        strategy_return_per_drawdown=strategy_ratio,
        benchmark_return_per_drawdown=benchmark_ratio,
        beats_absolute=strategy.net_after_tax > benchmark.net_after_tax,
        beats_risk_adjusted=strategy_ratio > benchmark_ratio,
        excess=strategy.net_after_tax - benchmark.net_after_tax,
    )
