"""The India cost model, as executable claims.

A bug here is indistinguishable from a lie: it would make an unprofitable strategy look
profitable, survive falsification, and reach live capital. Coverage target is 90%+.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import math
import textwrap
from dataclasses import replace

import pytest

from supercortex.costs.model import (
    CostedResult,
    CostModel,
    ProfitFactorState,
    TaxRegime,
    Trade,
    apply,
    compare_regimes,
    funding_pnl,
    required_profit_factor,
)

DAY = 86_400_000


def trade(pct_move: float, *, direction: int = 1, qty: float = 1.0,
          price: float = 100.0, i: int = 0, funding: float = 0.0) -> Trade:
    return Trade(i * DAY, i * DAY + 3600_000, direction, qty,
                 price, price * (1 + direction * pct_move), funding_pnl=funding)


def series(n: int, win_rate: float, win: float, loss: float, **kw) -> list[Trade]:
    wins = round(n * win_rate)
    return [trade(win if i < wins else -loss, i=i, **kw) for i in range(n)]


# ── trade arithmetic ────────────────────────────────────────────────────────────
def test_long_gross_pnl():
    assert trade(0.10).gross_pnl == pytest.approx(10.0)


def test_short_gross_pnl_is_positive_when_price_falls():
    t = Trade(0, DAY, -1, 1.0, 100.0, 90.0)
    assert t.gross_pnl == pytest.approx(10.0)


def test_turnover_counts_both_legs():
    assert trade(0.10).turnover == pytest.approx(210.0)


@pytest.mark.parametrize("bad", [
    dict(direction=0), dict(qty=0.0), dict(qty=-1.0),
])
def test_invalid_trade_rejected(bad):
    kwargs = dict(entry_ts_ms=0, exit_ts_ms=DAY, direction=1, qty=1.0,
                  entry_price=100.0, exit_price=110.0) | bad
    with pytest.raises(ValueError):
        Trade(**kwargs)


def test_exit_before_entry_rejected():
    with pytest.raises(ValueError, match="exit precedes entry"):
        Trade(DAY, 0, 1, 1.0, 100.0, 110.0)


# ── the closed-form threshold ───────────────────────────────────────────────────
def test_break_even_profit_factor_matches_the_derivation():
    """PF_min = 1/(1-r). At 30% + 4% cess, r = 0.312 and PF_min = 1.4535."""
    model = CostModel()
    assert model.effective_tax_rate == pytest.approx(0.312)
    assert model.break_even_profit_factor == pytest.approx(1 / 0.688, rel=1e-9)
    assert model.break_even_profit_factor == pytest.approx(1.4535, abs=1e-4)


def test_loss_offset_regime_has_a_floor_of_one():
    model = CostModel(regime=TaxRegime.BUSINESS_INCOME)
    assert model.break_even_profit_factor == 1.0


def test_at_the_threshold_tax_exactly_consumes_profit():
    """The derivation, verified end to end with frictions switched off."""
    free = CostModel(taker_fee_bps=0, maker_fee_bps=0, slippage_bps=0,
                     half_spread_bps=0, tds_rate=0.0)
    pf = free.break_even_profit_factor
    # one winner of size pf, one loser of size 1
    trades = [trade(pf / 100), trade(-0.01, i=1)]
    result = apply(trades, free)
    assert result.profit_factor_gross == pytest.approx(pf, rel=1e-6)
    assert result.net_after_tax == pytest.approx(0.0, abs=1e-9)


# ── the trap ────────────────────────────────────────────────────────────────────
def test_losing_year_still_owes_tax_under_no_offset():
    """The signature of s.115BBH: every winning disposal is assessed on its own."""
    trades = [trade(0.05), trade(-0.10, i=1)]          # net -5, clearly a losing year
    result = apply(trades, CostModel())
    assert result.net_of_costs < 0
    assert result.tax_total > 0
    assert result.taxed_while_losing


def test_business_income_regime_owes_nothing_on_a_losing_year():
    trades = [trade(0.05), trade(-0.10, i=1)]
    result = apply(trades, CostModel(regime=TaxRegime.BUSINESS_INCOME))
    assert result.tax_total == 0.0
    assert not result.taxed_while_losing


def test_no_offset_taxes_more_than_offset_on_the_same_trades():
    trades = series(50, 0.5, 0.02, 0.015)
    strict = apply(trades, CostModel())
    business = apply(trades, CostModel(regime=TaxRegime.BUSINESS_INCOME))
    assert strict.tax_total > business.tax_total
    assert strict.net_after_tax < business.net_after_tax


# ── TDS is a prepayment, not an extra tax ───────────────────────────────────────
def test_recoverable_tds_is_credited_not_added():
    """Treating TDS as additive double-counts it — it offsets the final liability."""
    trades = series(20, 0.6, 0.05, 0.02)
    result = apply(trades, CostModel())
    assert result.tds_withheld > 0
    assert result.tds_credited == pytest.approx(min(result.tds_withheld, result.tax_total))
    assert result.tds_unrecovered == 0.0
    assert result.tax_payable == pytest.approx(result.tax_total - result.tds_credited)


def test_recoverable_tds_costs_only_the_carry():
    """With full credit, the only permanent loss is the idle capital."""
    trades = series(20, 0.6, 0.05, 0.02)
    model = CostModel()
    with_tds = apply(trades, model)
    without = apply(trades, replace(model, tds_rate=0.0))
    expected_drag = with_tds.tds_withheld * model.capital_carry_rate * model.tds_refund_lag_years
    assert without.net_after_tax - with_tds.net_after_tax == pytest.approx(expected_drag)


def test_unrecoverable_tds_is_a_permanent_loss():
    """No Form 16A, no credit — a venue-selection consequence, not a tax one."""
    trades = series(20, 0.6, 0.05, 0.02)
    recoverable = apply(trades, CostModel())
    stranded = apply(trades, CostModel(tds_recoverable=False))
    assert stranded.tds_unrecovered == pytest.approx(stranded.tds_withheld)
    assert stranded.tds_carry_cost == 0.0
    assert stranded.net_after_tax < recoverable.net_after_tax


def test_tds_scales_with_turnover_not_profit():
    """Two trades of identical PnL but different notional owe different TDS."""
    small = apply([trade(0.10, qty=1.0, price=100.0)], CostModel())
    large = apply([Trade(0, DAY, 1, 0.1, 1000.0, 1100.0)], CostModel())
    assert small.gross_pnl == pytest.approx(large.gross_pnl)
    assert small.tds_withheld == pytest.approx(large.tds_withheld)  # same notional here
    bigger = apply([Trade(0, DAY, 1, 1.0, 1000.0, 1010.0)], CostModel())
    assert bigger.gross_pnl < small.gross_pnl
    assert bigger.tds_withheld > small.tds_withheld  # more turnover, less profit


def test_no_tds_regime_withholds_nothing():
    result = apply(series(10, 0.6, 0.03, 0.02), CostModel(regime=TaxRegime.VDA_NO_TDS))
    assert result.tds_withheld == 0.0


# ── execution frictions ─────────────────────────────────────────────────────────
def test_maker_is_cheaper_than_taker():
    t = trade(0.10)
    model = CostModel()
    maker = replace(t, entry_taker=False, exit_taker=False)
    assert model.execution_cost(maker) < model.execution_cost(t)


def test_execution_cost_is_proportional_to_turnover():
    model = CostModel()
    one = model.execution_cost(Trade(0, DAY, 1, 1.0, 100.0, 100.0))
    two = model.execution_cost(Trade(0, DAY, 1, 2.0, 100.0, 100.0))
    assert two == pytest.approx(2 * one)


def test_round_trip_friction_is_seventeen_bps_by_default():
    model = CostModel()
    flat = Trade(0, DAY, 1, 1.0, 100.0, 100.0)          # no price move
    assert model.execution_cost(flat) / flat.turnover * 1e4 == pytest.approx(8.5)


# ── funding ─────────────────────────────────────────────────────────────────────
def test_long_pays_when_funding_is_positive():
    """The sign convention everyone gets wrong: positive rate, longs pay shorts."""
    assert funding_pnl(1, 1.0, [(0, 0.0001, 50_000.0)]) == pytest.approx(-5.0)


def test_short_receives_when_funding_is_positive():
    assert funding_pnl(-1, 1.0, [(0, 0.0001, 50_000.0)]) == pytest.approx(5.0)


def test_funding_accumulates_across_events():
    events = [(0, 0.0001, 50_000.0), (1, 0.0002, 50_000.0)]
    assert funding_pnl(1, 1.0, events) == pytest.approx(-15.0)


def test_funding_received_is_taxable_income():
    model = CostModel()
    flat_with_funding = Trade(0, DAY, 1, 1.0, 100.0, 100.0, funding_pnl=10.0)
    assert model.taxable_base(flat_with_funding) == pytest.approx(10.0)


def test_funding_paid_is_not_deductible_by_default():
    """Conservative reading of s.115BBH: you can be taxed on a price gain while
    separately bearing the funding that consumed it."""
    model = CostModel()
    t = trade(0.10, funding=-5.0)
    assert model.taxable_base(t) == pytest.approx(10.0)   # funding paid excluded
    assert model.net_of_costs(t) < 10.0                   # but it still costs you


def test_funding_paid_is_deductible_when_allowed():
    model = CostModel(funding_deductible=True)
    assert model.taxable_base(trade(0.10, funding=-5.0)) == pytest.approx(5.0)


# ── fee deductibility ───────────────────────────────────────────────────────────
def test_fees_not_deductible_by_default():
    model = CostModel()
    t = trade(0.10)
    assert model.taxable_base(t) == pytest.approx(t.gross_pnl)


def test_fees_deductible_lowers_the_base():
    model = CostModel(fees_deductible=True)
    t = trade(0.10)
    assert model.taxable_base(t) < t.gross_pnl


# ── aggregation ─────────────────────────────────────────────────────────────────
def test_empty_trade_list_is_all_zeros():
    result = apply([], CostModel())
    assert result.n_trades == 0
    assert result.net_after_tax == 0.0
    assert result.win_rate == 0.0


def test_four_numbers_are_monotonically_worse():
    """Each rung must be worse than the last, or the ladder misleads."""
    result = apply(series(40, 0.4, 0.06, 0.02), CostModel())
    labels = [label for label, _ in result.four_numbers()]
    values = [v for _label, v in result.four_numbers()]
    assert labels == ["gross", "net of costs", "net of tax", "net of TDS drag"]
    assert values == sorted(values, reverse=True)


def test_mid_year_cash_position_can_be_below_the_final_result():
    """TDS leaves the account immediately and comes back after assessment, so the
    cash position during the year is worse than the year's economic outcome. This is
    a financing constraint, not a cost, and conflating the two overstates the burden."""
    result = apply(series(40, 0.4, 0.06, 0.02), CostModel())
    assert result.net_of_tds < result.net_after_tax
    assert result.tds_credited > 0


def test_profit_factor_matches_hand_computation():
    trades = [trade(0.03), trade(0.03, i=1), trade(-0.02, i=2)]
    result = apply(trades, CostModel())
    assert result.profit_factor_gross == pytest.approx(6.0 / 2.0)
    assert result.n_wins == 2
    assert result.win_rate == pytest.approx(2 / 3)


def test_realised_tax_rate_is_infinite_when_losing_but_taxed():
    result = apply([trade(0.05), trade(-0.10, i=1)], CostModel())
    assert math.isinf(result.realised_tax_rate)


def test_compare_regimes_covers_every_regime():
    results = compare_regimes(series(20, 0.5, 0.03, 0.02), CostModel())
    assert set(results) == set(TaxRegime)
    assert results[TaxRegime.BUSINESS_INCOME].net_after_tax > \
           results[TaxRegime.VDA_STRICT].net_after_tax


# ── the required-profit-factor solver ───────────────────────────────────────────
def test_required_pf_is_scale_invariant_in_trade_count():
    """With fixed notional per trade, everything scales with n — so the requirement
    does not. The axis that matters is move size, not intensity."""
    model = CostModel()
    a = required_profit_factor(model, 20, loss_move=0.01)
    b = required_profit_factor(model, 500, loss_move=0.01)
    assert a == pytest.approx(b, rel=1e-3)


def test_required_pf_falls_as_the_move_grows():
    """Frictions are charged per round trip regardless of how far price moved, so a
    bigger edge per trade dilutes them."""
    model = CostModel()
    small = required_profit_factor(model, 100, loss_move=0.002)
    large = required_profit_factor(model, 100, loss_move=0.10)
    assert small > large
    assert large > model.break_even_profit_factor  # never below the tax-only floor


def test_required_pf_is_lower_under_permissive_regimes():
    strict = required_profit_factor(CostModel(), 100, loss_move=0.015)
    business = required_profit_factor(
        CostModel(regime=TaxRegime.BUSINESS_INCOME), 100, loss_move=0.015
    )
    assert business < strict


def test_hft_horizon_is_unreachable():
    """A 5 bp edge against 17 bp of round-trip friction needs a profit factor no
    strategy achieves. This is the quantitative form of 'market making: No'."""
    pf = required_profit_factor(CostModel(), 100, loss_move=0.0005)
    assert pf > 10.0


def test_tds_is_withheld_even_on_a_losing_disposal():
    """s.194S is charged on the transfer, not on the gain. Sell at a loss and 1% still
    leaves the account. There is no income-tax liability to credit it against, so it
    sits idle until the refund is assessed — a losing year has a financing cost."""
    result = apply([trade(-0.05)], CostModel())
    assert result.net_of_costs < 0
    assert result.tax_total == 0.0          # no gain, no liability
    assert result.tds_withheld > 0          # withheld anyway
    assert result.tds_credited == 0.0       # nothing to credit it against
    assert result.tds_carry_cost > 0        # so it costs carry
    assert not result.taxed_while_losing    # taxed, no; charged, yes


def test_burden_is_zero_only_when_nothing_is_withheld_at_all():
    """The one case where a losing year is genuinely free: no gain and no TDS."""
    result = apply([trade(-0.05)], CostModel(regime=TaxRegime.VDA_NO_TDS))
    assert result.net_of_costs < 0
    assert result.total_tax_burden == 0.0
    assert result.realised_tax_rate == 0.0


def test_survives_tax_flag():
    assert apply(series(40, 0.3, 0.10, 0.02), CostModel()).survives_tax
    assert not apply(series(40, 0.55, 0.004, 0.004), CostModel()).survives_tax


def test_unreachable_intensity_returns_infinity():
    """When frictions exceed any achievable edge, no winner size rescues the profile."""
    brutal = CostModel(taker_fee_bps=500.0, slippage_bps=500.0)
    assert required_profit_factor(brutal, 50, loss_move=0.001) == float("inf")


def test_realised_tax_rate_on_a_profitable_year():
    """The ordinary path: burden as a fraction of pre-tax profit. Well above the
    headline 31.2% because TDS carry and non-deductible fees both sit on top."""
    result = apply(series(40, 0.3, 0.10, 0.02), CostModel())
    assert result.net_of_costs > 0
    assert result.realised_tax_rate > CostModel().effective_tax_rate
    assert math.isfinite(result.realised_tax_rate)


# ══════════════════════════════════════════════════════════════════════════════
# the empty result, constructed by name
# ══════════════════════════════════════════════════════════════════════════════
# `apply([])` built its CostedResult from 22 positional arguments. Every one was a bare
# `0.0` in a run of sixteen, so a field inserted, removed or reordered in the dataclass
# would shift the rest silently and still type-check. These tests pin the value of every
# field so the construction can be rewritten by name without changing a single number.
EMPTY_ZEROS = (
    "turnover", "gross_pnl", "execution_costs", "funding_pnl", "net_of_costs",
    "tds_withheld", "tds_credited", "tds_unrecovered", "tds_carry_cost", "net_of_tds",
    "tax_total", "tax_payable", "net_after_tax", "gross_wins", "gross_losses",
    "profit_factor_gross", "profit_factor_net", "net_wins", "net_losses",
)


@pytest.mark.parametrize("model", [
    CostModel(),
    CostModel(regime=TaxRegime.BUSINESS_INCOME),
    CostModel(regime=TaxRegime.VDA_NO_TDS, taker_fee_bps=9.0, slippage_bps=7.0),
])
def test_an_empty_result_carries_the_model_it_was_built_from(model):
    result = apply([], model)
    assert result.regime is model.regime
    assert result.cost_model_version == model.version
    assert result.break_even_profit_factor == model.break_even_profit_factor


@pytest.mark.parametrize("field", EMPTY_ZEROS)
def test_every_numeric_field_of_an_empty_result_is_zero(field):
    assert getattr(apply([], CostModel()), field) == 0.0


def test_the_empty_result_has_no_trades_and_no_wins():
    result = apply([], CostModel())
    assert result.n_trades == 0
    assert result.n_wins == 0
    assert result.win_rate == 0.0


def test_the_empty_result_is_undefined_and_clears_nothing():
    result = apply([], CostModel())
    assert result.profit_factor_gross_state is ProfitFactorState.UNDEFINED
    assert result.profit_factor_net_state is ProfitFactorState.UNDEFINED
    assert result.clears_break_even is False


def test_every_declared_field_of_an_empty_result_is_pinned_by_these_tests():
    """A field added to CostedResult without a value here is a gap in this regression."""
    covered = set(EMPTY_ZEROS) | {
        "regime", "cost_model_version", "break_even_profit_factor", "n_trades", "n_wins",
    }
    declared = {f.name for f in dataclasses.fields(apply([], CostModel()))}
    assert declared == covered, declared ^ covered


def _empty_branch_construction() -> ast.Call:
    """The `CostedResult(...)` call inside `if not trades:`, as a syntax node.

    Read structurally rather than as text: a substring search for `regime=` passes on a
    call that also has twenty positional arguments, which is the exact defect this test
    exists to prevent.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(apply)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
                and isinstance(test.operand, ast.Name)
                and test.operand.id == "trades"):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                    and inner.func.id == "CostedResult"):
                return inner
    raise AssertionError("no CostedResult(...) call found in the `if not trades:` branch")


def test_the_empty_result_takes_no_positional_arguments():
    """Twenty-two positional arguments, sixteen of them a bare `0.0`, meant a field
    inserted or reordered in CostedResult shifted every value after it - silently, and
    still type-checking."""
    call = _empty_branch_construction()
    assert call.args == [], f"{len(call.args)} positional argument(s) reintroduced"
    assert not any(keyword.arg is None for keyword in call.keywords), \
        "**kwargs unpacking hides which fields are set"


def test_the_empty_result_names_every_declared_field():
    call = _empty_branch_construction()
    passed = {keyword.arg for keyword in call.keywords}
    declared = {f.name for f in dataclasses.fields(CostedResult)}
    assert passed == declared, f"missing {declared - passed}, unknown {passed - declared}"
