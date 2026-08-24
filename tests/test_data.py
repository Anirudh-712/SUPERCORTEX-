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


# ── gap detection must not confuse clock jitter with missing data ───────────────
def test_off_grid_jitter_is_not_a_gap():
    """Binance metrics timestamps drift 1-2s off the 5-minute grid 42 times in six
    years. Flooring the delta reported each as a 'gap of 0', burying the real gaps."""
    step = 300_000
    ts = [0, step, 2 * step + 1000, 3 * step]  # third observation is 1s late
    gaps, off_grid = canonical._gap_scan(pl.DataFrame({"ts_ms": ts}), step)
    assert gaps == []
    assert off_grid == 2  # the late arrival perturbs the delta before and after it


def test_genuine_missing_interval_is_a_gap():
    step = 300_000
    gaps, off_grid = canonical._gap_scan(pl.DataFrame({"ts_ms": [0, step, 3 * step]}), step)
    assert len(gaps) == 1
    assert gaps[0]["missing_bars"] == 1
    assert off_grid == 0


def test_jitter_around_a_real_gap_still_counts_the_gap():
    step = 300_000
    ts = [0, step, 4 * step + 2000]  # two missing, plus 2s of jitter
    gaps, _ = canonical._gap_scan(pl.DataFrame({"ts_ms": ts}), step)
    assert len(gaps) == 1
    assert gaps[0]["missing_bars"] == 2


# ── funding ─────────────────────────────────────────────────────────────────────
def _funding(ts_ms: list[int], rate: float = 0.0001, hours: int = 8) -> pl.DataFrame:
    n = len(ts_ms)
    return pl.DataFrame({
        "ts_ms": ts_ms,
        "funding_interval_hours": [hours] * n,
        "funding_rate": [rate] * n,
    })


def test_funding_eight_hourly_series_is_clean():
    step = 8 * 3_600_000
    report = canonical.validate_funding(_funding([0, step, 2 * step]))
    assert report.ok
    assert report.notes["funding_intervals_seen"] == [8]


def test_implausible_funding_rate_is_a_violation():
    """A rate beyond +/-5% per interval means the units are wrong, not the market."""
    report = canonical.validate_funding(_funding([0, 8 * 3_600_000], rate=0.5))
    assert not report.ok
    assert any("beyond" in v for v in report.violations)


def test_missing_funding_event_is_a_gap():
    step = 8 * 3_600_000
    report = canonical.validate_funding(_funding([0, step, 3 * step]))
    assert len(report.gaps) == 1
    assert report.gaps[0]["missing_bars"] == 1


# ── metrics ─────────────────────────────────────────────────────────────────────
def _metrics(ts_ms: list[int], oi: float = 100.0) -> pl.DataFrame:
    n = len(ts_ms)
    return pl.DataFrame({
        "ts_ms": ts_ms, "open_interest": [oi] * n, "open_interest_value": [oi * 1e4] * n,
        "toptrader_accounts_ls": [1.0] * n, "toptrader_positions_ls": [1.0] * n,
        "all_accounts_ls": [1.0] * n, "taker_volume_ls": [1.0] * n,
    })


def test_metrics_five_minute_grid_is_clean():
    report = canonical.validate_metrics(_metrics([0, 300_000, 600_000]))
    assert report.ok
    assert report.gaps == []


def test_negative_open_interest_is_a_violation():
    report = canonical.validate_metrics(_metrics([0, 300_000], oi=-1.0))
    assert not report.ok
    assert any("negative open interest" in v for v in report.violations)


# ── archive addressing for the two cadences ─────────────────────────────────────
def test_metrics_is_daily_not_monthly():
    """Binance publishes no monthly metrics archive — requesting one 404s."""
    a = sources.Archive("um", "BTCUSDT", "metrics", date(2026, 6, 15))
    assert a.cadence == "daily"
    assert a.url.endswith("daily/metrics/BTCUSDT/BTCUSDT-metrics-2026-06-15.zip")


def test_funding_is_monthly():
    a = sources.Archive("um", "BTCUSDT", "fundingRate", date(2026, 6, 1))
    assert a.cadence == "monthly"
    assert a.url.endswith("monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2026-06.zip")


def test_daily_archives_are_foldered_by_year():
    a = sources.Archive("um", "BTCUSDT", "metrics", date(2026, 6, 15))
    assert "/metrics/2026/" in a.local.as_posix()


def test_days_is_inclusive():
    got = sources.days(date(2024, 2, 27), date(2024, 3, 2))
    assert [d.isoformat() for d in got] == [
        "2024-02-27", "2024-02-28", "2024-02-29", "2024-03-01", "2024-03-02"
    ]
