"""The bar type must describe the interval it carries.

`bar_type_for()` hardcoded `1-HOUR` for every run. A 1-minute or 4-hour backtest therefore
told Nautilus its bars were hourly. Freezing that into the verifier as well would have made
the reconstruction agree with the producer *about the wrong thing* - the digest would match
and both would be wrong together, which is the failure mode a verifier exists to prevent.
"""
from __future__ import annotations

import pytest

from supercortex.backtest import feed, runner

EXPECTED = {
    "1m": "1-MINUTE",
    "1h": "1-HOUR",
    "4h": "4-HOUR",
    "1d": "1-DAY",
}


@pytest.fixture(scope="module")
def instrument():
    return runner.fee_free_instrument()


@pytest.mark.parametrize(("interval", "spec"), sorted(EXPECTED.items()))
def test_each_supported_interval_maps_to_its_own_bar_type(instrument, interval, spec):
    bar_type = runner.bar_type_for(instrument, interval)
    assert str(bar_type).endswith(f"-{spec}-LAST-EXTERNAL"), str(bar_type)


def test_every_interval_the_feed_supports_has_a_bar_type(instrument):
    """`interval_ms` is the feed's supported set. A gap here is a run that cannot be
    described, not a default to fall back on."""
    supported = {"1m", *feed.RESAMPLE}
    assert set(EXPECTED) == supported
    for interval in supported:
        runner.bar_type_for(instrument, interval)


def test_two_intervals_never_share_a_bar_type(instrument):
    produced = {interval: str(runner.bar_type_for(instrument, interval))
                for interval in EXPECTED}
    assert len(set(produced.values())) == len(produced), produced


def test_an_unsupported_interval_is_refused(instrument):
    with pytest.raises(KeyError):
        runner.bar_type_for(instrument, "3m")
