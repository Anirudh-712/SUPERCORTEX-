"""Falsification statistics, checked against answers known in advance.

Every test here either has a closed-form answer or a construction whose result is forced by
design (pure noise must look like pure noise; genuine skill must look like skill). A test
that merely records whatever the function returned would freeze a bug rather than catch one.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import stats

from supercortex.falsify import statistics as st


def normal(n: int, mean: float, sd: float, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).normal(mean, sd, n)


# ── Sharpe ──────────────────────────────────────────────────────────────────────
def test_sharpe_is_mean_over_sd():
    r = np.array([0.01, 0.02, 0.03, 0.04])
    assert st.sharpe_ratio(r) == pytest.approx(r.mean() / r.std(ddof=1))


def test_sharpe_annualises_by_sqrt_periods():
    r = normal(1000, 0.001, 0.01)
    assert st.sharpe_ratio(r, periods_per_year=252) == pytest.approx(
        st.sharpe_ratio(r) * math.sqrt(252)
    )


def test_constant_series_has_zero_sharpe_not_infinity():
    """Zero variance must not divide by zero."""
    assert st.sharpe_ratio(np.array([0.01] * 50)) == 0.0


def test_too_short_series_returns_zero():
    assert st.sharpe_ratio(np.array([0.01])) == 0.0


# ── probabilistic Sharpe ────────────────────────────────────────────────────────
def test_psr_is_half_when_observed_equals_benchmark():
    """By construction: PSR(SR) where the benchmark IS the observed Sharpe is 0.5."""
    r = normal(500, 0.001, 0.01)
    assert st.probabilistic_sharpe_ratio(r, st.sharpe_ratio(r)) == pytest.approx(0.5, abs=1e-9)


def test_psr_rises_with_sample_length():
    """The same Sharpe from more observations is more believable."""
    short = normal(60, 0.002, 0.01, seed=1)
    long = np.tile(short, 20)          # ~identical Sharpe, 20x the observations
    # Not bit-identical: the ddof=1 denominator uses n-1, so tiling shifts the sample
    # standard deviation by sqrt((n-1)/n) / sqrt((20n-1)/20n). That is an artifact of the
    # unbiased estimator, not of the Sharpe calculation.
    assert st.sharpe_ratio(long) == pytest.approx(st.sharpe_ratio(short), rel=0.01)
    assert st.probabilistic_sharpe_ratio(long) > st.probabilistic_sharpe_ratio(short)


def test_psr_penalises_negative_skew():
    """A left-tailed series earns less confidence than a symmetric one at equal Sharpe."""
    rng = np.random.default_rng(7)
    symmetric = rng.normal(0.001, 0.01, 2000)
    skewed = -np.abs(rng.normal(0, 0.01, 2000)) ** 1.5
    skewed = skewed - skewed.mean() + 0.001
    skewed = skewed / skewed.std(ddof=1) * symmetric.std(ddof=1)
    skewed = skewed - skewed.mean() + symmetric.mean()
    assert stats.skew(skewed) < stats.skew(symmetric)
    assert st.probabilistic_sharpe_ratio(skewed) < st.probabilistic_sharpe_ratio(symmetric)


def test_psr_of_a_losing_strategy_is_below_half():
    assert st.probabilistic_sharpe_ratio(normal(500, -0.001, 0.01)) < 0.5


# ── expected maximum from N trials ──────────────────────────────────────────────
def test_expected_maximum_sharpe_grows_with_trials():
    """The luckiest of many zero-edge strategies looks better the more you try."""
    values = [st.expected_maximum_sharpe(n, 0.01) for n in (2, 10, 100, 1000, 10_000)]
    assert values == sorted(values)
    assert all(v > 0 for v in values)


def test_expected_maximum_is_zero_without_trials_or_dispersion():
    assert st.expected_maximum_sharpe(1, 0.01) == 0.0
    assert st.expected_maximum_sharpe(1000, 0.0) == 0.0


def test_expected_maximum_scales_with_sqrt_variance():
    a = st.expected_maximum_sharpe(100, 0.01)
    b = st.expected_maximum_sharpe(100, 0.04)      # 4x variance = 2x sd
    assert b == pytest.approx(2 * a, rel=1e-9)


# ── deflated Sharpe: the number the registry exists for ─────────────────────────
def test_deflated_sharpe_falls_as_trial_count_rises():
    """The same result, searched harder, is less impressive. This is the whole point."""
    r = normal(1000, 0.0015, 0.01, seed=3)
    values = [st.deflated_sharpe_ratio(r, n, 0.01) for n in (2, 10, 100, 1000)]
    assert values == sorted(values, reverse=True)


def test_deflated_never_exceeds_undeflated():
    r = normal(1000, 0.0015, 0.01, seed=4)
    assert st.deflated_sharpe_ratio(r, 500, 0.01) <= st.probabilistic_sharpe_ratio(r)


def test_a_marginal_result_survives_few_trials_and_dies_under_many():
    """The failure mode the DSR exists to catch, demonstrated end to end."""
    r = normal(750, 0.0016, 0.01, seed=11)
    assert st.deflated_sharpe_ratio(r, n_trials=3, sharpe_variance=0.01) > 0.95
    assert st.deflated_sharpe_ratio(r, n_trials=5000, sharpe_variance=0.01) < 0.95


# ── probability of backtest overfitting ─────────────────────────────────────────
def test_pbo_of_pure_noise_is_near_one_half():
    """With no real skill anywhere, the in-sample winner is a coin flip out of sample."""
    rng = np.random.default_rng(21)
    noise = rng.normal(0, 0.01, size=(600, 12))
    result = st.probability_of_backtest_overfitting(noise, n_splits=8)
    assert 0.3 < result.pbo < 0.7
    assert result.n_combinations == math.comb(8, 4)


def test_pbo_is_low_when_one_configuration_genuinely_dominates():
    """Real, persistent skill should be selected reliably and hold up out of sample."""
    rng = np.random.default_rng(22)
    matrix = rng.normal(0, 0.01, size=(600, 12))
    matrix[:, 0] += 0.006                     # a large, persistent edge in column 0
    result = st.probability_of_backtest_overfitting(matrix, n_splits=8)
    assert result.pbo < 0.1
    assert not result.overfit


def test_pbo_is_high_when_the_edge_exists_only_in_the_first_half():
    """The classic overfit: a configuration that works until it doesn't."""
    rng = np.random.default_rng(23)
    matrix = rng.normal(0, 0.01, size=(600, 12))
    matrix[:300, 3] += 0.02                   # spectacular early, nothing later
    matrix[300:, 3] -= 0.02
    result = st.probability_of_backtest_overfitting(matrix, n_splits=8)
    assert result.pbo > 0.5
    assert result.overfit


def test_pbo_rejects_odd_split_counts():
    with pytest.raises(ValueError, match="even"):
        st.probability_of_backtest_overfitting(np.zeros((100, 3)), n_splits=7)


def test_pbo_needs_at_least_two_configurations():
    with pytest.raises(ValueError, match="at least 2"):
        st.probability_of_backtest_overfitting(np.zeros((100, 1)), n_splits=4)


def test_pbo_rejects_too_few_observations():
    with pytest.raises(ValueError, match="not enough observations"):
        st.probability_of_backtest_overfitting(np.zeros((6, 3)), n_splits=6)


# ── drawdown and resampling ─────────────────────────────────────────────────────
def test_max_drawdown_known_answer():
    equity = np.array([100.0, 120.0, 90.0, 110.0])
    assert st.max_drawdown(equity) == pytest.approx(0.25)      # 120 -> 90


def test_monotonic_equity_has_no_drawdown():
    assert st.max_drawdown(np.array([100.0, 110.0, 120.0])) == 0.0


def test_block_bootstrap_returns_one_drawdown_per_sample():
    pnls = normal(200, 10.0, 100.0, seed=5)
    out = st.block_bootstrap_drawdowns(pnls, 10_000.0, n_samples=200, seed=1)
    assert out.shape == (200,)
    assert np.all(out >= 0)


def test_block_bootstrap_is_deterministic_under_a_seed():
    pnls = normal(100, 5.0, 50.0, seed=6)
    a = st.block_bootstrap_drawdowns(pnls, 5_000.0, n_samples=100, seed=99)
    b = st.block_bootstrap_drawdowns(pnls, 5_000.0, n_samples=100, seed=99)
    assert np.array_equal(a, b)


def test_larger_blocks_produce_deeper_drawdowns():
    """Losing streaks cluster. Shuffling trade-by-trade destroys that and flatters you."""
    rng = np.random.default_rng(31)
    pnls = np.concatenate([rng.normal(50, 20, 100), rng.normal(-60, 20, 40)])
    small = st.block_bootstrap_drawdowns(pnls, 10_000.0, n_samples=400, block_size=1, seed=2)
    large = st.block_bootstrap_drawdowns(pnls, 10_000.0, n_samples=400, block_size=20, seed=2)
    assert np.median(large) > np.median(small)


# ── randomised entry null ───────────────────────────────────────────────────────
def test_random_entries_in_a_rising_market_are_profitable():
    """The beta trap, quantified: a monkey makes money when everything goes up."""
    prices = np.linspace(100.0, 200.0, 1000)
    out = st.randomised_entry_null(prices, n_trades=20, holding_bars=50,
                                   n_samples=500, long_only=True, seed=8)
    assert np.median(out) > 0


def test_random_long_short_entries_are_centred_on_zero():
    prices = np.linspace(100.0, 200.0, 1000)
    out = st.randomised_entry_null(prices, n_trades=20, holding_bars=50,
                                   n_samples=2000, long_only=False, seed=9)
    assert abs(np.median(out)) < abs(np.median(
        st.randomised_entry_null(prices, 20, 50, n_samples=2000, long_only=True, seed=9)))


def test_randomised_entry_handles_series_shorter_than_holding_period():
    assert st.randomised_entry_null(np.array([1.0, 2.0]), 5, 50).size == 0


def test_percentile_of_locates_a_value_in_a_distribution():
    dist = np.arange(100, dtype=float)
    assert st.percentile_of(50.0, dist) == pytest.approx(0.51)
    assert st.percentile_of(-1.0, dist) == 0.0
    assert st.percentile_of(200.0, dist) == 1.0


def test_percentile_of_empty_distribution_is_nan():
    assert math.isnan(st.percentile_of(1.0, np.zeros(0)))


# ── minimum track record ────────────────────────────────────────────────────────
def test_minimum_track_record_shrinks_as_sharpe_grows():
    weak = normal(1000, 0.0005, 0.01, seed=12)
    strong = normal(1000, 0.004, 0.01, seed=12)
    assert st.minimum_track_record_length(strong) < st.minimum_track_record_length(weak)


def test_minimum_track_record_is_infinite_for_a_losing_strategy():
    assert st.minimum_track_record_length(normal(500, -0.001, 0.01)) == float("inf")
