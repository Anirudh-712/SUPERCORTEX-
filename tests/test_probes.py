"""The look-ahead battery's verdict logic.

This encodes the defect from LRN-000002: a clean shift probe on a channel the strategy
does not read is a reassuring null that means nothing. The battery must say so out loud
rather than reporting a pass.
"""
from __future__ import annotations

import pytest

from supercortex.backtest.runner import ProbeSpec
from supercortex.falsify import probes


def m(pf: float) -> dict:
    return {"trades": {"profit_factor_gross": pf}}


def runs(base: float, scramble: float, advance: float, delay: float, channel="funding"):
    return m(base), {
        f"{channel}-scramble": m(scramble),
        f"{channel}-advance": m(advance),
        f"{channel}-delay": m(delay),
    }


# ── the three interpretable outcomes ────────────────────────────────────────────
def test_channel_the_strategy_ignores_is_flagged_inert():
    """EXP-000001's exact situation: probe came back clean and proved nothing."""
    base, p = runs(1.00, scramble=1.00, advance=1.00, delay=1.00)
    v = probes.assess(base, p, "funding")
    assert v.status == "INERT"
    assert not v.passed
    assert "uninformative" in v.reasoning


def test_used_channel_that_gains_nothing_from_lookahead_is_suspicious():
    """Consistent with the baseline already carrying the future information."""
    base, p = runs(1.00, scramble=0.60, advance=1.02, delay=0.70)
    v = probes.assess(base, p, "funding")
    assert v.status == "SUSPICIOUS"
    assert not v.passed
    assert "saturated" in v.reasoning


def test_live_channel_with_headroom_passes():
    base, p = runs(1.00, scramble=0.55, advance=1.40, delay=0.80)
    v = probes.assess(base, p, "funding")
    assert v.status == "PASS"
    assert v.passed


# ── either evidence of use is enough ────────────────────────────────────────────
def test_delay_alone_establishes_the_channel_matters():
    """Scrambling may not move a strategy that only reads the sign of a series."""
    base, p = runs(1.00, scramble=1.03, advance=1.35, delay=0.55)
    assert probes.assess(base, p, "funding").status == "PASS"


def test_scramble_alone_establishes_the_channel_matters():
    base, p = runs(1.00, scramble=0.40, advance=1.30, delay=1.01)
    assert probes.assess(base, p, "funding").status == "PASS"


# ── thresholds ──────────────────────────────────────────────────────────────────
def test_changes_below_the_material_threshold_are_noise():
    base, p = runs(1.00, scramble=1.05, advance=1.05, delay=0.96)
    assert probes.assess(base, p, "funding").status == "INERT"


def test_advance_must_help_not_merely_differ():
    """A large NEGATIVE response to added look-ahead is not evidence of cleanliness."""
    base, p = runs(1.00, scramble=0.50, advance=0.40, delay=0.60)
    assert probes.assess(base, p, "funding").status == "SUSPICIOUS"


def test_zero_baseline_does_not_divide_by_zero():
    base, p = runs(0.0, scramble=0.0, advance=0.0, delay=0.0)
    assert probes.assess(base, p, "funding").status == "INERT"


# ── overall verdict ─────────────────────────────────────────────────────────────
def test_every_channel_must_pass():
    base = m(1.00)
    p = {
        "bars-scramble": m(0.5), "bars-advance": m(1.4), "bars-delay": m(0.8),
        "funding-scramble": m(1.0), "funding-advance": m(1.0), "funding-delay": m(1.0),
    }
    passed, results = probes.verdict(base, p, ("bars", "funding"))
    assert not passed
    assert [r.status for r in results] == ["PASS", "INERT"]


def test_all_channels_live_passes_overall():
    base = m(1.00)
    p = {
        "bars-scramble": m(0.5), "bars-advance": m(1.4), "bars-delay": m(0.8),
        "funding-scramble": m(0.6), "funding-advance": m(1.3), "funding-delay": m(0.7),
    }
    passed, _ = probes.verdict(base, p, ("bars", "funding"))
    assert passed


# ── battery construction ────────────────────────────────────────────────────────
def test_battery_covers_every_channel_three_ways():
    specs = probes.battery(("bars", "funding"))
    assert len(specs) == 6
    assert {s.label for s in specs} == {
        "bars-scramble", "bars-advance", "bars-delay",
        "funding-scramble", "funding-advance", "funding-delay",
    }


def test_probe_specs_are_not_baseline():
    assert all(not s.is_baseline for s in probes.battery(("bars", "funding")))
    assert ProbeSpec().is_baseline


def test_advance_and_delay_have_opposite_signs():
    specs = {s.label: s for s in probes.battery(("funding",))}
    assert specs["funding-advance"].funding_shift == 1     # earlier = look-ahead granted
    assert specs["funding-delay"].funding_shift == -1


def test_unknown_channel_rejected():
    with pytest.raises(ValueError, match="unknown channel"):
        probes.battery(("order_book",))
