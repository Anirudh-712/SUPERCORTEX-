"""Profit factor has three states, and only one of them is a number.

Strict JSON was the right call; classifying a legitimate result as a technical error was
not. An all-winning trial is a real outcome — a rare and suspicious one that the battery
should attack, not an artifact the desk throws away. A zero-trade trial is a valid negative
result: the strategy declined to trade, which is information.

    finite      losses > 0                  -> a number
    unbounded   wins > 0 and losses == 0    -> null, tagged `unbounded`
    undefined   wins == 0 and losses == 0   -> null, tagged `undefined`

The tag is explicit rather than encoded in a sentinel: `1e308` would be a number nobody
could distinguish from a real one, and `allow_nan=True` would put `Infinity` back into a
document that claims to be JSON.
"""
from __future__ import annotations

import json

import pytest

from supercortex.costs.model import (
    CostModel,
    ProfitFactorState,
    Trade,
    apply,
    format_profit_factor,
    profit_factor_json,
)

WIN = Trade(0, 10, 1, 1.0, 100.0, 101.0)
LOSS = Trade(20, 30, 1, 1.0, 100.0, 99.0)
FEE_FREE = CostModel(taker_fee_bps=0.0, maker_fee_bps=0.0, slippage_bps=0.0,
                     half_spread_bps=0.0)


# ── classification ──────────────────────────────────────────────────────────────
def test_a_trial_with_losses_is_finite():
    result = apply([WIN, LOSS], FEE_FREE)
    assert result.profit_factor_gross_state is ProfitFactorState.FINITE
    assert result.profit_factor_gross == pytest.approx(1.0)


def test_an_all_winning_trial_is_unbounded_not_infinite():
    result = apply([WIN], FEE_FREE)
    assert result.profit_factor_gross_state is ProfitFactorState.UNBOUNDED


def test_a_zero_trade_trial_is_undefined():
    result = apply([], FEE_FREE)
    assert result.profit_factor_gross_state is ProfitFactorState.UNDEFINED
    assert result.profit_factor_net_state is ProfitFactorState.UNDEFINED
    assert result.n_trades == 0


def test_an_all_losing_trial_is_finite_at_zero():
    """0.0 is a real profit factor and must not be confused with undefined."""
    result = apply([LOSS], FEE_FREE)
    assert result.profit_factor_gross_state is ProfitFactorState.FINITE
    assert result.profit_factor_gross == 0.0


def test_net_state_is_classified_separately_from_gross():
    """Costs can turn a winning trade into a net loss, so the two states can differ."""
    expensive = CostModel(taker_fee_bps=500.0)
    result = apply([WIN], expensive)
    assert result.profit_factor_gross_state is ProfitFactorState.UNBOUNDED
    assert result.profit_factor_net_state is ProfitFactorState.FINITE


# ── clears_break_even, per state ────────────────────────────────────────────────
def test_unbounded_clears_break_even():
    assert apply([WIN], FEE_FREE).clears_break_even is True


def test_undefined_does_not_clear_break_even():
    """No trades is not a strategy that beat the tax floor."""
    assert apply([], FEE_FREE).clears_break_even is False


def test_finite_clears_break_even_by_comparison_as_before():
    fat = apply([Trade(0, 10, 1, 1.0, 100.0, 200.0), LOSS], FEE_FREE)
    thin = apply([Trade(0, 10, 1, 1.0, 100.0, 100.5), LOSS], FEE_FREE)
    assert fat.clears_break_even is True
    assert thin.clears_break_even is False


# ── JSON representation ─────────────────────────────────────────────────────────
def test_finite_serialises_as_the_number_it_always_was():
    result = apply([WIN, LOSS], FEE_FREE)
    assert profit_factor_json(result.profit_factor_gross,
                              result.profit_factor_gross_state) == pytest.approx(1.0)


@pytest.mark.parametrize("state", [ProfitFactorState.UNBOUNDED,
                                   ProfitFactorState.UNDEFINED])
def test_non_finite_states_serialise_as_null(state):
    assert profit_factor_json(float("inf"), state) is None


def test_every_state_survives_strict_json():
    for trades in ([WIN, LOSS], [WIN], []):
        result = apply(trades, FEE_FREE)
        payload = {
            "profit_factor_gross": profit_factor_json(
                result.profit_factor_gross, result.profit_factor_gross_state),
            "profit_factor_gross_state": str(result.profit_factor_gross_state),
        }
        json.dumps(payload, allow_nan=False)          # must not raise


# ── rendering ───────────────────────────────────────────────────────────────────
def test_rendering_names_the_state_instead_of_printing_inf():
    assert format_profit_factor(1.25, ProfitFactorState.FINITE, ".3f") == "1.250"
    assert format_profit_factor(float("inf"), ProfitFactorState.UNBOUNDED) == "unbounded"
    assert format_profit_factor(0.0, ProfitFactorState.UNDEFINED) == "undefined"


# ══════════════════════════════════════════════════════════════════════════════
# consumers: every one of them handles all three states explicitly
# ══════════════════════════════════════════════════════════════════════════════
from supercortex.falsify import probes  # noqa: E402
from supercortex.report import experiment as report  # noqa: E402


def _doc(value, state="finite", **extra):
    trades = {"profit_factor_gross": value, "profit_factor_gross_state": state,
              "n_trades": 5, "win_rate": 0.5}
    trades.update(extra)
    return {"trades": trades}


# ── probe comparison ────────────────────────────────────────────────────────────
def test_probe_score_reads_a_finite_profit_factor():
    assert probes.score(_doc(1.5)) == 1.5


@pytest.mark.parametrize("state", ["unbounded", "undefined"])
def test_probe_score_of_a_non_finite_run_is_none(state):
    assert probes.score(_doc(None, state)) is None


def test_a_legacy_document_without_a_state_tag_is_read_as_finite():
    """Sealed before the tag existed. A bare number was always finite — nothing else
    could be serialised — so this reads history, it does not guess at it."""
    assert probes.score({"trades": {"profit_factor_gross": 1.25}}) == 1.25


def test_a_channel_whose_baseline_is_not_finite_fails_closed():
    """No comparison exists, so no verdict can be a pass."""
    runs = {f"bars-{k}": _doc(1.0) for k in ("scramble", "advance", "delay")}
    verdict = probes.assess(_doc(None, "unbounded"), runs, "bars")
    assert verdict.status == "UNDEFINED"
    assert verdict.passed is False
    assert "undefined" in verdict.reasoning.lower()


def test_a_channel_whose_probe_run_is_not_finite_fails_closed():
    runs = {f"bars-{k}": _doc(1.0) for k in ("scramble", "advance", "delay")}
    runs["bars-advance"] = _doc(None, "undefined")
    verdict = probes.assess(_doc(1.0), runs, "bars")
    assert verdict.status == "UNDEFINED"
    assert verdict.passed is False
    assert "bars-advance" in verdict.reasoning


def test_an_undefined_channel_makes_the_whole_test_fail():
    runs = {f"bars-{k}": _doc(1.0) for k in ("scramble", "advance", "delay")}
    runs["bars-delay"] = _doc(None, "unbounded")
    passed, verdicts = probes.verdict(_doc(1.0), runs, ("bars",))
    assert passed is False
    assert verdicts[0].status == "UNDEFINED"


def test_finite_probe_comparison_is_unchanged():
    runs = {"bars-scramble": _doc(0.5), "bars-advance": _doc(1.5),
            "bars-delay": _doc(0.5)}
    verdict = probes.assess(_doc(1.0), runs, "bars")
    assert verdict.status == "PASS"
    assert verdict.advance_delta == pytest.approx(0.5)


# ── report rendering ────────────────────────────────────────────────────────────
def test_report_renders_a_finite_profit_factor_as_a_number():
    assert report.render_profit_factor(_doc(1.234567)["trades"]) == "1.234567"


@pytest.mark.parametrize(("state", "shown"),
                         [("unbounded", "unbounded"), ("undefined", "undefined")])
def test_report_names_a_non_finite_profit_factor(state, shown):
    assert report.render_profit_factor(_doc(None, state)["trades"]) == shown


def test_report_reads_a_legacy_document_as_finite():
    assert report.render_profit_factor({"profit_factor_gross": 2.0}) == "2.000000"


# ── clears_break_even reads the STATE, not the stored float ─────────────────────
# The three-state tests above pass under the old bare comparison too, because `inf` and
# `0.0` happen to compare the right way. These two do not: they pin that the property
# classifies rather than relying on whatever float the ratio left behind.
import dataclasses  # noqa: E402


def test_undefined_does_not_clear_even_when_the_stored_ratio_looks_good():
    undefined = apply([], FEE_FREE)
    assert undefined.profit_factor_gross_state is ProfitFactorState.UNDEFINED
    misleading = dataclasses.replace(undefined, profit_factor_gross=99.0)
    assert misleading.profit_factor_gross >= misleading.break_even_profit_factor
    assert misleading.clears_break_even is False


def test_unbounded_clears_even_when_the_stored_ratio_looks_bad():
    unbounded = apply([WIN], FEE_FREE)
    assert unbounded.profit_factor_gross_state is ProfitFactorState.UNBOUNDED
    misleading = dataclasses.replace(unbounded, profit_factor_gross=0.0)
    assert not misleading.profit_factor_gross >= misleading.break_even_profit_factor
    assert misleading.clears_break_even is True
