"""Data pipeline guarantees. These encode defects that would otherwise be silent."""
from __future__ import annotations

import json
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from supercortex.data import canonical, sources
from supercortex.data import cli as data_cli


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


# ── the ts_ms contract ──────────────────────────────────────────────────────────
# Every reader ends its timestamp expression with `.cast(pl.Int64)`, and all three live L1
# datasets carry ts_ms=Int64. The validators depend on that: they subtract the bounds,
# floor-divide by the step and divide by 1000 to build the ISO range. Nothing checked it,
# so a frame that violated the contract produced a plausible-looking QualityReport instead
# of a failure — the worst outcome available to a system whose job is not fooling itself.
def test_epoch_bounds_returns_the_first_and_last_millisecond():
    ts = pl.Series("ts_ms", [3_000, 1_000, 2_000], dtype=pl.Int64)
    assert canonical._epoch_bounds(ts) == (1_000, 3_000)


@pytest.mark.parametrize("dtype", [pl.Float64, pl.Int32, pl.UInt64])
def test_non_int64_timestamps_are_rejected_not_silently_reported(dtype):
    """A float or narrow ts_ms means the reader was bypassed. Fail loudly."""
    ts = pl.Series("ts_ms", [0, 60_000, 120_000], dtype=dtype)
    with pytest.raises(TypeError, match="Int64"):
        canonical._epoch_bounds(ts)


def test_all_null_timestamps_are_rejected_with_a_diagnostic():
    ts = pl.Series("ts_ms", [None, None], dtype=pl.Int64)
    with pytest.raises(TypeError, match="no non-null"):
        canonical._epoch_bounds(ts)


def test_validate_rejects_a_float_timestamp_column():
    """End to end: the whole validator refuses, not just the helper."""
    frame = _frame([0, 60_000, 120_000]).with_columns(pl.col("ts_ms").cast(pl.Float64))
    with pytest.raises(TypeError, match="Int64"):
        canonical.validate(frame, "1m")


# ── the gap record shape ────────────────────────────────────────────────────────
# Gap records are serialised into registry.sqlite as gaps_json and sliced into dataset
# cards (`g['from'][:16]`). The keys and their types are a published contract, not an
# implementation detail.
def test_gap_records_carry_the_documented_shape():
    report = canonical.validate(_frame([0, 60_000, 300_000]), "1m")
    gap = report.gaps[0]
    assert set(gap) == {"from", "to", "missing_bars"}
    assert isinstance(gap["missing_bars"], int)
    assert isinstance(gap["from"], str) and gap["from"].endswith("+00:00")
    assert isinstance(gap["to"], str) and gap["to"].endswith("+00:00")


def test_as_dict_sums_missing_bars_across_every_gap():
    report = canonical.validate(_frame([0, 60_000, 300_000, 900_000]), "1m")
    assert len(report.gaps) == 2
    assert report.as_dict()["missing_bars"] == sum(g["missing_bars"] for g in report.gaps)


# ── local archive addressing ────────────────────────────────────────────────────
def test_monthly_archive_local_path_is_assembled_from_mixed_parts():
    """RAW_ROOT is a Path and every other component is a str; the join must still work."""
    a = sources.Archive("um", "BTCUSDT", "klines", date(2026, 6, 1), "1m")
    assert a.local.is_absolute()
    assert a.local.as_posix().endswith(
        "data/raw/binance/um/BTCUSDT/klines/1m/BTCUSDT-1m-2026-06.zip"
    )
    assert "/2026/" not in a.local.as_posix()      # monthly kinds are not year-foldered


# ══════════════════════════════════════════════════════════════════════════════
# the traded-kline build path
# ══════════════════════════════════════════════════════════════════════════════
# `build()` predates `build_generic()` and carried its own copy of the same steps, with
# three behaviours the reviewed boundary has since removed: it deduplicated on `ts_ms`
# before validating, it built from whatever archives happened to be on disk, and it wrote
# the canonical file before checking violations. These tests exercise the real `build()`
# entry point, so they fail while that divergent implementation is what runs.
KLINE_MINUTE = 60_000
KLINE_BASE = 1_577_836_800_000          # 2020-01-01 00:00:00 UTC


def kline_row(ts: int, close: float = 100.5) -> str:
    """One archive line in the venue's twelve-field kline layout."""
    return (f"{ts},100.0,101.0,99.0,{close},1.0,{ts + KLINE_MINUTE - 1},"
            f"100.0,7,0.5,50.0,0")


def kline_rows(n: int = 5, start: int = KLINE_BASE) -> list[str]:
    return [kline_row(start + i * KLINE_MINUTE) for i in range(n)]


@pytest.fixture
def kline_archive(tmp_path, monkeypatch):
    """An `Archive` per month, written inside the test's own directory."""
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")

    def make(rows: list[str], *, month: int = 1) -> sources.Archive:
        a = sources.Archive("um", "BTCUSDT", "klines", date(2020, month, 1), "1m")
        a.local.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(a.local, "w") as zf:
            zf.writestr(f"{a.stem}.csv", "\n".join(rows) + "\n")
        return a

    return make


def kline_build(out: Path, archives: list[sources.Archive]):
    return canonical.build(archives, "1m", out)


# ── duplicates must survive to the report ──────────────────────────────────────
def test_kline_duplicates_inside_one_archive_reach_the_report(kline_archive, tmp_path):
    rows = [*kline_rows(3), kline_row(KLINE_BASE)]
    with pytest.raises(canonical.BuildRefused) as caught:
        kline_build(tmp_path / "1m.parquet", [kline_archive(rows)])
    assert caught.value.report is not None
    assert caught.value.report.duplicates == 1
    assert "duplicate" in " ".join(caught.value.report.violations)


def test_kline_duplicates_across_archives_reach_the_report(kline_archive, tmp_path):
    """The overlap a monthly boundary can produce."""
    first = kline_archive(kline_rows(3), month=1)
    second = kline_archive([kline_row(KLINE_BASE + 2 * KLINE_MINUTE),
                            kline_row(KLINE_BASE + 3 * KLINE_MINUTE)], month=2)
    assert first.local != second.local
    with pytest.raises(canonical.BuildRefused) as caught:
        kline_build(tmp_path / "1m.parquet", [first, second])
    assert caught.value.report.duplicates == 1


def test_the_kline_build_discards_no_row(kline_archive, tmp_path):
    """Choosing which of two rows claiming one instant to keep is a decision nobody made."""
    rows = [*kline_rows(3), kline_row(KLINE_BASE, close=999.0)]
    with pytest.raises(canonical.BuildRefused) as caught:
        kline_build(tmp_path / "1m.parquet", [kline_archive(rows)])
    assert caught.value.report.rows == 4


# ── the requested span, not what happened to be on disk ────────────────────────
@pytest.fixture
def three_kline_months(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")

    def make(present: set[int]) -> list[sources.Archive]:
        out = []
        for offset, month in enumerate((1, 2, 3)):
            a = sources.Archive("um", "BTCUSDT", "klines", date(2020, month, 1), "1m")
            if month in present:
                a.local.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(a.local, "w") as zf:
                    zf.writestr(f"{a.stem}.csv", "\n".join(
                        kline_rows(3, KLINE_BASE + offset * 10 * KLINE_MINUTE)) + "\n")
            out.append(a)
        return out

    return make


@pytest.mark.parametrize("present,absent", [
    ({2, 3}, "2020-01"), ({1, 3}, "2020-02"), ({1, 2}, "2020-03"),
], ids=["first missing", "middle missing", "final missing"])
def test_a_missing_kline_archive_refuses_the_build(three_kline_months, tmp_path,
                                                   present, absent):
    """Completeness is measured between the observed first and last row, so a truncated
    window reports as complete."""
    with pytest.raises(canonical.BuildRefused) as caught:
        kline_build(tmp_path / "1m.parquet", three_kline_months(present))
    assert absent in str(caught.value)


def test_every_missing_kline_archive_is_named(three_kline_months, tmp_path):
    with pytest.raises(canonical.BuildRefused) as caught:
        kline_build(tmp_path / "1m.parquet", three_kline_months({2}))
    assert "2020-01" in str(caught.value) and "2020-03" in str(caught.value)


def test_an_empty_kline_request_refuses(tmp_path):
    with pytest.raises(canonical.BuildRefused, match="no archives"):
        kline_build(tmp_path / "1m.parquet", [])


def test_an_unreadable_kline_archive_refuses_and_names_it(kline_archive, tmp_path):
    a = kline_archive(kline_rows())
    a.local.write_bytes(b"not a zip at all")
    with pytest.raises(canonical.BuildRefused) as caught:
        kline_build(tmp_path / "1m.parquet", [a])
    assert "2020-01" in str(caught.value)


# ── an invalid build must never replace or create a canonical L1 ───────────────
def test_an_invalid_kline_build_leaves_the_existing_l1_byte_identical(kline_archive,
                                                                      tmp_path):
    from supercortex.registry import db
    out = tmp_path / "1m.parquet"
    kline_build(out, [kline_archive(kline_rows(4))])
    before, digest = out.read_bytes(), db.sha256_file(out)

    bad = kline_archive([*kline_rows(3), kline_row(KLINE_BASE)], month=2)
    with pytest.raises(canonical.BuildRefused):
        kline_build(out, [bad])
    assert out.read_bytes() == before
    assert db.sha256_file(out) == digest


def test_an_invalid_kline_build_creates_no_canonical_l1(kline_archive, tmp_path):
    out = tmp_path / "1m.parquet"
    with pytest.raises(canonical.BuildRefused):
        kline_build(out, [kline_archive([*kline_rows(3), kline_row(KLINE_BASE)])])
    assert not out.exists()


def test_a_refused_kline_build_leaves_no_temporary_file(kline_archive, tmp_path):
    out = tmp_path / "sub" / "1m.parquet"
    with pytest.raises(canonical.BuildRefused):
        kline_build(out, [kline_archive([*kline_rows(3), kline_row(KLINE_BASE)])])
    assert list(out.parent.glob("*")) == []


# ── the CLI diagnoses rather than crashes ─────────────────────────────────────
def kline_invoke(*args):
    from typer.testing import CliRunner
    return CliRunner().invoke(data_cli.app, list(args))


def test_the_kline_build_command_reports_a_refusal_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")
    monkeypatch.setattr(canonical, "CATALOG_ROOT", tmp_path / "catalog")
    result = kline_invoke("build", "BTCUSDT", "--start", "2020-01", "--end", "2020-01")
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in result.output
    assert "BTCUSDT-1m-2020-01" in result.output


def test_a_violating_kline_build_reports_the_violation_cleanly(kline_archive, tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(canonical, "CATALOG_ROOT", tmp_path / "catalog")
    kline_archive([*kline_rows(3), kline_row(KLINE_BASE)])
    result = kline_invoke("build", "BTCUSDT", "--start", "2020-01", "--end", "2020-01")
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    assert "duplicate" in result.output
    assert not (tmp_path / "catalog" / "klines" / "BTCUSDT.um" / "1m.parquet").exists()


# ── the interval-specific validator is preserved ──────────────────────────────
def test_the_kline_build_uses_the_validator_bound_to_the_requested_interval(
        kline_archive, tmp_path):
    """`validate(frame, interval)` is what makes a one-minute gap a gap. Routing through
    the generic boundary must not lose the interval."""
    hourly = kline_archive([kline_row(KLINE_BASE + i * 3_600_000) for i in range(4)])
    _path, report = canonical.build([hourly], "1h", tmp_path / "1h.parquet")
    assert report.ok
    assert report.expected_rows == 4

    _path2, minute_report = canonical.build([hourly], "1m", tmp_path / "1m.parquet")
    assert minute_report.rows == 4
    assert minute_report.expected_rows == 181       # a 1m grid across three hours
    assert len(minute_report.gaps) == 3


# ── Parquet identity is a property of the data, not of the request's shape ─────
#: Enough rows that polars writes more than one row group, which is the only regime where
#: chunk boundaries are observable in the bytes. Measured: 120,000 rows in three parts is
#: byte-identical either way; 240,000 is not. Below the threshold this test cannot fail,
#: whatever the implementation does.
PARTITION_ROWS = 240_000


def test_parquet_identity_is_invariant_to_how_archives_partition_the_rows(
        kline_archive, tmp_path):
    """The same rows, divided differently among source archives, must produce the same
    file.

    `pl.concat` yields one chunk per archive and polars writes row groups on chunk
    boundaries, so without an explicit rechunk the bytes encode how the request happened
    to be decomposed. `data verify` re-hashes L1 against the registered digest and each
    card claims the dataset is rebuildable by the command written on it — both are claims
    about bytes.
    """
    from supercortex.registry import db
    rows = kline_rows(PARTITION_ROWS)
    third = PARTITION_ROWS // 3
    whole = kline_archive(rows, month=1)
    parts = [kline_archive(rows[:third], month=2),
             kline_archive(rows[third:2 * third], month=3),
             kline_archive(rows[2 * third:], month=4)]

    one, report_one = canonical.build([whole], "1m", tmp_path / "one.parquet")
    many, report_many = canonical.build(parts, "1m", tmp_path / "many.parquet")

    assert pl.read_parquet(one).equals(pl.read_parquet(many))
    assert report_one.as_dict() == report_many.as_dict()
    assert db.sha256_file(one) == db.sha256_file(many)


def test_the_rechunk_lives_on_the_shared_boundary_so_every_kind_inherits_it():
    """Funding, metrics, mark price and klines all reach Parquet through
    `_validate_and_write`. A rechunk in any one binding would give the guarantee to that
    kind and quietly withhold it from the others."""
    import inspect
    assert ".rechunk()" in inspect.getsource(canonical._validate_and_write)
    for other in (canonical.build, canonical.build_generic, canonical.build_metrics,
                  canonical.read_klines, canonical.read_mark_price):
        assert ".rechunk()" not in inspect.getsource(other), other.__name__


def test_the_rechunk_happens_after_validation_and_before_the_write():
    """Order is contract. Rechunking before validation pays a full materialising copy for
    a frame that may be about to be refused, and the validator's answer must be about the
    rows as read."""
    import inspect
    source = inspect.getsource(canonical._validate_and_write)
    validate_at = source.index("report = validator(")
    rechunk_at = source.index(".rechunk()")
    write_at = source.index("write_parquet(")
    assert validate_at < rechunk_at < write_at


# ══════════════════════════════════════════════════════════════════════════════
# metrics: slot ownership with delivery fallback
# ══════════════════════════════════════════════════════════════════════════════
# Binance publishes these observations one file per day. The files repeat timestamps
# (75,256 times across 2020-2026), overlap at the boundary, and jitter 1-2 seconds off the
# five-minute grid. A `unique(subset=["ts_ms"])` one layer up hid all three, collapsing
# byte-identical repeats harmlessly and once silently choosing between two rows that
# disagreed about open interest.
#
# Ownership cannot be decided inside a single-archive read: whether a boundary row should
# be kept depends on whether the OWNING archive supplies one, which is only knowable across
# archives. So the policy lives in a metrics-specific canonicalisation over all of them,
# and the validate-and-write step stays shared.
SLOT = 300_000
JITTER_LIMIT = 5_000


def metrics_row(stamp: str, oi: float = 100.0, ratio: float = 1.0,
                taker: float | str = 1.0) -> str:
    """One raw metrics line. `create_time` is a naive UTC datetime string, not an epoch."""
    return f"{stamp},BTCUSDT,{oi},{oi * 1000},{ratio},{ratio},{ratio},{taker}"


@pytest.fixture
def metrics_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")

    def make(rows: list[str], day: date) -> sources.Archive:
        a = sources.Archive("um", "BTCUSDT", "metrics", day)
        a.local.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(a.local, "w") as zf:
            zf.writestr(f"{a.stem}.csv", "\n".join(rows) + "\n")
        return a

    return make


D1, D2 = date(2024, 5, 1), date(2024, 5, 2)
MIDNIGHT = 1_714_608_000_000              # 2024-05-02 00:00:00 UTC


def canonicalise(archives):
    return canonical.canonical_metrics(archives)


# ── ownership ─────────────────────────────────────────────────────────────────
def test_the_owning_archive_beats_a_conflicting_non_owner(metrics_archive):
    """The 2024-05-01 case. Both files carry the midnight slot with different values; the
    slot's own date decides, not which file was read first."""
    prior = metrics_archive([metrics_row("2024-05-01 23:55:00"),
                             metrics_row("2024-05-02 00:00:00", oi=111.0)], D1)
    owner = metrics_archive([metrics_row("2024-05-02 00:00:00", oi=222.0),
                             metrics_row("2024-05-02 00:05:00")], D2)
    frame, facts = canonicalise([prior, owner])
    kept = frame.filter(pl.col("ts_ms") == MIDNIGHT)
    assert kept.height == 1
    assert kept["open_interest"][0] == 222.0
    assert facts["non_owner_candidates_ignored"] == 1


def test_a_delivery_fallback_is_retained_when_the_owner_supplies_none(metrics_archive):
    """The 2024-04-27 case. A slightly late observation for D+1 arrives only in D's file,
    and D+1's archive has no candidate for that slot. Dropping it would lose a real
    observation and open a gap."""
    prior = metrics_archive([metrics_row("2024-05-01 23:55:00"),
                             metrics_row("2024-05-02 00:00:02", oi=333.0)], D1)
    owner = metrics_archive([metrics_row("2024-05-02 00:05:00")], D2)
    frame, facts = canonicalise([prior, owner])
    late = frame.filter(pl.col("ts_ms") == MIDNIGHT + 2_000)
    assert late.height == 1
    assert late["open_interest"][0] == 333.0
    assert facts["delivery_fallback_rows"] == 1


def test_an_owner_candidate_removes_the_fallback_deterministically(metrics_archive):
    """Same input plus one owner row: the fallback stops being used, with no other change."""
    prior_rows = [metrics_row("2024-05-01 23:55:00"),
                  metrics_row("2024-05-02 00:00:02", oi=333.0)]
    without = canonicalise([metrics_archive(prior_rows, D1),
                            metrics_archive([metrics_row("2024-05-02 00:05:00")], D2)])
    with_owner = canonicalise([
        metrics_archive(prior_rows, D1),
        metrics_archive([metrics_row("2024-05-02 00:00:00", oi=444.0),
                         metrics_row("2024-05-02 00:05:00")], D2)])
    assert without[0].filter(pl.col("ts_ms") == MIDNIGHT + 2_000).height == 1
    assert without[1]["delivery_fallback_rows"] == 1

    kept = with_owner[0].filter(pl.col("ts_ms").is_between(MIDNIGHT, MIDNIGHT + 5_000))
    assert kept.height == 1
    assert kept["open_interest"][0] == 444.0
    assert kept["ts_ms"][0] == MIDNIGHT
    assert with_owner[1]["delivery_fallback_rows"] == 0
    assert with_owner[1]["non_owner_candidates_ignored"] == 1


def test_reversing_the_archive_order_changes_nothing(metrics_archive, tmp_path):
    from supercortex.registry import db
    prior = metrics_archive([metrics_row("2024-05-01 23:55:00"),
                             metrics_row("2024-05-02 00:00:00", oi=111.0)], D1)
    owner = metrics_archive([metrics_row("2024-05-02 00:00:00", oi=222.0),
                             metrics_row("2024-05-02 00:05:00")], D2)
    forward, ff = canonicalise([prior, owner])
    reverse, rf = canonicalise([owner, prior])
    assert forward.equals(reverse)
    assert ff == rf
    a, _ = canonical.build_metrics([prior, owner], tmp_path / "a.parquet")
    b, _ = canonical.build_metrics([owner, prior], tmp_path / "b.parquet")
    assert db.sha256_file(a) == db.sha256_file(b)


# ── identity within the preferred candidates ──────────────────────────────────
def test_exact_full_row_repeats_collapse(metrics_archive):
    rows = [metrics_row("2024-05-01 00:00:00"), metrics_row("2024-05-01 00:00:00"),
            metrics_row("2024-05-01 00:05:00")]
    frame, facts = canonicalise([metrics_archive(rows, D1)])
    assert frame.height == 2
    assert facts["exact_repeat_rows_removed"] == 1


def test_null_equals_null_for_repeat_purposes(metrics_archive):
    rows = [metrics_row("2024-05-01 00:00:00", taker=""),
            metrics_row("2024-05-01 00:00:00", taker="")]
    frame, facts = canonicalise([metrics_archive(rows, D1)])
    assert frame.height == 1
    assert frame["taker_volume_ls"][0] is None
    assert facts["exact_repeat_rows_removed"] == 1


def test_a_null_against_a_value_is_a_conflict(metrics_archive):
    """~15% of `toptrader_*` is null, so this is the common case, not a corner."""
    rows = [metrics_row("2024-05-01 00:00:00", taker=""),
            metrics_row("2024-05-01 00:00:00", taker=1.0)]
    with pytest.raises(canonical.MetricsOwnershipError, match="taker_volume_ls"):
        canonicalise([metrics_archive(rows, D1)])


def test_conflicting_candidates_within_the_preferred_source_refuse(metrics_archive):
    """Two owner rows disagreeing. There is no honest way to choose, and the order they
    appear in the file says nothing about which is right."""
    rows = [metrics_row("2024-05-01 00:05:00", oi=100.0),
            metrics_row("2024-05-01 00:05:00", oi=200.0)]
    with pytest.raises(canonical.MetricsOwnershipError) as caught:
        canonicalise([metrics_archive(rows, D1)])
    said = str(caught.value)
    assert "2024-05-01 00:05:00" in said
    assert "BTCUSDT-metrics-2024-05-01" in said
    assert "open_interest" in said


def test_two_raw_stamps_in_one_slot_are_candidates_for_the_same_observation(
        metrics_archive):
    """00:05:00 and 00:05:02 are one observation reported twice, not two observations."""
    rows = [metrics_row("2024-05-01 00:05:00", oi=100.0),
            metrics_row("2024-05-01 00:05:02", oi=200.0)]
    with pytest.raises(canonical.MetricsOwnershipError, match="open_interest"):
        canonicalise([metrics_archive(rows, D1)])


# ── jitter ────────────────────────────────────────────────────────────────────
def test_jitter_at_the_allowed_boundary_is_accepted(metrics_archive):
    rows = [metrics_row("2024-05-01 00:00:05"), metrics_row("2024-05-01 00:09:55")]
    frame, facts = canonicalise([metrics_archive(rows, D1)])
    assert frame.height == 2
    assert facts["max_grid_jitter_ms"] == JITTER_LIMIT


def test_jitter_beyond_the_boundary_refuses(metrics_archive):
    rows = [metrics_row("2024-05-01 00:00:00"), metrics_row("2024-05-01 00:00:06")]
    with pytest.raises(canonical.MetricsOwnershipError) as caught:
        canonicalise([metrics_archive(rows, D1)])
    assert "00:00:06" in str(caught.value)
    assert "BTCUSDT-metrics-2024-05-01" in str(caught.value)


def test_the_retained_timestamp_is_the_raw_one_not_the_slot(metrics_archive):
    """The slot is identity and ownership metadata. Replacing the observation's own
    timestamp with it would invent a measurement time the venue never reported."""
    frame, _ = canonicalise([metrics_archive(
        [metrics_row("2024-05-01 00:00:02", oi=7.0)], D1)])
    assert frame["ts_ms"].to_list() == [1_714_521_600_000 + 2_000]
    assert frame.columns == list(canonical.METRICS_CANONICAL)


# ── audit facts reach the report and the card ─────────────────────────────────
def test_the_normalisation_facts_are_computed_and_carried_into_the_report(
        metrics_archive, tmp_path):
    prior = metrics_archive([metrics_row("2024-05-01 00:00:00"),
                             metrics_row("2024-05-01 00:00:00"),
                             metrics_row("2024-05-02 00:00:00", oi=111.0),
                             metrics_row("2024-05-02 00:05:02", oi=333.0)], D1)
    owner = metrics_archive([metrics_row("2024-05-02 00:00:00", oi=222.0)], D2)
    _path, report = canonical.build_metrics([prior, owner], tmp_path / "m.parquet")
    assert report.notes["exact_repeat_rows_removed"] == 1
    assert report.notes["non_owner_candidates_ignored"] == 1
    assert report.notes["delivery_fallback_rows"] == 1
    assert report.notes["max_grid_jitter_ms"] == 2_000


# ── the shared boundary stays shared ──────────────────────────────────────────
def test_the_generic_boundary_still_performs_no_deduplication():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(canonical.build_generic))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "unique" not in called


def test_the_metrics_build_does_not_copy_the_staging_and_write_logic():
    """One atomic-write implementation. A second copy is how `build` and `build_generic`
    came to disagree in the first place."""
    import inspect
    metrics_src = inspect.getsource(canonical.build_metrics)
    generic_src = inspect.getsource(canonical.build_generic)
    for copied in ("write_parquet", ".building", "staged.replace"):
        assert copied not in metrics_src, copied
        assert copied not in generic_src, copied
    shared = inspect.getsource(canonical._validate_and_write)
    for owned in ("write_parquet", ".building", "staged.replace"):
        assert owned in shared, owned


def test_the_metrics_card_states_the_ownership_and_fallback_rules(tmp_path, monkeypatch,
                                                                  metrics_archive):
    from supercortex.registry import db
    registry = tmp_path / "registry.sqlite"
    db.init(registry)
    monkeypatch.setattr(db, "DB_PATH", registry)
    monkeypatch.setattr(db, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(canonical, "CATALOG_ROOT", tmp_path / "data" / "catalog")
    monkeypatch.setattr(data_cli, "CARDS_DIR", tmp_path / "05-data")
    (tmp_path / "05-data").mkdir()

    rows = [metrics_row(f"2024-05-01 {h:02d}:00:00") for h in range(24)]
    out = tmp_path / "data" / "catalog" / "metrics" / "BTCUSDT.um" / "metrics.parquet"
    canonical.build_metrics([metrics_archive(rows, D1)], out)

    from typer.testing import CliRunner
    result = CliRunner().invoke(data_cli.app, ["card", "BTCUSDT", "--kind", "metrics"])
    assert result.exit_code == 0, result.output
    card = " ".join(next((tmp_path / "05-data").glob("*.md"))
                    .read_text(encoding="utf-8").split())
    assert "five-minute slot" in card
    assert "owning archive" in card
    assert "delivery fallback" in card
    assert "identical in every field" in card
    assert "refuses" in card
    for false_promise in ("resolved automatically", "keeps the first", "picks the first"):
        assert false_promise not in card
    # Deliberately NOT asserting the fact names appear somewhere in the card: the static
    # SEMANTICS prose mentions all four, so a whole-card search finds them whether or not a
    # single computed value was persisted. `test_the_card_notes_carry_the_computed_values`
    # isolates the Notes block and compares against the build's own numbers - that is the
    # contract; this test covers the prose.


# ══════════════════════════════════════════════════════════════════════════════
# metrics: fallback eligibility, honest facts, slot-based validation
# ══════════════════════════════════════════════════════════════════════════════
D3, D10 = date(2024, 5, 3), date(2024, 5, 10)


def test_a_previous_day_delivery_is_an_eligible_fallback(metrics_archive):
    prior = metrics_archive([metrics_row("2024-05-02 00:00:02", oi=333.0)], D1)
    owner = metrics_archive([metrics_row("2024-05-02 00:05:00")], D2)
    frame, facts = canonicalise([prior, owner])
    assert frame.filter(pl.col("ts_ms") == MIDNIGHT + 2_000).height == 1
    assert facts["delivery_fallback_rows"] == 1


def test_a_next_day_delivery_is_an_eligible_fallback(metrics_archive):
    owner = metrics_archive([metrics_row("2024-05-02 00:05:00")], D2)
    later = metrics_archive([metrics_row("2024-05-02 23:55:00", oi=555.0)], D3)
    frame, facts = canonicalise([owner, later])
    assert frame.filter(pl.col("open_interest") == 555.0).height == 1
    assert facts["delivery_fallback_rows"] == 1


def test_a_non_adjacent_delivery_is_refused(metrics_archive):
    """A 2024-05-10 observation shipped in the 2024-05-01 file is not a boundary overlap.
    Accepting it would let any archive vouch for any day."""
    stray = metrics_archive([metrics_row("2024-05-01 00:00:00"),
                             metrics_row("2024-05-10 12:00:00", oi=999.0)], D1)
    owner = metrics_archive([metrics_row("2024-05-10 12:05:00")], D10)
    with pytest.raises(canonical.MetricsOwnershipError) as caught:
        canonicalise([stray, owner])
    said = str(caught.value)
    assert "2024-05-10 12:00:00" in said
    assert "BTCUSDT-metrics-2024-05-01" in said
    assert "BTCUSDT-metrics-2024-05-10" in said


def test_a_fallback_is_refused_when_the_owning_archive_was_not_supplied(metrics_archive):
    """Without the owner in the request we cannot claim it supplied nothing — it was never
    asked."""
    prior = metrics_archive([metrics_row("2024-05-01 23:55:00"),
                             metrics_row("2024-05-02 00:00:02", oi=333.0)], D1)
    with pytest.raises(canonical.MetricsOwnershipError) as caught:
        canonicalise([prior])
    said = str(caught.value)
    assert "2024-05-02 00:00:00" in said            # the slot
    assert "BTCUSDT-metrics-2024-05-02" in said     # the expected owner
    assert "BTCUSDT-metrics-2024-05-01" in said     # the supplied source


# ── the reported jitter must be the observed maximum ──────────────────────────
def test_the_reported_jitter_is_the_maximum_observed_not_the_maximum_retained(
        metrics_archive):
    """Measured over every candidate that passed the tolerance check, before preference or
    deduplication removed any. A figure computed after the ignored rows are gone describes
    what survived, not what was seen."""
    prior = metrics_archive([metrics_row("2024-05-01 23:55:00"),
                             metrics_row("2024-05-02 00:00:04", oi=111.0)], D1)
    owner = metrics_archive([metrics_row("2024-05-02 00:00:00", oi=222.0)], D2)
    frame, facts = canonicalise([prior, owner])
    assert frame.filter(pl.col("ts_ms") == MIDNIGHT)["open_interest"][0] == 222.0
    assert facts["non_owner_candidates_ignored"] == 1
    assert facts["max_grid_jitter_ms"] == 4_000     # from the row that was ignored


# ── validation is on the normalised slot ──────────────────────────────────────
def slot_frame(stamps: list[str]) -> pl.DataFrame:
    return pl.DataFrame({"ts_ms": [
        int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=UTC).timestamp() * 1000) for s in stamps]},
        schema={"ts_ms": pl.Int64}).with_columns(
        pl.from_epoch("ts_ms", time_unit="ms").dt.replace_time_zone("UTC").alias("ts"),
        *[pl.lit(1.0).alias(c) for c in canonical.METRICS_CANONICAL
          if c not in ("ts_ms", "ts")])


def test_opposing_jitter_cannot_report_completeness_above_one():
    """+5s then -5s spans 290,000 ms — under the raw endpoints that is `expected_rows=1`
    for two rows, i.e. 200% complete."""
    report = canonical.validate_metrics(
        slot_frame(["2024-05-01 00:00:05", "2024-05-01 00:04:55"]))
    assert report.rows == 2
    assert report.expected_rows == 2
    assert report.completeness == 1.0


def test_two_raw_stamps_in_one_slot_are_duplicate_observation_keys():
    report = canonical.validate_metrics(
        slot_frame(["2024-05-01 00:00:00", "2024-05-01 00:00:02"]))
    assert report.duplicates == 1
    assert "duplicate" in " ".join(report.violations)


def test_jitter_beyond_tolerance_cannot_pass_card_time_validation():
    """`card` re-validates the Parquet it is about to register. A stamp too far from the
    grid must not slip through that second gate."""
    report = canonical.validate_metrics(
        slot_frame(["2024-05-01 00:00:00", "2024-05-01 00:05:06"]))
    assert not report.ok
    assert "grid" in " ".join(report.violations).lower()


def test_the_reported_range_is_the_raw_timestamps():
    """Slots are identity. The range a reader is shown is what the venue reported."""
    report = canonical.validate_metrics(
        slot_frame(["2024-05-01 00:00:02", "2024-05-01 00:05:00"]))
    assert report.start.startswith("2024-05-01T00:00:02")


def test_there_is_one_metrics_grid_constant():
    assert not hasattr(canonical, "METRICS_SLOT_MS")
    assert not hasattr(canonical, "METRICS_STEP_MS")
    assert canonical.METRICS_GRID_MS == 300_000


# ── ownership failures reach the operator as a refusal ────────────────────────
def metrics_invoke(*args):
    from typer.testing import CliRunner
    return CliRunner().invoke(data_cli.app, list(args))


@pytest.mark.parametrize("rows,expect", [
    ([metrics_row("2024-05-01 00:05:00", oi=1.0),
      metrics_row("2024-05-01 00:05:00", oi=2.0)], "open_interest"),
    ([metrics_row("2024-05-01 00:00:00"),
      metrics_row("2024-05-01 00:00:06")], "grid"),
], ids=["conflicting candidates", "excessive jitter"])
def test_an_ownership_failure_is_a_clean_cli_refusal(metrics_archive, tmp_path,
                                                     monkeypatch, rows, expect):
    monkeypatch.setattr(canonical, "CATALOG_ROOT", tmp_path / "catalog")
    metrics_archive(rows, D1)
    result = metrics_invoke("build-metrics", "BTCUSDT",
                            "--start", "2024-05-01", "--end", "2024-05-01")
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in result.output
    assert expect in " ".join(result.output.split())


def test_an_ownership_failure_is_a_build_refusal_at_the_api(metrics_archive, tmp_path):
    rows = [metrics_row("2024-05-01 00:05:00", oi=1.0),
            metrics_row("2024-05-01 00:05:00", oi=2.0)]
    with pytest.raises(canonical.BuildRefused) as caught:
        canonical.build_metrics([metrics_archive(rows, D1)], tmp_path / "m.parquet")
    assert "open_interest" in str(caught.value)


# ── a violating report must not reach the writer at all ───────────────────────
def test_a_violating_report_never_rechunks_or_writes(monkeypatch, tmp_path):
    """Staging a file for a frame already known to be invalid does work that can only be
    thrown away, and it puts bytes on disk beside a good L1."""
    calls: list[str] = []
    real_rechunk = pl.DataFrame.rechunk
    real_write = pl.DataFrame.write_parquet
    monkeypatch.setattr(pl.DataFrame, "rechunk",
                        lambda self, *a, **k: (calls.append("rechunk"),
                                               real_rechunk(self, *a, **k))[1])
    monkeypatch.setattr(pl.DataFrame, "write_parquet",
                        lambda self, *a, **k: (calls.append("write"),
                                               real_write(self, *a, **k))[1])
    frame = pl.DataFrame({"ts_ms": [1]}, schema={"ts_ms": pl.Int64})
    out = tmp_path / "sub" / "x.parquet"
    with pytest.raises(canonical.BuildRefused):
        canonical._validate_and_write(
            frame, lambda f: canonical.QualityReport(1, "", "", 1,
                                                     violations=["deliberate"]), out)
    assert calls == []
    assert not out.exists()
    assert not out.parent.exists() or list(out.parent.glob("*")) == []


# ── computed facts must survive to the card, not just the in-memory report ────
def notes_section(card_text: str) -> str:
    """Only the `## Notes` block. The prose in SEMANTICS names these keys too, so a search
    over the whole card finds them whether or not a single value was persisted."""
    after = card_text.split("\n## Notes\n", 1)[1]
    return after.split("\n## ", 1)[0]


@pytest.fixture
def metrics_desk(tmp_path, monkeypatch, metrics_archive):
    from supercortex.registry import db
    registry = tmp_path / "registry.sqlite"
    db.init(registry)
    monkeypatch.setattr(db, "DB_PATH", registry)
    monkeypatch.setattr(db, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(canonical, "CATALOG_ROOT", tmp_path / "data" / "catalog")
    monkeypatch.setattr(data_cli, "CARDS_DIR", tmp_path / "05-data")
    (tmp_path / "05-data").mkdir()

    prior = metrics_archive([metrics_row("2024-05-01 23:50:00"),
                             metrics_row("2024-05-01 23:50:00"),
                             metrics_row("2024-05-02 00:00:00", oi=111.0),
                             metrics_row("2024-05-02 00:05:03", oi=333.0)], D1)
    owner = metrics_archive([metrics_row("2024-05-02 00:00:00", oi=222.0)], D2)
    out = tmp_path / "data" / "catalog" / "metrics" / "BTCUSDT.um" / "metrics.parquet"
    _path, report = canonical.build_metrics([prior, owner], out)
    return tmp_path, out, report


def test_the_build_facts_are_persisted_with_the_parquet(metrics_desk):
    """The card command re-reads the file and builds a fresh report; anything held only in
    the build's memory is gone by then."""
    _root, out, report = metrics_desk
    stored = canonical.stored_facts(out)
    for key in ("exact_repeat_rows_removed", "non_owner_candidates_ignored",
                "delivery_fallback_rows", "max_grid_jitter_ms"):
        assert stored[key] == report.notes[key], key


def test_the_card_notes_carry_the_computed_values(metrics_desk):
    root, _out, report = metrics_desk
    from typer.testing import CliRunner
    result = CliRunner().invoke(data_cli.app, ["card", "BTCUSDT", "--kind", "metrics"])
    assert result.exit_code == 0, result.output
    notes = notes_section(next((root / "05-data").glob("*.md"))
                          .read_text(encoding="utf-8"))
    for key in ("exact_repeat_rows_removed", "non_owner_candidates_ignored",
                "delivery_fallback_rows", "max_grid_jitter_ms"):
        assert f"**{key}**: `{report.notes[key]}`" in notes, f"{key} missing from ## Notes"
    assert report.notes["exact_repeat_rows_removed"] == 1
    assert report.notes["delivery_fallback_rows"] == 1


def test_a_foreign_or_malformed_metadata_key_is_ignored(metrics_desk):
    """The sidecar is untrusted input like any other file: only known keys with the right
    type are merged into a report a card is written from."""
    import json
    _root, out, _report = metrics_desk
    frame = pl.read_parquet(out)
    frame.write_parquet(out, compression="zstd", statistics=True, metadata={
        canonical.FACTS_KEY: json.dumps({
            "exact_repeat_rows_removed": 4,
            "delivery_fallback_rows": "not a number",
            "rows": 999_999,
            "unexpected_key": 1,
        })})
    stored = canonical.stored_facts(out)
    assert stored == {"exact_repeat_rows_removed": 4}


def test_other_dataset_kinds_carry_no_metadata_and_are_byte_identical(kline_archive,
                                                                      tmp_path):
    """Adding a metrics sidecar must not perturb any other kind's bytes."""
    from supercortex.registry import db
    a = kline_archive(kline_rows(6))
    first, _ = canonical.build([a], "1m", tmp_path / "one.parquet")
    digest = db.sha256_file(first)
    second, _ = canonical.build([a], "1m", tmp_path / "two.parquet")
    assert db.sha256_file(second) == digest
    assert canonical.stored_facts(second) == {}


# ══════════════════════════════════════════════════════════════════════════════
# carding a metrics dataset fails closed on its build facts
# ══════════════════════════════════════════════════════════════════════════════
# The card's prose states all four computed facts appear under Notes. Registering one whose
# metadata is missing or unusable publishes that claim without the evidence — and
# `stored_facts` is deliberately lenient, so it cannot be the thing that refuses.
def rewrite_metadata(path: Path, payload: str | None) -> None:
    frame = pl.read_parquet(path)
    frame.write_parquet(path, compression="zstd", statistics=True,
                        metadata=None if payload is None
                        else {canonical.FACTS_KEY: payload})


def registry_is_untouched(root: Path) -> None:
    from supercortex.registry import db
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dataset").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM id_counter WHERE prefix='DATA'").fetchone()[0] == 0
    assert list((root / "05-data").glob("*.md")) == []


GOOD_FACTS = {"exact_repeat_rows_removed": 1, "non_owner_candidates_ignored": 0,
              "delivery_fallback_rows": 1, "max_grid_jitter_ms": 3000}


@pytest.mark.parametrize("payload,expect", [
    (None, "no build facts"),
    ("{not json", "could not be read"),
    (json.dumps({k: v for k, v in GOOD_FACTS.items()
                 if k != "delivery_fallback_rows"}), "delivery_fallback_rows"),
    (json.dumps({**GOOD_FACTS, "max_grid_jitter_ms": "3000"}), "max_grid_jitter_ms"),
    (json.dumps({**GOOD_FACTS, "exact_repeat_rows_removed": -1}),
     "exact_repeat_rows_removed"),
    (json.dumps({**GOOD_FACTS, "non_owner_candidates_ignored": True}),
     "non_owner_candidates_ignored"),
    (json.dumps({**GOOD_FACTS, "max_grid_jitter_ms": 9_999}), "tolerance"),
], ids=["absent", "malformed json", "missing key", "wrong type", "negative",
        "bool is not an int", "jitter beyond tolerance"])
def test_carding_metrics_refuses_unusable_build_facts(metrics_desk, payload, expect):
    root, out, _report = metrics_desk
    rewrite_metadata(out, payload)

    from typer.testing import CliRunner
    result = CliRunner().invoke(data_cli.app, ["card", "BTCUSDT", "--kind", "metrics"])
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    said = " ".join(result.output.split())
    assert "Traceback" not in said
    assert expect in said
    assert "rebuild" in said.lower()
    registry_is_untouched(root)


def test_carding_metrics_succeeds_when_the_facts_are_complete(metrics_desk):
    from supercortex.registry import db
    root, _out, report = metrics_desk
    from typer.testing import CliRunner
    assert CliRunner().invoke(
        data_cli.app, ["card", "BTCUSDT", "--kind", "metrics"]).exit_code == 0
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dataset").fetchone()[0] == 1
    notes = notes_section(next((root / "05-data").glob("*.md"))
                          .read_text(encoding="utf-8"))
    for key in canonical.REQUIRED_METRICS_FACTS:
        assert f"**{key}**: `{report.notes[key]}`" in notes


def test_the_strict_reader_distinguishes_absent_from_valid(metrics_desk, tmp_path):
    """`stored_facts` stays lenient — it is a parser, not a gate. The gate has to be able
    to tell 'this file records nothing' from 'this file records four good numbers'."""
    _root, out, _report = metrics_desk
    assert set(canonical.require_metrics_facts(out)) == set(
        canonical.REQUIRED_METRICS_FACTS)
    rewrite_metadata(out, None)
    assert canonical.stored_facts(out) == {}
    with pytest.raises(canonical.FactsUnavailable):
        canonical.require_metrics_facts(out)


# ── ownership diagnostics must not invent a symbol ────────────────────────────
def test_the_ineligible_diagnostic_names_the_real_symbol(tmp_path, monkeypatch):
    """The command takes any symbol. A hardcoded `BTCUSDT-metrics-...` would name an
    archive that does not exist and send the operator looking for it."""
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")

    def make(rows: list[str], day: date) -> sources.Archive:
        a = sources.Archive("um", "ETHUSDT", "metrics", day)
        a.local.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(a.local, "w") as zf:
            zf.writestr(f"{a.stem}.csv", "\n".join(rows) + "\n")
        return a

    prior = make([metrics_row("2024-05-01 23:55:00"),
                  metrics_row("2024-05-02 00:00:02", oi=333.0)], D1)
    with pytest.raises(canonical.MetricsOwnershipError) as caught:
        canonical.canonical_metrics([prior])
    said = str(caught.value)
    assert "ETHUSDT-metrics-2024-05-02" in said      # the expected owner
    assert "ETHUSDT-metrics-2024-05-01" in said      # the supplied source
    assert "BTCUSDT" not in said
