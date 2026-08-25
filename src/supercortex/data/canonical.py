"""L0 → L1: normalise venue archives into typed, validated, gap-inventoried Parquet.

L1 is rebuildable from L0 by this module alone. If `supercortex data rebuild` ever stops
reproducing L1 byte-for-byte, we have a data swamp rather than a data lake.
"""
from __future__ import annotations

import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

import polars as pl

from supercortex.data.sources import Archive
from supercortex.registry.db import PROJECT_ROOT

CATALOG_ROOT = PROJECT_ROOT / "data" / "catalog"

# Binance kline CSVs are headerless in older archives and headed in newer ones.
KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}


def _to_millis(column: pl.Expr) -> pl.Expr:
    """Normalise Binance epoch timestamps to milliseconds.

    Verified by probing data.binance.vision on 2026-08-24:

        futures/um klines  2024-01 … 2026-06   headed,    milliseconds (13 digits)
        spot       klines  2024-06             headerless, milliseconds (13 digits)
        spot       klines  2026-06             headerless, MICROseconds (16 digits)
        futures/um fundingRate 2026-06         headed,    milliseconds

    So spot klines switched to microseconds while USD-M futures klines did not. Concatenate
    the two eras without normalising and half your history lands in 1970 or 58,000 AD —
    silently, because every value is still a plausible integer.

    Detect by magnitude, never by filename or date: a millisecond epoch for any date this
    century is < 1e13, a microsecond epoch is > 1e15.
    """
    return pl.when(column > 1e13).then(column // 1000).otherwise(column).cast(pl.Int64)


def read_klines(archive: Archive) -> pl.DataFrame:
    """Read one kline archive into a typed frame. Raises on structural surprise."""
    with zipfile.ZipFile(archive.local) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"{archive.stem}: expected 1 csv, found {len(names)}")
        raw = zf.read(names[0])

    first = raw.split(b"\n", 1)[0].decode("utf-8", "replace")
    has_header = "open_time" in first.lower()

    frame = pl.read_csv(
        raw,
        has_header=has_header,
        new_columns=None if has_header else KLINE_COLUMNS,
        schema_overrides={c: pl.Float64 for c in
                          ("open", "high", "low", "close", "volume",
                           "quote_volume", "taker_buy_base", "taker_buy_quote")},
        truncate_ragged_lines=False,
    )
    if frame.width != len(KLINE_COLUMNS):
        raise ValueError(
            f"{archive.stem}: expected {len(KLINE_COLUMNS)} columns, got {frame.width}"
        )
    if has_header:
        frame = frame.rename(dict(zip(frame.columns, KLINE_COLUMNS, strict=True)))

    return (
        frame.select(
            _to_millis(pl.col("open_time")).alias("ts_ms"),
            pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close"),
            pl.col("volume"), pl.col("quote_volume"),
            pl.col("trades").cast(pl.Int64),
            pl.col("taker_buy_base"), pl.col("taker_buy_quote"),
        )
        .with_columns(
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.replace_time_zone("UTC").alias("ts")
        )
        .sort("ts_ms")
    )



#: One missing stretch. Serialised verbatim into `gaps_json` in registry.sqlite and sliced
#: into dataset cards, so the keys and their types are a published contract. `from` is a
#: Python keyword, hence the functional spelling.
Gap = TypedDict("Gap", {"from": str, "to": str, "missing_bars": int})


def _epoch_bounds(ts: pl.Series) -> tuple[int, int]:
    """The first and last epoch-millisecond in `ts`, as ints.

    Every reader ends its timestamp expression with `.cast(pl.Int64)`, so `ts_ms` is Int64
    by construction and `Series.min()/max()` return Python ints. Polars cannot express that
    in its return type — a Series may hold any dtype, so the annotation is the union of all
    of them — and the validators need real ints to subtract, floor-divide by the step, and
    divide by 1000 for the ISO range.

    Checking the invariant is what makes narrowing it honest. A Float64 or all-null `ts_ms`
    means the reader was bypassed; without this it produced a plausible-looking
    QualityReport from a broken frame, which is the one outcome this desk cannot tolerate.
    """
    if ts.dtype != pl.Int64:
        raise TypeError(
            f"ts_ms must be Int64 epoch milliseconds, got {ts.dtype}. Build L1 through "
            f"the readers in this module — they cast; nothing downstream re-checks."
        )
    low, high = ts.min(), ts.max()
    if not isinstance(low, int) or not isinstance(high, int):
        raise TypeError("ts_ms has no non-null values, so its range is undefined")
    return low, high


def _gap_scan(frame: pl.DataFrame, step_ms: int) -> tuple[list[Gap], int]:
    """Find genuinely missing intervals, and count off-grid jitter separately.

    Flooring the delta conflates the two. Binance metrics observations are 1-2 seconds
    off the 5-minute grid 42 times in six years; `d // step - 1` reports each of those as
    a "gap of 0", which is noise that hides the 155 real gaps underneath it. Rounding
    asks the right question: how many observations are missing between these two?
    """
    deltas = frame.select(
        pl.col("ts_ms").alias("t"), pl.col("ts_ms").diff().alias("d")
    ).drop_nulls()

    gaps: list[Gap] = []
    off_grid = 0
    for row in deltas.iter_rows(named=True):
        missing = round(row["d"] / step_ms) - 1
        if missing >= 1:
            gaps.append({
                "from": datetime.fromtimestamp((row["t"] - row["d"]) / 1000, UTC).isoformat(),
                "to": datetime.fromtimestamp(row["t"] / 1000, UTC).isoformat(),
                "missing_bars": missing,
            })
        elif row["d"] != step_ms:
            off_grid += 1
    return gaps, off_grid


# ── quality contract ────────────────────────────────────────────────────────────
@dataclass
class QualityReport:
    """What we know about a dataset, stated so it can be argued with."""

    rows: int
    start: str
    end: str
    expected_rows: int
    gaps: list[Gap] = field(default_factory=list)
    duplicates: int = 0
    non_monotonic: int = 0
    violations: list[str] = field(default_factory=list)
    notes: dict[str, object] = field(default_factory=dict)

    @property
    def completeness(self) -> float:
        return self.rows / self.expected_rows if self.expected_rows else 0.0

    @property
    def ok(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict[str, object]:
        return {
            "rows": self.rows, "start": self.start, "end": self.end,
            "expected_rows": self.expected_rows,
            "completeness": round(self.completeness, 6),
            "gap_count": len(self.gaps),
            "missing_bars": sum(g["missing_bars"] for g in self.gaps),
            "duplicates": self.duplicates,
            "non_monotonic": self.non_monotonic,
            "violations": self.violations,
            **self.notes,
        }


def validate(frame: pl.DataFrame, interval: str) -> QualityReport:
    """Every claim we make about a dataset, checked. Gaps are inventoried, not hidden.

    A gap is not automatically a defect — venues halt, symbols relist. But an
    *unexplained* gap is, and you cannot explain what you never listed.
    """
    step = INTERVAL_MS[interval]
    violations: list[str] = []

    if frame.height == 0:
        return QualityReport(0, "", "", 0, violations=["empty dataset"])

    ts = frame["ts_ms"]
    duplicates = frame.height - ts.n_unique()
    non_monotonic = int((ts.diff().drop_nulls() <= 0).sum())

    gaps, off_grid = _gap_scan(frame, step)

    low, high = _epoch_bounds(ts)
    expected = (high - low) // step + 1

    if duplicates:
        violations.append(f"{duplicates} duplicate timestamps")
    if non_monotonic:
        violations.append(f"{non_monotonic} non-monotonic timestamps")

    bad_ohlc = frame.filter(
        (pl.col("high") < pl.col("low"))
        | (pl.col("high") < pl.col("open")) | (pl.col("high") < pl.col("close"))
        | (pl.col("low") > pl.col("open")) | (pl.col("low") > pl.col("close"))
    ).height
    if bad_ohlc:
        violations.append(f"{bad_ohlc} bars violate low <= open/close <= high")

    if frame.filter(pl.col("volume") < 0).height:
        violations.append("negative volume")
    if frame.filter(
        (pl.col("open") <= 0) | (pl.col("high") <= 0)
        | (pl.col("low") <= 0) | (pl.col("close") <= 0)
    ).height:
        violations.append("non-positive prices")

    return QualityReport(
        rows=frame.height,
        start=datetime.fromtimestamp(low / 1000, UTC).isoformat(),
        end=datetime.fromtimestamp(high / 1000, UTC).isoformat(),
        expected_rows=expected,
        gaps=gaps,
        duplicates=duplicates,
        non_monotonic=non_monotonic,
        violations=violations,
        notes={"off_grid": off_grid} if off_grid else {},
    )


def build(archives: list[Archive], interval: str, out: Path) -> tuple[Path, QualityReport]:
    """Concatenate L0 archives into one validated L1 Parquet file."""
    frames = [read_klines(a) for a in archives if a.local.exists()]
    if not frames:
        raise RuntimeError("no L0 archives present — run `supercortex data fetch` first")

    combined = pl.concat(frames).unique(subset=["ts_ms"], keep="first").sort("ts_ms")
    report = validate(combined, interval)

    out.parent.mkdir(parents=True, exist_ok=True)
    combined.write_parquet(out, compression="zstd", statistics=True)
    return out, report


def expected_bar_count(start: datetime, end: datetime, interval: str) -> int:
    return int((end - start) / timedelta(milliseconds=INTERVAL_MS[interval])) + 1


# ── funding rate ────────────────────────────────────────────────────────────────
FUNDING_COLUMNS = ["calc_time", "funding_interval_hours", "last_funding_rate"]


def read_funding(archive: Archive) -> pl.DataFrame:
    """Read one monthly fundingRate archive.

    Funding is a *cash flow*, not an indicator: longs pay shorts (or vice versa) every
    interval. Modelling it as a signal and forgetting the payment is a classic way to
    build a perp backtest that is wrong in the direction of optimism.
    """
    with zipfile.ZipFile(archive.local) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"{archive.stem}: expected 1 csv, found {len(names)}")
        raw = zf.read(names[0])

    has_header = b"calc_time" in raw.split(b"\n", 1)[0]
    frame = pl.read_csv(
        raw, has_header=has_header,
        new_columns=None if has_header else FUNDING_COLUMNS,
        schema_overrides={"last_funding_rate": pl.Float64},
    )
    if has_header:
        frame = frame.rename(dict(zip(frame.columns, FUNDING_COLUMNS, strict=True)))

    return (
        frame.select(
            _to_millis(pl.col("calc_time")).alias("ts_ms"),
            pl.col("funding_interval_hours").cast(pl.Int32),
            pl.col("last_funding_rate").alias("funding_rate"),
        )
        .with_columns(
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.replace_time_zone("UTC").alias("ts")
        )
        .sort("ts_ms")
    )


def validate_funding(frame: pl.DataFrame) -> QualityReport:
    """Funding events should arrive on the venue's stated interval, with no duplicates."""
    violations: list[str] = []
    if frame.height == 0:
        return QualityReport(0, "", "", 0, violations=["empty dataset"])

    ts = frame["ts_ms"]
    duplicates = frame.height - ts.n_unique()
    non_monotonic = int((ts.diff().drop_nulls() <= 0).sum())

    intervals = frame["funding_interval_hours"].unique().sort().to_list()
    # Binance has changed the funding interval per symbol (8h -> 4h -> 1h on some pairs).
    # Assuming a constant 8h and annualising by 3x/day silently misprices those windows.
    step_ms = int(frame["funding_interval_hours"].mode()[0]) * 3_600_000

    gaps, off_grid = _gap_scan(frame, step_ms)

    low, high = _epoch_bounds(ts)
    expected = (high - low) // step_ms + 1

    if duplicates:
        violations.append(f"{duplicates} duplicate funding timestamps")
    if non_monotonic:
        violations.append(f"{non_monotonic} non-monotonic timestamps")
    if frame.filter(pl.col("funding_rate").abs() > 0.05).height:
        violations.append("funding rate beyond +/-5% — implausible, check units")

    report = QualityReport(
        rows=frame.height,
        start=datetime.fromtimestamp(low / 1000, UTC).isoformat(),
        end=datetime.fromtimestamp(high / 1000, UTC).isoformat(),
        expected_rows=expected, gaps=gaps,
        duplicates=duplicates, non_monotonic=non_monotonic, violations=violations,
    )
    report.notes["funding_intervals_seen"] = intervals
    if off_grid:
        report.notes["off_grid"] = off_grid
    return report


# ── mark-price klines ───────────────────────────────────────────────────────────
# The venue publishes the mark-price series in the same twelve-field envelope as a traded
# kline, and its documentation lists fields 6 and 8-12 as **Ignore**. They are named
# `ignore_*` here for exactly that reason: calling field 6 `volume` and field 9 `trades`
# would assert semantics Binance explicitly withholds, on a series that has neither, and a
# column named `volume` is a number somebody eventually believes.
MARK_PRICE_RAW_COLUMNS = [
    "open_time", "open", "high", "low", "close",
    "ignore_1", "close_time", "ignore_2", "ignore_3", "ignore_4", "ignore_5", "ignore_6",
]

#: What L1 keeps. Only the documented, meaningful fields survive.
MARK_PRICE_COLUMNS = ["ts_ms", "open", "high", "low", "close", "ts"]

MARK_PRICE_STEP_MS = 60_000


def read_mark_price(archive: Archive) -> pl.DataFrame:
    """Read one monthly mark-price kline archive into a typed frame.

    Deliberately NOT `read_klines`. The envelopes are identical and the meanings are not:
    a traded kline's volume, quote volume, trade count and taker splits are real
    quantities, and on a mark-price series the venue publishes zeros and Ignore in those
    positions. Sharing the parser would carry those names into L1.

    This function stores what Binance publishes. It makes no claim that `open` is the mark
    price used to settle a funding payment - see the dataset card.
    """
    with zipfile.ZipFile(archive.local) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"{archive.stem}: expected 1 csv, found {len(names)}")
        raw = zf.read(names[0])

    first = raw.split(b"\n", 1)[0].decode("utf-8", "replace")
    has_header = "open_time" in first.lower()

    # Width is checked BEFORE names are applied. Handing polars a twelve-name list for a
    # six-column frame raises a ShapeError from inside the reader, which names neither the
    # archive nor the expectation - and an archive whose shape changed is precisely the
    # thing this reader must report clearly rather than coerce.
    frame = pl.read_csv(raw, has_header=has_header, truncate_ragged_lines=False)
    if frame.width != len(MARK_PRICE_RAW_COLUMNS):
        raise ValueError(
            f"{archive.stem}: expected {len(MARK_PRICE_RAW_COLUMNS)} columns, "
            f"got {frame.width}"
        )
    frame = frame.rename(dict(zip(frame.columns, MARK_PRICE_RAW_COLUMNS, strict=True)))

    return (
        frame.select(
            _to_millis(pl.col("open_time")).alias("ts_ms"),
            # Cast rather than override on read: a month whose prices are all integral
            # (`43158`) infers as Int64, and a column that is Float64 in one month and
            # Int64 in the next cannot be concatenated with the rest.
            pl.col("open").cast(pl.Float64), pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64), pl.col("close").cast(pl.Float64),
        )
        .with_columns(
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms")
            .dt.replace_time_zone("UTC").alias("ts")
        )
        .sort("ts_ms")
    )


def validate_mark_price(frame: pl.DataFrame) -> QualityReport:
    """What we know about a mark-price series, stated so it can be argued with.

    The archive is published on an exact one-minute grid, so an off-grid timestamp is not
    jitter to absorb - it means the reader was bypassed or the archive changed shape.
    Nothing here fills a gap: a missing minute is inventoried and stays missing.
    """
    violations: list[str] = []
    if frame.height == 0:
        return QualityReport(0, "", "", 0, violations=["empty dataset"])

    prices = ("open", "high", "low", "close")
    required = ("ts_ms", *prices)
    empty = [name for name in required if frame[name].null_count()]
    if empty:
        violations.append(f"null values in required fields: {', '.join(empty)}")

    ts = frame["ts_ms"]
    duplicates = frame.height - ts.n_unique()
    non_monotonic = int((ts.diff().drop_nulls() <= 0).sum())
    if duplicates:
        violations.append(f"{duplicates} duplicate timestamps")
    if non_monotonic:
        violations.append(f"{non_monotonic} non-monotonic timestamps")

    off_grid_stamps = frame.filter(pl.col("ts_ms") % MARK_PRICE_STEP_MS != 0).height
    if off_grid_stamps:
        violations.append(
            f"{off_grid_stamps} timestamps are not on the one-minute grid")

    non_finite = frame.filter(
        pl.any_horizontal(
            (pl.col(c).is_not_null() & (pl.col(c).is_nan() | pl.col(c).is_infinite()))
            for c in prices
        )
    ).height
    if non_finite:
        violations.append(f"{non_finite} rows carry a non-finite price")

    non_positive = frame.filter(
        pl.any_horizontal((pl.col(c).is_not_null() & (pl.col(c) <= 0)) for c in prices)
    ).height
    if non_positive:
        violations.append(f"{non_positive} rows carry a non-positive price")

    bad_ohlc = frame.filter(
        (pl.col("high") < pl.col("low"))
        | (pl.col("high") < pl.col("open")) | (pl.col("high") < pl.col("close"))
        | (pl.col("low") > pl.col("open")) | (pl.col("low") > pl.col("close"))
    ).height
    if bad_ohlc:
        violations.append(f"{bad_ohlc} bars violate low <= open/close <= high")

    gaps, off_grid = _gap_scan(frame, MARK_PRICE_STEP_MS)
    low, high = _epoch_bounds(ts)
    expected = (high - low) // MARK_PRICE_STEP_MS + 1

    return QualityReport(
        rows=frame.height,
        start=datetime.fromtimestamp(low / 1000, UTC).isoformat(),
        end=datetime.fromtimestamp(high / 1000, UTC).isoformat(),
        expected_rows=expected,
        gaps=gaps,
        duplicates=duplicates,
        non_monotonic=non_monotonic,
        violations=violations,
        notes={"off_grid": off_grid} if off_grid else {},
    )


# ── metrics: open interest and crowding ratios ──────────────────────────────────
METRICS_COLUMNS = [
    "create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
    "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
    "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
]
METRICS_STEP_MS = 300_000  # 5 minutes


def read_metrics(archive: Archive) -> pl.DataFrame:
    """Read one daily metrics archive: open interest plus long/short crowding ratios.

    `create_time` is a naive datetime STRING ("2026-06-15 00:00:00"), not an epoch — a
    different shape from every other Binance archive. It is UTC, and it is parsed as UTC
    explicitly: this machine runs Asia/Kolkata (+5:30), so a naive parse would shift every
    observation by five and a half hours. See .claude/rules/data.md.
    """
    with zipfile.ZipFile(archive.local) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"{archive.stem}: expected 1 csv, found {len(names)}")
        raw = zf.read(names[0])

    has_header = b"create_time" in raw.split(b"\n", 1)[0]
    frame = pl.read_csv(
        raw, has_header=has_header,
        new_columns=None if has_header else METRICS_COLUMNS,
        schema_overrides={c: pl.Float64 for c in METRICS_COLUMNS if c not in
                          ("create_time", "symbol")},
    )
    if has_header:
        frame = frame.rename(dict(zip(frame.columns, METRICS_COLUMNS, strict=True)))

    return (
        frame.with_columns(
            pl.col("create_time")
            .str.strptime(pl.Datetime, "%Y-%m-%d %H:%M:%S")
            .dt.replace_time_zone("UTC")          # the string IS UTC; say so explicitly
            .alias("ts")
        )
        .select(
            (pl.col("ts").dt.epoch(time_unit="ms")).alias("ts_ms"),
            pl.col("ts"),
            pl.col("sum_open_interest").alias("open_interest"),
            pl.col("sum_open_interest_value").alias("open_interest_value"),
            pl.col("count_toptrader_long_short_ratio").alias("toptrader_accounts_ls"),
            pl.col("sum_toptrader_long_short_ratio").alias("toptrader_positions_ls"),
            pl.col("count_long_short_ratio").alias("all_accounts_ls"),
            pl.col("sum_taker_long_short_vol_ratio").alias("taker_volume_ls"),
        )
        .sort("ts_ms")
    )


def validate_metrics(frame: pl.DataFrame) -> QualityReport:
    """Metrics arrive on a 5-minute grid: 288 observations per day."""
    violations: list[str] = []
    if frame.height == 0:
        return QualityReport(0, "", "", 0, violations=["empty dataset"])

    ts = frame["ts_ms"]
    duplicates = frame.height - ts.n_unique()
    non_monotonic = int((ts.diff().drop_nulls() <= 0).sum())

    gaps, off_grid = _gap_scan(frame, METRICS_STEP_MS)

    low, high = _epoch_bounds(ts)
    if duplicates:
        violations.append(f"{duplicates} duplicate timestamps")
    if non_monotonic:
        violations.append(f"{non_monotonic} non-monotonic timestamps")
    if frame.filter(pl.col("open_interest") < 0).height:
        violations.append("negative open interest")

    # The crowding ratios are sparser than open interest itself: Binance leaves them null
    # in ~0.9% of observations. Not a defect, but a strategy that reads them must handle
    # the hole rather than forward-filling it into a stale signal.
    null_notes: dict[str, object] = {}
    for column in ("toptrader_accounts_ls", "toptrader_positions_ls",
                   "all_accounts_ls", "taker_volume_ls"):
        if column in frame.columns:
            nulls = int(frame[column].null_count())
            if nulls:
                null_notes[f"nulls_{column}"] = nulls

    return QualityReport(
        rows=frame.height,
        start=datetime.fromtimestamp(low / 1000, UTC).isoformat(),
        end=datetime.fromtimestamp(high / 1000, UTC).isoformat(),
        expected_rows=(high - low) // METRICS_STEP_MS + 1, gaps=gaps,
        duplicates=duplicates, non_monotonic=non_monotonic, violations=violations,
        notes={**({"off_grid": off_grid} if off_grid else {}), **null_notes},
    )


class BuildRefused(RuntimeError):
    """L1 was not written, and the existing one was not touched.

    Carries the report when one exists, so the caller can show what the validator found
    rather than only that it objected.
    """

    def __init__(self, message: str, report: QualityReport | None = None) -> None:
        super().__init__(message)
        self.report = report


def build_generic(
    archives: list[Archive],
    reader: Callable[[Archive], pl.DataFrame],
    validator: Callable[[pl.DataFrame], QualityReport],
    out: Path,
) -> tuple[Path, QualityReport]:
    """Concatenate the requested L0 archives into one validated L1 Parquet file.

    Three things this function must not do, each of which it used to:

    * **Deduplicate.** `unique(subset=["ts_ms"])` erased exactly the defect the validators
      exist to find, and it chose which of two rows claiming the same instant to keep - a
      decision nobody made and nobody could see. Every row read is a row validated.
    * **Build from whatever happened to be on disk.** Skipping absent archives measures
      completeness between the observed first and last row, so a truncated window reports
      as complete. The requested span is what was asked for.
    * **Write before validating.** The canonical file was replaced and only then checked,
      so a refused build left the invalid result in place of the good one.
    """
    if not archives:
        raise BuildRefused(
            "no archives requested, so there is nothing to build and no span to check"
        )

    missing = [a for a in archives if not a.local.exists()]
    if missing:
        raise BuildRefused(
            f"{len(missing)} of {len(archives)} requested archives are not in L0: "
            f"{', '.join(a.stem for a in missing)}. Fetch them first — a build that "
            f"silently skips a month reports the months it did read as complete."
        )

    frames = []
    for archive in archives:
        try:
            frames.append(reader(archive))
        except Exception as exc:
            raise BuildRefused(
                f"{archive.stem} is present but could not be read: {exc}"
            ) from exc

    # No dedup: two rows claiming one instant both reach the validator. The sort is stable
    # so that a report cannot depend on an arbitrary tie order — belt and braces rather
    # than a load-bearing guarantee, since a frame carrying ties is refused before
    # anything is persisted, and no test can observe the difference.
    combined = pl.concat(frames).sort("ts_ms", maintain_order=True)
    report = validator(combined)

    out.parent.mkdir(parents=True, exist_ok=True)
    staged = out.with_name(out.name + ".building")
    try:
        combined.write_parquet(staged, compression="zstd", statistics=True)
        if report.violations:
            raise BuildRefused(
                f"{out.name} was not written: {'; '.join(report.violations)}", report
            )
        # Same directory, so this is an atomic rename rather than a copy: either the old
        # L1 is intact or the new one is complete, never a half-written file.
        staged.replace(out)
    finally:
        staged.unlink(missing_ok=True)
    return out, report
