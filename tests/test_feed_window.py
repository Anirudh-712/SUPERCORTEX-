"""`common_window` at the data boundary it depends on.

The function reads the minimum and maximum `ts_ms` of two Parquet datasets and wraps each
in `int(...)`. Polars returns `None` from `Series.min()` for an empty or all-null column,
and `int(None)` is a `TypeError` naming nothing — not the dataset, not the field, not which
of the four reads failed. A backtest window is the span a strategy is evaluated over, so
"the window came out wrong" is not a failure anyone notices in time.

Every fixture is a temporary Parquet file under tmp_path with `feed.CATALOG` redirected at
it. No real dataset is opened, no engine runs.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from supercortex.backtest import feed

MINUTE = 60_000
HOUR = 3_600_000
BASE = 1_600_000_000_000


@pytest.fixture
def catalog(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(feed, "CATALOG", tmp_path)
    return tmp_path


def write(catalog: Path, kind: str, stamps, dtype=pl.Int64, *, column="ts_ms") -> Path:
    folder, name = {"klines": ("klines", "1m.parquet"),
                    "funding": ("funding_rate", "funding.parquet")}[kind]
    path = catalog / folder / "BTCUSDT.um" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame({column: pl.Series(column, stamps, dtype=dtype)})
    frame.write_parquet(path)
    return path


def both(catalog: Path, bars, funding, **kw) -> None:
    write(catalog, "klines", bars, **kw)
    write(catalog, "funding", funding, **kw)


# ── the valid window, unchanged ─────────────────────────────────────────────────
def test_the_window_is_the_overlap_of_the_two_datasets(catalog):
    both(catalog,
         [BASE, BASE + MINUTE, BASE + 2 * MINUTE],
         [BASE + MINUTE, BASE + 8 * HOUR])
    assert feed.common_window("BTCUSDT", "um") == (BASE + MINUTE, BASE + 2 * MINUTE)


def test_a_single_bar_and_a_single_funding_event_still_form_a_window(catalog):
    both(catalog, [BASE], [BASE])
    assert feed.common_window("BTCUSDT", "um") == (BASE, BASE)


def test_the_window_ignores_row_order(catalog):
    both(catalog, [BASE + 2 * MINUTE, BASE, BASE + MINUTE], [BASE, BASE + 8 * HOUR])
    assert feed.common_window("BTCUSDT", "um") == (BASE, BASE + 2 * MINUTE)


# ── empty, null and mistyped timestamps ─────────────────────────────────────────
def test_an_empty_klines_dataset_names_that_dataset(catalog):
    write(catalog, "klines", [])
    write(catalog, "funding", [BASE])
    with pytest.raises(feed.FeedDataError, match="klines"):
        feed.common_window("BTCUSDT", "um")


def test_an_empty_funding_dataset_names_that_dataset(catalog):
    write(catalog, "klines", [BASE])
    write(catalog, "funding", [])
    with pytest.raises(feed.FeedDataError, match="funding"):
        feed.common_window("BTCUSDT", "um")


def test_an_all_null_timestamp_column_names_the_field(catalog):
    write(catalog, "klines", [None, None])
    write(catalog, "funding", [BASE])
    with pytest.raises(feed.FeedDataError, match="ts_ms"):
        feed.common_window("BTCUSDT", "um")


def test_a_null_among_valid_timestamps_is_rejected(catalog):
    """A hole in the timestamps means the bounds are computed over a different set of rows
    than the caller thinks. Polars skips nulls in min/max; we do not."""
    write(catalog, "klines", [BASE, None, BASE + MINUTE])
    write(catalog, "funding", [BASE])
    with pytest.raises(feed.FeedDataError, match="null"):
        feed.common_window("BTCUSDT", "um")


@pytest.mark.parametrize("dtype", [pl.Float64, pl.Utf8])
def test_a_non_integer_timestamp_column_is_rejected(catalog, dtype):
    stamps = [float(BASE)] if dtype is pl.Float64 else [str(BASE)]
    write(catalog, "klines", stamps, dtype=dtype)
    write(catalog, "funding", [BASE])
    with pytest.raises(feed.FeedDataError, match="Int64"):
        feed.common_window("BTCUSDT", "um")


def test_a_missing_timestamp_column_names_the_field(catalog):
    write(catalog, "klines", [BASE], column="open_time")
    write(catalog, "funding", [BASE])
    with pytest.raises(feed.FeedDataError, match="ts_ms"):
        feed.common_window("BTCUSDT", "um")


@pytest.mark.parametrize("bad", [0, -1, -BASE])
def test_a_non_positive_epoch_is_rejected(catalog, bad):
    """Epoch-ms 0 is 1970. No crypto dataset legitimately starts there, and a zero that
    reaches the window silently widens it to include everything."""
    write(catalog, "klines", [bad, BASE])
    write(catalog, "funding", [BASE])
    with pytest.raises(feed.FeedDataError, match="ts_ms"):
        feed.common_window("BTCUSDT", "um")


def test_a_missing_parquet_file_names_the_dataset(catalog):
    write(catalog, "klines", [BASE])
    with pytest.raises(feed.FeedDataError, match="funding"):
        feed.common_window("BTCUSDT", "um")


# ── the window must be a window ─────────────────────────────────────────────────
def test_datasets_that_do_not_overlap_are_refused(catalog):
    """An inverted span is not an empty backtest; it is a backtest over nothing, reported
    as though it ran."""
    both(catalog, [BASE, BASE + MINUTE], [BASE + 400 * 24 * HOUR])
    with pytest.raises(feed.FeedDataError, match="do not overlap"):
        feed.common_window("BTCUSDT", "um")


# ── the guarantee those add up to ───────────────────────────────────────────────
def test_no_failure_escapes_as_a_bare_typeerror(catalog):
    cases = [
        ([], [BASE]),
        ([BASE], []),
        ([None], [BASE]),
        ([BASE], [None]),
    ]
    for bars, funding in cases:
        both(catalog, bars, funding)
        with pytest.raises(feed.FeedDataError) as caught:
            feed.common_window("BTCUSDT", "um")
        assert not isinstance(caught.value, TypeError)
        assert "BTCUSDT" in str(caught.value)


def test_feed_data_error_is_a_runtime_error():
    assert issubclass(feed.FeedDataError, RuntimeError)
