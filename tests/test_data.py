"""Data pipeline guarantees. These encode defects that would otherwise be silent."""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from supercortex.data import canonical, sources


# ── timestamp normalisation ─────────────────────────────────────────────────────
def _norm(values: list[int]) -> list[int]:
    frame = pl.DataFrame({"t": values})
    return frame.select(canonical._to_millis(pl.col("t")).alias("t"))["t"].to_list()


def test_milliseconds_pass_through_unchanged():
    assert _norm([1704067200000, 1780272000000]) == [1704067200000, 1780272000000]


def test_microseconds_are_downscaled():
    """spot klines 2026-06 ships 16-digit microsecond epochs."""
    assert _norm([1780272000000000]) == [1780272000000]


def test_mixed_eras_align_to_the_same_instant():
    """The actual defect: concatenating spot 2024 (ms) with spot 2026 (us) unnormalised
    puts half the history three orders of magnitude away, silently."""
    ms, us = 1780272000000, 1780272000000000
    out = _norm([ms, us])
    assert out[0] == out[1], "same instant must normalise to the same value"


def test_second_epochs_are_left_alone():
    """Guard must not mangle values that are already small."""
    assert _norm([1704067200]) == [1704067200]


# ── quality contract ────────────────────────────────────────────────────────────
def _frame(timestamps: list[int], **overrides) -> pl.DataFrame:
    n = len(timestamps)
    base = {
        "ts_ms": timestamps,
        "open": [100.0] * n, "high": [101.0] * n,
        "low": [99.0] * n, "close": [100.5] * n,
        "volume": [1.0] * n, "quote_volume": [100.0] * n,
        "trades": [1] * n, "taker_buy_base": [0.5] * n, "taker_buy_quote": [50.0] * n,
    }
    base.update(overrides)
    return pl.DataFrame(base)


def test_clean_series_is_complete_with_no_gaps():
    report = canonical.validate(_frame([0, 60_000, 120_000]), "1m")
    assert report.ok
    assert report.rows == 3
    assert report.completeness == 1.0
    assert report.gaps == []


def test_gap_is_inventoried_not_hidden():
    report = canonical.validate(_frame([0, 60_000, 300_000]), "1m")
    assert len(report.gaps) == 1
    assert report.gaps[0]["missing_bars"] == 3
    assert report.completeness < 1.0
    # a gap alone is not a violation — venues halt, symbols relist
    assert report.ok


def test_duplicate_timestamps_are_a_violation():
    report = canonical.validate(_frame([0, 60_000, 60_000]), "1m")
    assert not report.ok
    assert any("duplicate" in v for v in report.violations)


def test_impossible_ohlc_is_a_violation():
    report = canonical.validate(_frame([0, 60_000], high=[101.0, 98.0]), "1m")
    assert not report.ok
    assert any("high" in v for v in report.violations)


def test_non_positive_price_is_a_violation():
    report = canonical.validate(_frame([0, 60_000], close=[100.5, 0.0]), "1m")
    assert not report.ok
    assert any("non-positive" in v for v in report.violations)


def test_empty_dataset_is_a_violation():
    assert not canonical.validate(_frame([]), "1m").ok


# ── archive addressing ──────────────────────────────────────────────────────────
def test_futures_kline_url_shape():
    a = sources.Archive("um", "BTCUSDT", "klines", date(2024, 1, 1), "1m")
    assert a.url == (
        "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1m/"
        "BTCUSDT-1m-2024-01.zip"
    )
    assert a.local.as_posix().endswith(
        "data/raw/binance/um/BTCUSDT/klines/1m/BTCUSDT-1m-2024-01.zip"
    )


def test_months_is_inclusive_across_a_year_boundary():
    got = sources.months(date(2023, 11, 1), date(2024, 2, 1))
    assert [f"{d:%Y-%m}" for d in got] == ["2023-11", "2023-12", "2024-01", "2024-02"]


def test_unknown_interval_is_rejected():
    with pytest.raises(KeyError):
        canonical.validate(_frame([0, 60_000]), "7m")
