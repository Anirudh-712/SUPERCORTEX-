"""The falsification battery. Its job is to kill strategies, not to evaluate them.

Design rules, all of which exist because the alternative is a battery that quietly passes
everything:

* **Thresholds live in git**, in `falsify/thresholds.yaml`. Loosening one shows in a diff
  and needs a decision note. The bar cannot move to meet the number.
* **A test that cannot run is a FAIL**, never a skip. A battery where "insufficient data"
  reads as a pass is worse than no battery, because it produces a verdict people trust.
* **Every engine re-run is a registered trial.** The battery reports its cost in trials
  *before* spending them, because that cost inflates the multiple-testing penalty for the
  very result it is about to judge.
* **The Deflated Sharpe reads the trial count from the registry**, never from a caller.
  That number is the entire reason the trial table is append-only.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TypedDict, TypeGuard

import numpy as np
import yaml

from supercortex.costs.model import CostModel, Trade, apply, format_profit_factor
from supercortex.falsify import benchmark as bench
from supercortex.falsify import statistics as st
from supercortex.registry.db import PROJECT_ROOT

THRESHOLDS_PATH = PROJECT_ROOT / "falsify" / "thresholds.yaml"


# ── the thresholds schema ───────────────────────────────────────────────────────
# Every section the static battery reads, stated once. These are TypedDicts rather than a
# loose mapping because each test indexes exactly one section and one field: with
# `dict[str, Any]` a renamed key is a KeyError at run time, halfway through a battery,
# after the expensive part has already happened.
class CostLadder(TypedDict):
    survive_multiple: float


class WalkForward(TypedDict):
    min_positive_window_fraction: float
    min_windows: int
    embargo_fraction: float


class PBO(TypedDict):
    max: float


class DeflatedSharpe(TypedDict):
    min: float


class RandomisedEntry(TypedDict):
    min_percentile: float


class MonteCarlo(TypedDict):
    max_observed_drawdown_percentile: float


class Regime(TypedDict):
    max_single_year_pnl_share: float


class PostTax(TypedDict):
    require_positive_under: str


class Benchmark(TypedDict):
    must_beat_buy_and_hold_after_tax: bool
    min_excess_fraction: float


class Thresholds(TypedDict):
    """What the static battery requires. Documents may carry more — sections for the
    dynamic tests, `set_on` — and validation leaves those untouched."""

    version: int
    cost_ladder: CostLadder
    walk_forward: WalkForward
    pbo: PBO
    deflated_sharpe: DeflatedSharpe
    randomised_entry: RandomisedEntry
    monte_carlo: MonteCarlo
    regime: Regime
    post_tax: PostTax
    benchmark: Benchmark


class ThresholdsError(RuntimeError):
    """The thresholds cannot be trusted, so the battery must not run.

    A RuntimeError so existing callers keep catching it, but its own type so a
    configuration problem is never confused with a test that legitimately failed.
    """


#: field -> kind, per section. The kinds are checked against the parsed YAML, not coerced:
#: `min_windows: 5.5` is a mistake worth naming, not something to round.
_SCHEMA: dict[str, dict[str, str]] = {
    "cost_ladder": {"survive_multiple": "number"},
    "walk_forward": {
        "min_positive_window_fraction": "number",
        "min_windows": "integer",
        "embargo_fraction": "number",
    },
    "pbo": {"max": "number"},
    "deflated_sharpe": {"min": "number"},
    "randomised_entry": {"min_percentile": "number"},
    "monte_carlo": {"max_observed_drawdown_percentile": "number"},
    "regime": {"max_single_year_pnl_share": "number"},
    "post_tax": {"require_positive_under": "text"},
    "benchmark": {
        "must_beat_buy_and_hold_after_tax": "boolean",
        "min_excess_fraction": "number",
    },
}


def _matches(value: object, kind: str) -> bool:
    """`True` is an `int` in Python, so every numeric kind excludes it explicitly. A
    threshold written as `true` is a typo, not a limit of 1.0."""
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "text":
        return isinstance(value, str)
    if isinstance(value, bool):
        return False
    if kind == "integer":
        return isinstance(value, int)
    return isinstance(value, int | float)


def _shape_errors(raw: object) -> list[str]:
    """Every way `raw` falls short of a usable thresholds document.

    All of them, not just the first: an operator repairing a truncated file should see the
    whole list once rather than rerun the battery to discover the next missing key.
    """
    if raw is None:
        return ["the document is empty; there are no default thresholds to fall back on"]
    if not isinstance(raw, dict):
        return [f"the document is a {type(raw).__name__}, expected a mapping of sections"]

    problems: list[str] = []
    if not _matches(raw.get("version"), "integer"):
        problems.append("version is missing or is not an integer")

    for section, fields in _SCHEMA.items():
        block = raw.get(section)
        if section not in raw:
            problems.append(f"section {section} is missing")
            continue
        if not isinstance(block, dict):
            problems.append(
                f"section {section} is a {type(block).__name__}, expected a mapping"
            )
            continue
        for field_name, kind in fields.items():
            if field_name not in block:
                problems.append(f"{section}.{field_name} is missing")
            elif not _matches(block[field_name], kind):
                problems.append(
                    f"{section}.{field_name} is "
                    f"{type(block[field_name]).__name__}, expected {kind}"
                )
    return problems


def _is_thresholds(raw: object) -> TypeGuard[Thresholds]:
    return not _shape_errors(raw)


def load_thresholds(path: Path | None = None) -> Thresholds:
    """Read the thresholds, or refuse to produce any.

    Checked once, here, rather than discovered key by key inside individual tests. A
    battery that runs on a partial document is the rubber stamp this module exists to
    prevent — and `benchmark` in particular used to default to enabled when its section was
    absent, so a truncated file produced a full verdict with a gate nobody had configured.
    """
    target = path or THRESHOLDS_PATH
    if not target.exists():
        raise ThresholdsError(
            f"{target} is missing. A battery with no thresholds cannot fail anything, "
            f"which makes it a rubber stamp. Refusing to run."
        )
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ThresholdsError(
            f"{target} could not be parsed as YAML: {exc}. Refusing to run: thresholds "
            f"that cannot be read cannot fail anything."
        ) from exc

    if not _is_thresholds(raw):
        problems = "; ".join(_shape_errors(raw))
        raise ThresholdsError(
            f"{target} is not a usable thresholds document: {problems}. Refusing to run: "
            f"a battery on partial thresholds silently judges a strategy against limits "
            f"nobody set."
        )
    return raw


class BatteryStatus(StrEnum):
    """What a battery test can conclude, and nothing else.

    Two members on purpose. "A test that cannot run is a FAIL, never a skip" is the rule
    the whole battery rests on, and with no third member there is nothing to reach for
    when a test is inconvenient.
    """

    PASS = "PASS"
    FAIL = "FAIL"


@dataclass(frozen=True)
class TestResult:
    name: str
    passed: bool
    statistic: float | None
    threshold: float | None
    detail: str
    trials_spent: int = 0

    @property
    def status(self) -> BatteryStatus:
        return BatteryStatus.PASS if self.passed else BatteryStatus.FAIL


@dataclass
class Context:
    """Everything the battery needs. Missing pieces cause FAILs, never skips."""

    experiment_id: str
    trades: list[Trade]
    prices: np.ndarray
    timestamps: np.ndarray
    starting_equity: float
    model: CostModel
    trials_consumed: int
    #: Returns matrix (T x N configurations) from a declared parameter sweep, if one was run.
    sweep_returns: np.ndarray | None = None
    #: Per-configuration final results from the sweep, for the neighbourhood test.
    sweep_scores: dict[str, float] = field(default_factory=dict)
    chosen_config: str | None = None
    #: Cross-trial Sharpe variance. Nothing supplies this today and nothing may supply it
    #: from unauthenticated bytes: the only source that ever existed read sibling metrics
    #: documents straight off disk, with no digest, no registry binding and no provenance,
    #: which made the DSR's deflation settable by anyone with write access. A future source
    #: must be authenticated evidence or this field stays None.
    sharpe_variance: float | None = None

    def trade_returns(self) -> np.ndarray:
        """Per-trade return on starting equity. The actual decisions, not a resampling."""
        if not self.trades:
            return np.zeros(0)
        ordered = sorted(self.trades, key=lambda t: t.exit_ts_ms)
        return np.array(
            [self.model.net_of_costs(t) / self.starting_equity for t in ordered],
            dtype=float,
        )

    def effective_sharpe_variance(self) -> tuple[float, str]:
        """Variance of the Sharpe estimator, and an honest label for where it came from.

        Preference order: an authenticated measurement of dispersion across this
        experiment's trials, then the asymptotic approximation Var(SR) ~ (1 + SR^2/2)/n.
        The label matters — a DSR computed from an assumed variance is a weaker claim than
        one computed from measured dispersion, and the report should say which it is.

        Today it is always the approximation, and the label says why rather than implying
        the desk had one trial. There is no authenticated cross-trial variance to have.
        """
        if self.sharpe_variance is not None and self.sharpe_variance > 0:
            return self.sharpe_variance, "measured across this experiment's trials"
        returns = self.trade_returns()
        n = max(returns.size, 2)
        sr = st.sharpe_ratio(returns)
        return (1.0 + 0.5 * sr**2) / n, ("asymptotic approximation; no authenticated "
                                         "cross-trial variance available")


# ── the tests ───────────────────────────────────────────────────────────────────
def test_cost_ladder(ctx: Context, thresholds: Thresholds) -> TestResult:
    """Re-apply the cost model at multiples. Costs no trials — the trades already exist."""
    from dataclasses import replace

    multiple = float(thresholds["cost_ladder"]["survive_multiple"])
    rungs = {}
    for m in (1.0, 1.5, multiple, 3.0):
        scaled = replace(
            ctx.model,
            taker_fee_bps=ctx.model.taker_fee_bps * m,
            maker_fee_bps=ctx.model.maker_fee_bps * m,
            slippage_bps=ctx.model.slippage_bps * m,
            half_spread_bps=ctx.model.half_spread_bps * m,
        )
        rungs[m] = apply(ctx.trades, scaled).net_after_tax

    survives = rungs[multiple] > 0
    ladder = " · ".join(f"{m:g}x {v:,.0f}" for m, v in rungs.items())
    return TestResult(
        "3-cost-ladder", survives, rungs[multiple], 0.0,
        f"net after tax at each cost multiple: {ladder}. "
        f"{'Survives' if survives else 'Dies'} at {multiple:g}x.",
    )


def test_walk_forward(ctx: Context, thresholds: Thresholds) -> TestResult:
    """Contiguous out-of-sample windows with an embargo between them.

    Purging matters when labels overlap; here trades are discrete and non-overlapping, so
    the embargo is the operative protection — it drops trades adjacent to a window boundary
    so a position spanning the split cannot be counted on both sides.
    """
    cfg = thresholds["walk_forward"]
    min_windows = int(cfg["min_windows"])
    required = float(cfg["min_positive_window_fraction"])
    embargo_fraction = float(cfg["embargo_fraction"])

    ordered = sorted(ctx.trades, key=lambda t: t.exit_ts_ms)
    if len(ordered) < min_windows * 2:
        return TestResult(
            "5-walk-forward", False, float(len(ordered)), float(min_windows * 2),
            f"only {len(ordered)} trades; need at least {min_windows * 2} to form "
            f"{min_windows} windows. Cannot run, therefore FAIL — a battery where "
            f"'insufficient data' reads as a pass is a rubber stamp.",
        )

    embargo = max(1, int(len(ordered) * embargo_fraction))
    chunks = np.array_split(np.arange(len(ordered)), min_windows)
    positives = 0
    for chunk in chunks:
        kept = chunk[embargo:] if len(chunk) > embargo else chunk
        pnl = sum(ctx.model.net_of_costs(ordered[i]) for i in kept)
        positives += pnl > 0

    fraction = positives / min_windows
    return TestResult(
        "5-walk-forward", fraction >= required, fraction, required,
        f"{positives}/{min_windows} out-of-sample windows profitable "
        f"({fraction:.0%}, need {required:.0%}); {embargo} trades embargoed per boundary.",
    )


def test_pbo(ctx: Context, thresholds: Thresholds) -> TestResult:
    """PBO via CSCV. Requires a declared parameter sweep — without one it cannot run."""
    limit = float(thresholds["pbo"]["max"])
    if ctx.sweep_returns is None:
        return TestResult(
            "6-pbo", False, None, limit,
            "no parameter sweep supplied. PBO measures whether the SELECTION procedure "
            "has skill, so it is undefined when nothing was selected. Run the declared "
            "grid first. Cannot run, therefore FAIL.",
        )
    try:
        result = st.probability_of_backtest_overfitting(ctx.sweep_returns)
    except ValueError as exc:
        return TestResult("6-pbo", False, None, limit, f"cannot run: {exc}")
    return TestResult(
        "6-pbo", result.pbo <= limit, result.pbo, limit,
        f"PBO {result.pbo:.3f} across {result.n_combinations} splits (limit {limit}). "
        + ("The in-sample winner holds up out of sample."
           if result.pbo <= limit
           else "The in-sample winner is close to a coin flip out of sample."),
    )


def test_deflated_sharpe(ctx: Context, thresholds: Thresholds) -> TestResult:
    """The number the registry exists for."""
    minimum = float(thresholds["deflated_sharpe"]["min"])
    returns = ctx.trade_returns()
    if returns.size < 3:
        return TestResult(
            "7-deflated-sharpe", False, None, minimum,
            f"only {returns.size} trades; the higher moments the DSR needs are not "
            f"estimable. Cannot run, therefore FAIL.",
        )
    variance, source = ctx.effective_sharpe_variance()
    dsr = st.deflated_sharpe_ratio(returns, ctx.trials_consumed, variance)
    undeflated = st.probabilistic_sharpe_ratio(returns)
    return TestResult(
        "7-deflated-sharpe", dsr >= minimum, dsr, minimum,
        f"DSR {dsr:.4f} against {ctx.trials_consumed} registered trials "
        f"(undeflated PSR {undeflated:.4f}). Sharpe variance {variance:.6f}, {source}.",
    )


def test_randomised_entry(ctx: Context, thresholds: Thresholds) -> TestResult:
    """Would a monkey with the same trade profile have done as well?"""
    required = float(thresholds["randomised_entry"]["min_percentile"])
    if not ctx.trades or ctx.prices.size < 10:
        return TestResult("9-randomised-entry", False, None, required,
                          "no trades or price series. Cannot run, therefore FAIL.")

    ordered = sorted(ctx.trades, key=lambda t: t.exit_ts_ms)
    holding_ms = int(np.median([t.exit_ts_ms - t.entry_ts_ms for t in ordered]))
    span = int(ctx.timestamps[-1] - ctx.timestamps[0])
    bar_ms = max(span // max(ctx.prices.size - 1, 1), 1)
    holding_bars = max(int(holding_ms / bar_ms), 1)

    observed = sum(
        t.direction * (t.exit_price - t.entry_price) / t.entry_price for t in ordered
    )
    null = st.randomised_entry_null(
        ctx.prices, len(ordered), holding_bars, n_samples=5_000, seed=42
    )
    if null.size == 0:
        return TestResult("9-randomised-entry", False, None, required,
                          "price series shorter than the holding period. FAIL.")
    percentile = st.percentile_of(observed, null)
    return TestResult(
        "9-randomised-entry", percentile >= required, percentile, required,
        f"the strategy beats {percentile:.1%} of random entries with the same trade count "
        f"({len(ordered)}) and median holding period ({holding_bars} bars). "
        f"Need {required:.0%}.",
    )


def test_monte_carlo_drawdown(ctx: Context, thresholds: Thresholds) -> TestResult:
    """Was the observed drawdown lucky ordering?"""
    limit = float(thresholds["monte_carlo"]["max_observed_drawdown_percentile"])
    if len(ctx.trades) < 10:
        return TestResult("10-monte-carlo", False, None, limit,
                          f"only {len(ctx.trades)} trades. Cannot run, therefore FAIL.")
    ordered = sorted(ctx.trades, key=lambda t: t.exit_ts_ms)
    pnls = np.array([ctx.model.net_of_costs(t) for t in ordered])
    observed = st.max_drawdown(
        bench.strategy_equity_curve(ordered, ctx.starting_equity, ctx.model)
    )
    distribution = st.block_bootstrap_drawdowns(
        pnls, ctx.starting_equity, n_samples=5_000, seed=42
    )
    percentile = st.percentile_of(observed, distribution)
    return TestResult(
        "10-monte-carlo", percentile <= limit, percentile, limit,
        f"observed max drawdown {observed:.2%} sits at the {percentile:.1%} point of the "
        f"resampled distribution (95th percentile {np.percentile(distribution, 95):.2%}). "
        f"{'Inside the band.' if percentile <= limit else 'The backtest got a kind ordering.'}",
    )


def test_regime_attribution(ctx: Context, thresholds: Thresholds) -> TestResult:
    """No single year may carry the result."""
    limit = float(thresholds["regime"]["max_single_year_pnl_share"])
    if not ctx.trades:
        return TestResult("G1-regime", False, None, limit, "no trades. FAIL.")

    by_year: dict[int, float] = {}
    for t in ctx.trades:
        year = datetime.fromtimestamp(t.exit_ts_ms / 1000, UTC).year
        by_year[year] = by_year.get(year, 0.0) + ctx.model.net_of_costs(t)

    total_positive = sum(v for v in by_year.values() if v > 0)
    if total_positive <= 0:
        return TestResult("G1-regime", False, 1.0, limit,
                          "no profitable year. FAIL.")
    shares = {y: v / total_positive for y, v in by_year.items() if v > 0}
    worst_year, worst_share = max(shares.items(), key=lambda kv: kv[1])
    detail = " · ".join(f"{y}: {s:.0%}" for y, s in sorted(shares.items()))
    return TestResult(
        "G1-regime", worst_share <= limit, worst_share, limit,
        f"largest single-year share of gross profit is {worst_year} at {worst_share:.0%} "
        f"(limit {limit:.0%}). Breakdown: {detail}.",
    )


def test_post_tax(ctx: Context, thresholds: Thresholds) -> TestResult:
    """Positive after the conservative tax reading, or it is not a strategy here."""
    regime = thresholds["post_tax"]["require_positive_under"]
    result = apply(ctx.trades, ctx.model)
    passed = result.net_after_tax > 0
    return TestResult(
        "G2-post-tax", passed, result.net_after_tax, 0.0,
        f"net after tax under {regime}: {result.net_after_tax:,.2f}. "
        f"Profit factor "
        f"{format_profit_factor(result.profit_factor_gross, result.profit_factor_gross_state)}"
        f" against a break-even of {result.break_even_profit_factor:.3f}.",
    )


def test_benchmark(ctx: Context, thresholds: Thresholds) -> TestResult:
    """Did it beat buying once and holding, after tax?"""
    cfg = thresholds["benchmark"]
    if not cfg["must_beat_buy_and_hold_after_tax"]:
        return TestResult("G3-benchmark", True, None, None, "gate disabled in thresholds")
    if ctx.prices.size < 2:
        return TestResult("G3-benchmark", False, None, None,
                          "no price series to benchmark against. FAIL.")

    c = bench.compare(ctx.trades, ctx.prices, ctx.timestamps, ctx.starting_equity, ctx.model)
    note = ""
    if c.portfolio_argument:
        note = (" It wins per unit of drawdown, which is a PORTFOLIO argument — that it "
                "could be levered to match. That is a separate decision bounded by the "
                "risk engine's leverage limits, and it does not satisfy this gate.")
    return TestResult(
        "G3-benchmark", c.beats_absolute, c.excess, 0.0,
        f"strategy {c.strategy_net:,.0f} vs buy-and-hold {c.benchmark_net:,.0f} after tax "
        f"(excess {c.excess:,.0f}). Drawdown {c.strategy_max_drawdown:.1%} vs "
        f"{c.benchmark_max_drawdown:.1%}.{note}",
    )


#: One shape for every static test: judge this context against these thresholds and say
#: what happened. A test never returns None and never raises to mean "skipped" — a test
#: that cannot run returns a TestResult with passed=False, which is what makes
#: "cannot run is a FAIL" a property of the type rather than a convention.
StaticTest = Callable[[Context, Thresholds], TestResult]

#: Tests that need no engine re-run. Each declares its trial cost so the battery can
#: report what it will spend before it spends it.
STATIC_TESTS: tuple[StaticTest, ...] = (
    test_cost_ladder,
    test_walk_forward,
    test_pbo,
    test_deflated_sharpe,
    test_randomised_entry,
    test_monte_carlo_drawdown,
    test_regime_attribution,
    test_post_tax,
    test_benchmark,
)

#: The eleven numbered tests promotion requires, per §16.1 of the architecture report.
#: Named here as a whole so a partial battery can say which tests it did NOT run. A report
#: that lists only what it did read as a complete verdict, which is exactly how a strategy
#: gets promoted on six tests out of eleven.
PROMOTION_GATE_TESTS: tuple[str, ...] = tuple(str(n) for n in range(1, 12))


def gate_number(name: str) -> str | None:
    """The promotion-gate test a result belongs to, or None for a structural gate.

    Read off the result's own name rather than a hand-kept list, so adding a static test
    changes the coverage report by construction instead of leaving a second list to
    forget.
    """
    head = name.split("-", 1)[0]
    return head if head in PROMOTION_GATE_TESTS else None


def covered_numbers(results: Sequence[TestResult]) -> tuple[str, ...]:
    """Which of the eleven these results answer, in gate order."""
    found = {gate_number(r.name) for r in results}
    return tuple(n for n in PROMOTION_GATE_TESTS if n in found)


def uncovered_numbers(results: Sequence[TestResult]) -> tuple[str, ...]:
    """Which of the eleven these results leave unanswered. The honest half of the report."""
    found = {gate_number(r.name) for r in results}
    return tuple(n for n in PROMOTION_GATE_TESTS if n not in found)


def structural_gates(results: Sequence[TestResult]) -> tuple[str, ...]:
    """Results that are not one of the eleven: the structural gates, which block promotion
    all the same."""
    return tuple(r.name for r in results if gate_number(r.name) is None)


#: What each promotion-gate test the static battery cannot run still needs before it can.
#: Split because the two debts are not the same kind. An engine re-run is trial budget the
#: desk can choose to spend today; test 8 cannot be run at any price until a variant family
#: is registered AND Hansen's SPA / White's Reality Check are implemented. Reporting them
#: together as "needs a re-run" says promotion is a budget decision when it is not.
UNRUN_REQUIREMENTS: dict[str, str] = {
    "1": "registered engine re-runs",
    "2": "registered engine re-runs",
    "4": "registered engine re-runs",
    "11": "registered engine re-runs",
    "8": "authenticated registered variant-family evidence, plus the SPA / Reality Check "
         "implementation — neither exists yet",
}

#: For a test whose requirement nobody wrote down. It gets its own line rather than being
#: dropped: a gap that vanishes from the report is a gap the desk stops seeing.
UNRECORDED_REQUIREMENT = "an unrecorded prerequisite; nothing states what this test needs"


def unrun_by_requirement(
    results: Sequence[TestResult],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The uncovered tests, grouped by what each still needs, in gate order."""
    groups: dict[str, list[str]] = {}
    for number in uncovered_numbers(results):
        groups.setdefault(UNRUN_REQUIREMENTS.get(number, UNRECORDED_REQUIREMENT),
                          []).append(number)
    return tuple((need, tuple(numbers)) for need, numbers in groups.items())


#: Tests requiring engine re-runs, with their cost in registered trials.
DYNAMIC_TEST_COSTS: dict[str, int] = {
    "1-lookahead": 6,          # 3 probes x 2 channels
    "2-future-shuffle": 1,
    "4-parameter-neighbourhood": 5,
    "11-reproducibility": 1,
}


@dataclass(frozen=True)
class Verdict:
    experiment_id: str
    results: list[TestResult]
    trials_at_verdict: int
    thresholds_version: int

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[TestResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        if self.passed:
            return (f"{len(self.results)}/{len(self.results)} tests survived. The strategy "
                    f"was attacked and did not die.")
        names = ", ".join(r.name for r in self.failures)
        return f"{len(self.failures)} of {len(self.results)} tests failed: {names}"


def run_static_battery(ctx: Context, thresholds: Thresholds | None = None) -> Verdict:
    """Run every test that needs no engine re-run. Costs zero trials."""
    cfg = thresholds if thresholds is not None else load_thresholds()
    results = [fn(ctx, cfg) for fn in STATIC_TESTS]
    return Verdict(ctx.experiment_id, results, ctx.trials_consumed, cfg["version"])


def dynamic_trial_cost(channels: int = 2) -> int:
    """What the re-run tests will cost, in registered trials, before spending them."""
    cost = dict(DYNAMIC_TEST_COSTS)
    cost["1-lookahead"] = 3 * channels
    return sum(cost.values())


def annualisation_factor(trades: list[Trade]) -> float:
    """Trades per year, for presenting a per-trade Sharpe annually."""
    if len(trades) < 2:
        return 1.0
    span_ms = max(t.exit_ts_ms for t in trades) - min(t.entry_ts_ms for t in trades)
    years = span_ms / (365.25 * 24 * 3600 * 1000)
    return len(trades) / years if years > 0 else 1.0


def sharpe_summary(ctx: Context) -> dict[str, float]:
    returns = ctx.trade_returns()
    per_trade = st.sharpe_ratio(returns)
    per_year = annualisation_factor(ctx.trades)
    return {
        "sharpe_per_trade": per_trade,
        "trades_per_year": per_year,
        "sharpe_annualised": per_trade * math.sqrt(per_year) if per_year > 0 else 0.0,
    }
