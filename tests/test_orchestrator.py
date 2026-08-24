"""The falsification battery.

The property that matters most: **a test that cannot run must FAIL, never skip.** A battery
where "insufficient data" reads as a pass produces a verdict people trust and shouldn't.
"""
from __future__ import annotations

import re

import numpy as np
import pytest

from supercortex.costs.model import CostModel, Trade
from supercortex.falsify import benchmark as bench
from supercortex.falsify import orchestrator as orch

DAY = 86_400_000
MODEL = CostModel()


def thresholds() -> dict:
    return orch.load_thresholds()


def make_trades(n: int, win_rate: float, win: float, loss: float,
                price: float = 100.0, qty: float = 10.0,
                start_day: int = 0) -> list[Trade]:
    wins = round(n * win_rate)
    out = []
    for i in range(n):
        move = win if i < wins else -loss
        t0 = (start_day + i * 30) * DAY
        out.append(Trade(t0, t0 + DAY, 1, qty, price, price * (1 + move)))
    return out


def ctx(trades, prices=None, trials=1, **kw) -> orch.Context:
    if prices is None:
        prices = np.linspace(100.0, 110.0, 500)
    timestamps = np.linspace(0, len(prices) * 3_600_000, len(prices))
    return orch.Context(
        experiment_id="EXP-000009", trades=trades, prices=prices,
        timestamps=timestamps, starting_equity=100_000.0, model=MODEL,
        trials_consumed=trials, **kw,
    )


# ── thresholds are not optional ─────────────────────────────────────────────────
def test_missing_thresholds_file_refuses_to_run(tmp_path):
    with pytest.raises(RuntimeError, match="rubber stamp"):
        orch.load_thresholds(tmp_path / "absent.yaml")


def test_thresholds_are_version_stamped():
    assert thresholds()["version"] >= 1


# ── a test that cannot run is a failure, never a skip ───────────────────────────
def test_walk_forward_fails_on_too_few_trades():
    result = orch.test_walk_forward(ctx(make_trades(4, 0.5, 0.05, 0.02)), thresholds())
    assert not result.passed
    assert "rubber stamp" in result.detail


def test_pbo_fails_without_a_parameter_sweep():
    result = orch.test_pbo(ctx(make_trades(40, 0.5, 0.05, 0.02)), thresholds())
    assert not result.passed
    assert "no parameter sweep" in result.detail


def test_deflated_sharpe_fails_on_too_few_trades():
    result = orch.test_deflated_sharpe(ctx(make_trades(2, 1.0, 0.05, 0.0)), thresholds())
    assert not result.passed
    assert "not estimable" in result.detail


def test_monte_carlo_fails_on_too_few_trades():
    result = orch.test_monte_carlo_drawdown(ctx(make_trades(5, 0.6, 0.05, 0.02)), thresholds())
    assert not result.passed


def test_benchmark_fails_without_prices():
    c = ctx(make_trades(40, 0.5, 0.05, 0.02), prices=np.array([100.0]))
    assert not orch.test_benchmark(c, thresholds()).passed


# ── individual tests behave as designed ─────────────────────────────────────────
def test_cost_ladder_kills_a_thin_edge():
    """A strategy whose edge is smaller than 2x its frictions must die on the ladder."""
    thin = make_trades(60, 0.55, 0.0025, 0.0020)
    result = orch.test_cost_ladder(ctx(thin), thresholds())
    assert not result.passed
    assert "Dies at 2x" in result.detail


def test_cost_ladder_passes_a_fat_edge():
    fat = make_trades(60, 0.40, 0.08, 0.02)
    assert orch.test_cost_ladder(ctx(fat), thresholds()).passed


def test_cost_ladder_costs_no_trials():
    assert orch.test_cost_ladder(ctx(make_trades(60, 0.4, 0.08, 0.02)),
                                 thresholds()).trials_spent == 0


def test_regime_attribution_kills_a_single_year_result():
    """All the profit in one year is not a strategy, it is a regime."""
    trades = []
    for i in range(20):                       # 2020: big winners
        t0 = 1_577_836_800_000 + i * DAY
        trades.append(Trade(t0, t0 + DAY, 1, 10.0, 100.0, 130.0))
    for i in range(20):                       # 2023: flat
        t0 = 1_672_531_200_000 + i * DAY
        trades.append(Trade(t0, t0 + DAY, 1, 10.0, 100.0, 100.2))
    result = orch.test_regime_attribution(ctx(trades), thresholds())
    assert not result.passed
    assert result.statistic > 0.5


def test_regime_attribution_passes_when_spread_across_years():
    trades = []
    for year_start in (1_577_836_800_000, 1_609_459_200_000, 1_640_995_200_000):
        for i in range(15):
            t0 = year_start + i * DAY
            trades.append(Trade(t0, t0 + DAY, 1, 10.0, 100.0, 108.0))
    assert orch.test_regime_attribution(ctx(trades), thresholds()).passed


def test_post_tax_gate_reflects_the_conservative_regime():
    losing = make_trades(60, 0.55, 0.004, 0.004)
    assert not orch.test_post_tax(ctx(losing), thresholds()).passed
    winning = make_trades(60, 0.35, 0.12, 0.02)
    assert orch.test_post_tax(ctx(winning), thresholds()).passed


def test_deflated_sharpe_reads_the_registry_trial_count():
    """The same trades, searched harder, must score worse. This is the whole point."""
    trades = make_trades(80, 0.45, 0.05, 0.03)
    few = orch.test_deflated_sharpe(ctx(trades, trials=2), thresholds())
    many = orch.test_deflated_sharpe(ctx(trades, trials=5000), thresholds())
    assert few.statistic > many.statistic
    assert "2 registered trials" in few.detail


def test_pbo_runs_when_a_sweep_is_supplied():
    rng = np.random.default_rng(3)
    sweep = rng.normal(0, 0.01, size=(600, 10))
    sweep[:, 0] += 0.006
    result = orch.test_pbo(ctx(make_trades(40, 0.5, 0.05, 0.02), sweep_returns=sweep),
                           thresholds())
    assert result.passed


# ── the benchmark gate ──────────────────────────────────────────────────────────
def test_benchmark_kills_a_strategy_beaten_by_holding():
    """The finding that created this gate: clears every statistical bar, loses to holding."""
    prices = np.linspace(100.0, 800.0, 2000)          # the asset went up 8x
    trades = make_trades(40, 0.35, 0.06, 0.02)         # a decent-looking strategy
    result = orch.test_benchmark(ctx(trades, prices=prices), thresholds())
    assert not result.passed
    assert "buy-and-hold" in result.detail


def test_benchmark_passes_when_the_strategy_genuinely_wins():
    prices = np.linspace(100.0, 101.0, 2000)           # the asset went nowhere
    trades = make_trades(40, 0.4, 0.10, 0.02)
    assert orch.test_benchmark(ctx(trades, prices=prices), thresholds()).passed


def test_portfolio_argument_does_not_satisfy_the_gate():
    """Winning per unit of risk while losing outright is a leverage question, not a pass."""
    prices = np.linspace(100.0, 800.0, 2000)
    trades = make_trades(30, 0.5, 0.02, 0.005)         # small, steady, low drawdown
    c = bench.compare(trades, prices, np.linspace(0, 2000 * 3_600_000, 2000),
                      100_000.0, MODEL)
    if c.portfolio_argument:
        result = orch.test_benchmark(ctx(trades, prices=prices), thresholds())
        assert not result.passed
        assert "PORTFOLIO argument" in result.detail


def test_buy_and_hold_needs_at_least_two_prices():
    with pytest.raises(ValueError, match="at least two prices"):
        bench.buy_and_hold(np.array([100.0]), np.array([0]), 100_000.0, MODEL)


def test_equity_curve_starts_at_the_starting_equity():
    curve = bench.strategy_equity_curve(make_trades(10, 0.5, 0.05, 0.02), 100_000.0, MODEL)
    assert curve[0] == 100_000.0
    assert curve.size == 11


# ── the verdict ─────────────────────────────────────────────────────────────────
def test_battery_fails_if_any_single_test_fails():
    verdict = orch.run_static_battery(ctx(make_trades(60, 0.4, 0.08, 0.02)))
    assert not verdict.passed                     # PBO cannot run without a sweep
    assert any(r.name == "6-pbo" for r in verdict.failures)


def test_verdict_names_every_failure():
    verdict = orch.run_static_battery(ctx(make_trades(6, 0.5, 0.05, 0.02)))
    assert not verdict.passed
    assert len(verdict.failures) >= 3
    assert "failed" in verdict.summary()


def test_battery_covers_every_static_test():
    verdict = orch.run_static_battery(ctx(make_trades(60, 0.4, 0.08, 0.02)))
    assert len(verdict.results) == len(orch.STATIC_TESTS) == 9
    assert len({r.name for r in verdict.results}) == 9


def test_dynamic_cost_is_known_before_spending_it():
    """The battery must say what it will cost, because that cost inflates the very
    multiple-testing penalty it is about to apply."""
    assert orch.dynamic_trial_cost(channels=2) == 6 + 1 + 5 + 1
    assert orch.dynamic_trial_cost(channels=1) == 3 + 1 + 5 + 1


def test_sharpe_variance_source_is_labelled_honestly():
    c = ctx(make_trades(40, 0.5, 0.05, 0.02))
    _, source = c.effective_sharpe_variance()
    assert "asymptotic" in source
    c.sharpe_variance = 0.02
    _, source = c.effective_sharpe_variance()
    assert "measured" in source


# ══════════════════════════════════════════════════════════════════════════════
# Thresholds are a boundary, and a battery cannot judge its own configuration
# ══════════════════════════════════════════════════════════════════════════════
# `yaml.safe_load` returns whatever the document says, and `load_thresholds` returned it
# with `or {}` behind it. So an empty, truncated or half-written thresholds file reached
# the tests, where each one indexed the section it wanted and died on a KeyError deep
# inside a test — or worse, `benchmark` used `.get(..., True)` and a missing section
# silently became a default. A battery that runs on partial configuration is exactly the
# rubber stamp this module exists to avoid, so the file is now checked once, at the door.
#
# Never touches falsify/thresholds.yaml. Every case writes its own document under tmp_path.
VALID_THRESHOLDS = """
version: 1
set_on: 2026-08-24
lookahead:
  material_change: 0.10
cost_ladder:
  survive_multiple: 2.0
parameter_neighbourhood:
  min_median_fraction_of_peak: 0.50
walk_forward:
  min_positive_window_fraction: 0.60
  min_windows: 5
  embargo_fraction: 0.01
pbo:
  max: 0.35
deflated_sharpe:
  min: 0.95
randomised_entry:
  min_percentile: 0.95
monte_carlo:
  max_observed_drawdown_percentile: 0.95
regime:
  max_single_year_pnl_share: 0.50
post_tax:
  require_positive_under: vda_strict
benchmark:
  must_beat_buy_and_hold_after_tax: true
  min_excess_fraction: 0.0
reproducibility:
  require_bit_identical: true
"""


def _thresholds_file(tmp_path, body: str):
    path = tmp_path / "thresholds.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _without(section: str, body: str = VALID_THRESHOLDS) -> str:
    """The valid document with one whole section removed."""
    lines, out, dropping = body.splitlines(), [], False
    for line in lines:
        if line.startswith(f"{section}:"):
            dropping = True
            continue
        if dropping and (not line or not line.startswith((" ", "\t"))):
            dropping = False
        if not dropping:
            out.append(line)
    return "\n".join(out) + "\n"


def test_a_complete_document_loads(tmp_path):
    cfg = orch.load_thresholds(_thresholds_file(tmp_path, VALID_THRESHOLDS))
    assert cfg["version"] == 1
    assert cfg["pbo"]["max"] == 0.35
    assert cfg["benchmark"]["must_beat_buy_and_hold_after_tax"] is True


def test_sections_the_static_battery_does_not_read_are_preserved(tmp_path):
    """Validation checks the document; it does not rewrite it."""
    cfg = orch.load_thresholds(_thresholds_file(tmp_path, VALID_THRESHOLDS))
    assert "lookahead" in cfg
    assert "reproducibility" in cfg
    assert "set_on" in cfg


def test_a_complete_document_drives_the_battery_identically(tmp_path):
    """The whole point of preserving behavior: same thresholds, same verdict."""
    from_file = orch.run_static_battery(
        ctx(make_trades(60, 0.4, 0.08, 0.02)),
        orch.load_thresholds(_thresholds_file(tmp_path, VALID_THRESHOLDS)),
    )
    from_repo = orch.run_static_battery(ctx(make_trades(60, 0.4, 0.08, 0.02)))
    assert [(r.name, r.passed, r.threshold) for r in from_file.results] == \
           [(r.name, r.passed, r.threshold) for r in from_repo.results]


# ── every way the document can be unusable ──────────────────────────────────────
def test_malformed_yaml_is_a_thresholds_error(tmp_path):
    with pytest.raises(orch.ThresholdsError, match="parse"):
        orch.load_thresholds(_thresholds_file(tmp_path, "pbo:\n  max: [1, 2\n"))


def test_empty_document_is_a_thresholds_error(tmp_path):
    """No thresholds at all is not "use the defaults"; there are no defaults."""
    with pytest.raises(orch.ThresholdsError, match="empty"):
        orch.load_thresholds(_thresholds_file(tmp_path, ""))


def test_comment_only_document_is_a_thresholds_error(tmp_path):
    with pytest.raises(orch.ThresholdsError, match="empty"):
        orch.load_thresholds(_thresholds_file(tmp_path, "# thresholds coming soon\n"))


@pytest.mark.parametrize("body", ["- pbo\n- regime\n", "0.35\n", "hello\n", "true\n"])
def test_non_mapping_top_level_is_a_thresholds_error(tmp_path, body: str):
    with pytest.raises(orch.ThresholdsError, match="mapping"):
        orch.load_thresholds(_thresholds_file(tmp_path, body))


@pytest.mark.parametrize("section", [
    "cost_ladder", "walk_forward", "pbo", "deflated_sharpe", "randomised_entry",
    "monte_carlo", "regime", "post_tax", "benchmark",
])
def test_a_missing_section_names_that_section(tmp_path, section: str):
    with pytest.raises(orch.ThresholdsError, match=section):
        orch.load_thresholds(_thresholds_file(tmp_path, _without(section)))


def test_a_missing_version_is_a_thresholds_error(tmp_path):
    body = VALID_THRESHOLDS.replace("version: 1\n", "")
    with pytest.raises(orch.ThresholdsError, match="version"):
        orch.load_thresholds(_thresholds_file(tmp_path, body))


@pytest.mark.parametrize("replacement", ["pbo: 0.35", "pbo:\n  - max: 0.35", "pbo: null"])
def test_a_section_of_the_wrong_shape_is_a_thresholds_error(tmp_path, replacement: str):
    body = VALID_THRESHOLDS.replace("pbo:\n  max: 0.35", replacement)
    with pytest.raises(orch.ThresholdsError, match="pbo"):
        orch.load_thresholds(_thresholds_file(tmp_path, body))


def test_a_missing_field_names_section_and_field(tmp_path):
    body = VALID_THRESHOLDS.replace("  min_windows: 5\n", "")
    with pytest.raises(orch.ThresholdsError, match=r"walk_forward\.min_windows"):
        orch.load_thresholds(_thresholds_file(tmp_path, body))


@pytest.mark.parametrize(("old", "new", "names"), [
    ("  min_windows: 5", "  min_windows: five", "walk_forward.min_windows"),
    ("  min_windows: 5", "  min_windows: 5.5", "walk_forward.min_windows"),
    ("  max: 0.35", "  max: sometimes", "pbo.max"),
    ("  survive_multiple: 2.0", "  survive_multiple: null", "cost_ladder.survive_multiple"),
    ("  must_beat_buy_and_hold_after_tax: true",
     "  must_beat_buy_and_hold_after_tax: yes please",
     "benchmark.must_beat_buy_and_hold_after_tax"),
    ("  require_positive_under: vda_strict", "  require_positive_under: 3",
     "post_tax.require_positive_under"),
])
def test_a_field_of_the_wrong_type_names_the_field(tmp_path, old, new, names):
    body = VALID_THRESHOLDS.replace(old, new)
    with pytest.raises(orch.ThresholdsError, match=re.escape(names)):
        orch.load_thresholds(_thresholds_file(tmp_path, body))


def test_a_boolean_is_not_accepted_where_a_number_is_required(tmp_path):
    """True == 1 in Python. A threshold of `true` is a mistake, not a limit of 1.0."""
    body = VALID_THRESHOLDS.replace("  max: 0.35", "  max: true")
    with pytest.raises(orch.ThresholdsError, match=re.escape("pbo.max")):
        orch.load_thresholds(_thresholds_file(tmp_path, body))


# ── the guarantee those cases add up to ─────────────────────────────────────────
def test_no_failure_escapes_as_a_keyerror_or_parser_error(tmp_path):
    import yaml

    bodies = [
        "", "- a\n", "42\n", "pbo:\n  max: [1, 2\n",
        _without("benchmark"), _without("pbo"),
        VALID_THRESHOLDS.replace("  min_windows: 5", "  min_windows: five"),
    ]
    for body in bodies:
        with pytest.raises(orch.ThresholdsError) as caught:
            orch.load_thresholds(_thresholds_file(tmp_path, body))
        assert not isinstance(caught.value, KeyError | TypeError | yaml.YAMLError)
        assert "thresholds" in str(caught.value).lower()


def test_a_battery_never_runs_on_partial_configuration(tmp_path, monkeypatch):
    """The failure that matters: `benchmark` used to default to enabled when absent, so a
    truncated file produced a full verdict with a gate nobody had configured.

    The battery is entered for real. Passing `load_thresholds(path)` as an argument would
    prove nothing — Python evaluates it before `run_static_battery` is called, so the
    exception would come from the argument and the body would never be reached either way.
    So the module's own threshold path is pointed at the partial file, and every static
    test is replaced by a sentinel that fails the test loudly if anything invokes it.
    """
    invocations: list[str] = []

    def sentinel(context: orch.Context, thresholds: orch.Thresholds) -> orch.TestResult:
        invocations.append("ran")
        raise AssertionError(
            "a static test ran against thresholds that failed validation"
        )

    monkeypatch.setattr(orch, "THRESHOLDS_PATH",
                        _thresholds_file(tmp_path, _without("benchmark")))
    monkeypatch.setattr(orch, "STATIC_TESTS", (sentinel,) * len(orch.STATIC_TESTS))

    with pytest.raises(orch.ThresholdsError, match="benchmark"):
        orch.run_static_battery(ctx(make_trades(60, 0.4, 0.08, 0.02)))
    assert invocations == [], "the battery must refuse before any test executes"


def test_a_valid_file_at_the_module_path_does_reach_the_tests(tmp_path, monkeypatch):
    """The control for the test above: the sentinel is reachable when the file is good, so
    `invocations == []` there means "refused", not "wired up wrong"."""
    invocations: list[str] = []

    def sentinel(context: orch.Context, thresholds: orch.Thresholds) -> orch.TestResult:
        invocations.append("ran")
        return orch.TestResult("sentinel", True, None, None, "reached")

    monkeypatch.setattr(orch, "THRESHOLDS_PATH",
                        _thresholds_file(tmp_path, VALID_THRESHOLDS))
    monkeypatch.setattr(orch, "STATIC_TESTS", (sentinel, sentinel))

    verdict = orch.run_static_battery(ctx(make_trades(60, 0.4, 0.08, 0.02)))
    assert invocations == ["ran", "ran"]
    assert verdict.thresholds_version == 1


def test_thresholds_error_is_a_runtime_error(tmp_path):
    """Existing callers catching RuntimeError keep working."""
    assert issubclass(orch.ThresholdsError, RuntimeError)
    with pytest.raises(RuntimeError):
        orch.load_thresholds(_thresholds_file(tmp_path, "42\n"))
