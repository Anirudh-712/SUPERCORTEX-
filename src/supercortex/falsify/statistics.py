"""The statistics that decide whether a result is real.

Every function here is a pure function of a returns array. No engine, no registry, no
strategy — which is what makes them testable against known answers, and a module where "a
bug is indistinguishable from a lie" has to be.

The central idea, and the reason the registry exists at all: **an observed Sharpe ratio is
meaningless without knowing how many were looked at.** Pick the best of a thousand random
strategies and it will have a fine Sharpe. The Deflated Sharpe Ratio asks how surprising the
best one is *given* the search, and it cannot be computed honestly unless something counted
the search.

References:
  Bailey & López de Prado (2012), "The Sharpe Ratio Efficient Frontier" — PSR.
  Bailey & López de Prado (2014), "The Deflated Sharpe Ratio" — DSR, minimum track record.
  Bailey, Borwein, López de Prado & Zhu (2016), "The Probability of Backtest
    Overfitting" — CSCV.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import stats

EULER_MASCHERONI = 0.5772156649015329


# ── Sharpe and its distribution ─────────────────────────────────────────────────
def sharpe_ratio(returns: np.ndarray, periods_per_year: int | None = None) -> float:
    """Sharpe per observation, or annualised if a period count is given.

    Deliberately *not* annualised by default. Annualisation is a presentation choice;
    every test below needs the per-observation figure, and silently annualising inside a
    statistical routine is how a factor of sqrt(252) ends up somewhere it should not be.
    """
    returns = np.asarray(returns, dtype=float)
    if returns.size < 2:
        return 0.0
    sd = returns.std(ddof=1)
    if sd == 0:
        return 0.0
    sr = float(returns.mean() / sd)
    return sr * math.sqrt(periods_per_year) if periods_per_year else sr


def probabilistic_sharpe_ratio(
    returns: np.ndarray, benchmark_sr: float = 0.0
) -> float:
    """P(true Sharpe > benchmark), correcting for skew, kurtosis and sample length.

    A Sharpe estimated from 30 observations of a fat-tailed, negatively-skewed series is
    far less trustworthy than the same number from 3000 well-behaved ones. PSR says by how
    much. Bailey & López de Prado (2012).
    """
    returns = np.asarray(returns, dtype=float)
    n = returns.size
    if n < 3:
        return 0.0
    sr = sharpe_ratio(returns)
    skew = float(stats.skew(returns, bias=False))
    kurt = float(stats.kurtosis(returns, fisher=False, bias=False))  # non-excess

    denominator = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr**2
    if denominator <= 0:
        return 0.0
    z = (sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(denominator)
    return float(stats.norm.cdf(z))


def expected_maximum_sharpe(n_trials: int, sharpe_variance: float) -> float:
    """The Sharpe you would expect from the LUCKIEST of `n_trials`, all with zero edge.

    This is the benchmark a real strategy has to clear. With enough trials the expected
    maximum grows without bound, which is precisely why an uncounted search is fatal.

    Uses the extreme-value approximation from Bailey & López de Prado (2014).
    """
    if n_trials < 2 or sharpe_variance <= 0:
        return 0.0
    gamma = EULER_MASCHERONI
    a = stats.norm.ppf(1.0 - 1.0 / n_trials)
    b = stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(math.sqrt(sharpe_variance) * ((1.0 - gamma) * a + gamma * b))


def deflated_sharpe_ratio(
    returns: np.ndarray, n_trials: int, sharpe_variance: float
) -> float:
    """PSR measured against the expected maximum Sharpe from `n_trials`.

    Interpretation: the probability that the true Sharpe exceeds zero, **given that this
    was the best of `n_trials` looks at the data**. Above 0.95 is the usual bar.

    `n_trials` must come from the registry, not from how many results someone chose to
    keep. That is the entire reason the trial table is append-only.
    """
    benchmark = expected_maximum_sharpe(n_trials, sharpe_variance)
    return probabilistic_sharpe_ratio(returns, benchmark_sr=benchmark)


def minimum_track_record_length(
    returns: np.ndarray, benchmark_sr: float = 0.0, confidence: float = 0.95
) -> float:
    """Observations needed before the observed Sharpe is statistically distinguishable.

    Answers "how long must I run this before I could even tell?" — often longer than the
    backtest, which is itself a finding.
    """
    returns = np.asarray(returns, dtype=float)
    if returns.size < 3:
        return float("inf")
    sr = sharpe_ratio(returns)
    if sr <= benchmark_sr:
        return float("inf")
    skew = float(stats.skew(returns, bias=False))
    kurt = float(stats.kurtosis(returns, fisher=False, bias=False))
    z = stats.norm.ppf(confidence)
    numerator = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr**2
    return float(1.0 + numerator * (z / (sr - benchmark_sr)) ** 2)


# ── probability of backtest overfitting ─────────────────────────────────────────
@dataclass(frozen=True)
class PBOResult:
    pbo: float
    n_combinations: int
    logits: list[float]
    median_oos_rank: float

    @property
    def overfit(self) -> bool:
        return self.pbo >= 0.5


def probability_of_backtest_overfitting(
    returns_matrix: np.ndarray, n_splits: int = 10
) -> PBOResult:
    """PBO via Combinatorially Symmetric Cross-Validation.

    `returns_matrix` is (T observations x N configurations) — one column per parameter
    setting tried. The procedure:

      1. Cut the timeline into `n_splits` contiguous blocks.
      2. For every way of choosing half the blocks as in-sample, take the complement as
         out-of-sample.
      3. Pick the configuration that ranks best in-sample; find its rank out-of-sample.
      4. PBO is the share of splits where that pick lands below the OOS median.

    A PBO near 0.5 means the in-sample winner is no better than a coin flip out of sample —
    the selection procedure has no skill, whatever the headline backtest says.

    Note this measures the **procedure**, not any single configuration. That is the point:
    overfitting is a property of how you searched, not of the answer you kept.
    """
    from itertools import combinations

    matrix = np.asarray(returns_matrix, dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] < 2:
        raise ValueError("returns_matrix must be (T x N) with at least 2 configurations")
    if n_splits % 2 != 0:
        raise ValueError("n_splits must be even so the halves are symmetric")

    rows = matrix.shape[0] - (matrix.shape[0] % n_splits)
    if rows < n_splits * 2:
        raise ValueError("not enough observations for the requested number of splits")
    matrix = matrix[:rows]
    blocks = np.array_split(matrix, n_splits, axis=0)

    logits: list[float] = []
    below_median = 0
    all_indices = set(range(n_splits))

    for is_blocks in combinations(range(n_splits), n_splits // 2):
        oos_blocks = sorted(all_indices - set(is_blocks))
        is_data = np.vstack([blocks[i] for i in is_blocks])
        oos_data = np.vstack([blocks[i] for i in oos_blocks])

        is_scores = np.array([sharpe_ratio(is_data[:, c]) for c in range(matrix.shape[1])])
        oos_scores = np.array([sharpe_ratio(oos_data[:, c]) for c in range(matrix.shape[1])])

        best = int(np.argmax(is_scores))
        # Relative rank of the in-sample winner, out of sample. 1.0 = best, ~0 = worst.
        rank = float(stats.rankdata(oos_scores)[best] / (len(oos_scores) + 1))
        if rank <= 0.5:
            below_median += 1
        rank = min(max(rank, 1e-9), 1 - 1e-9)
        logits.append(float(math.log(rank / (1.0 - rank))))

    n_combos = len(logits)
    return PBOResult(
        pbo=below_median / n_combos if n_combos else 0.0,
        n_combinations=n_combos,
        logits=logits,
        median_oos_rank=float(np.median([1 / (1 + math.exp(-x)) for x in logits])),
    )


# ── resampling ──────────────────────────────────────────────────────────────────
def max_drawdown(equity: np.ndarray) -> float:
    """Maximum peak-to-trough decline as a positive fraction."""
    equity = np.asarray(equity, dtype=float)
    if equity.size == 0:
        return 0.0
    peaks = np.maximum.accumulate(equity)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdowns = np.where(peaks > 0, (peaks - equity) / peaks, 0.0)
    return float(np.max(drawdowns))


def block_bootstrap_drawdowns(
    trade_pnls: np.ndarray,
    starting_equity: float,
    n_samples: int = 10_000,
    block_size: int = 5,
    seed: int = 42,
) -> np.ndarray:
    """Resample the trade sequence in blocks and measure drawdown each time.

    Blocks rather than individual trades because losing streaks cluster: shuffling trade by
    trade destroys that dependence and produces a drawdown distribution far kinder than
    reality. The observed maximum drawdown should sit *inside* this distribution — if it
    is at the extreme, the backtest got a lucky ordering.
    """
    pnls = np.asarray(trade_pnls, dtype=float)
    if pnls.size == 0:
        return np.zeros(0)
    rng = np.random.default_rng(seed)
    n = pnls.size
    n_blocks = max(1, math.ceil(n / block_size))

    results = np.empty(n_samples)
    for i in range(n_samples):
        starts = rng.integers(0, n, size=n_blocks)
        sequence = np.concatenate([pnls[s : s + block_size] for s in starts])[:n]
        equity = starting_equity + np.cumsum(sequence)
        results[i] = max_drawdown(equity)
    return results


def randomised_entry_null(
    price_series: np.ndarray,
    n_trades: int,
    holding_bars: int,
    n_samples: int = 10_000,
    seed: int = 42,
    long_only: bool = False,
) -> np.ndarray:
    """Distribution of returns from entering at random with the same trade profile.

    The question this answers: would a monkey with the same trade count and holding period
    have done as well? For a long-biased strategy in a rising market the answer is very
    often yes, and that is not a strategy — it is beta with extra steps.
    """
    prices = np.asarray(price_series, dtype=float)
    if prices.size <= holding_bars or n_trades < 1:
        return np.zeros(0)
    rng = np.random.default_rng(seed)
    last_valid = prices.size - holding_bars - 1

    results = np.empty(n_samples)
    for i in range(n_samples):
        entries = rng.integers(0, last_valid, size=n_trades)
        exits = entries + holding_bars
        moves = (prices[exits] - prices[entries]) / prices[entries]
        if not long_only:
            moves = moves * rng.choice([-1.0, 1.0], size=n_trades)
        results[i] = float(np.sum(moves))
    return results


def percentile_of(value: float, distribution: np.ndarray) -> float:
    """Where an observed value sits in a simulated distribution, as a fraction."""
    dist = np.asarray(distribution, dtype=float)
    if dist.size == 0:
        return float("nan")
    return float(np.mean(dist <= value))
