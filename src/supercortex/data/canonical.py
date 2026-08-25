"""L0 → L1: normalise venue archives into typed, validated, gap-inventoried Parquet.

L1 is rebuildable from L0 by this module alone. If `supercortex data rebuild` ever stops
reproducing L1 byte-for-byte, we have a data swamp rather than a data lake.
"""
from __future__ import annotations

import dataclasses
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
    """Concatenate the requested kline archives into one validated L1 Parquet file.

    A thin binding over `build_generic`, which owns the guarantees: every requested
    archive present and readable, no row discarded before validation, and the canonical
    file replaced only after the validator is satisfied.

    This used to be a second implementation of those same steps, and it had none of them -
    it deduplicated on `ts_ms` before validating, skipped absent months, and wrote the
    file before checking violations. Two copies of one operation is how they came to
    disagree; the fix is one copy, not a matching patch.

    Only the interval is bound here, because `validate` needs it to know what a gap is and
    `build_generic` is deliberately ignorant of any particular series.
    """
    return build_generic(archives, read_klines, lambda frame: validate(frame, interval), out)


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
    # Every column as text, nothing inferred. The venue mixes integer and float spellings
    # of one value within a single archive - in 2026-03 `quote_volume` is `0` for 13,050
    # rows and then `0.00000000`, well past any sampling window - so inference locked that
    # column to i64 and then refused a value it had already ruled out. It is a column this
    # module discards and the venue documents as Ignore: its spelling must not be able to
    # reject an archive, and it must not get a vote on anyone else's dtype either.
    frame = pl.read_csv(raw, has_header=has_header, truncate_ragged_lines=False,
                        infer_schema_length=0)
    if frame.width != len(MARK_PRICE_RAW_COLUMNS):
        raise ValueError(
            f"{archive.stem}: expected {len(MARK_PRICE_RAW_COLUMNS)} columns, "
            f"got {frame.width}"
        )
    frame = frame.rename(dict(zip(frame.columns, MARK_PRICE_RAW_COLUMNS, strict=True)))

    return (
        frame.select(
            # Cast explicitly, from text. Positions are the contract - the headed archives
            # use the venue's own kline names (`count`, `taker_buy_volume`), which are not
            # the names this module gives those positions, so nothing may be selected by
            # the name the file happens to carry.
            _to_millis(pl.col("open_time").cast(pl.Int64)).alias("ts_ms"),
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



class MetricsOwnershipError(ValueError):
    """A metrics observation cannot be attributed to one archive with confidence."""


#: The published observation grid, and how far off it a stamp may sit. Binance's metrics
#: stamps jitter by a second or two; beyond a few seconds the row is not that slot's
#: observation and guessing which one it is would be inventing a measurement time.
#: The published observation grid. One constant: an observation's identity IS its slot,
#: so the step used to count expected rows and the step used to assign ownership cannot be
#: allowed to drift apart.
METRICS_GRID_MS = 300_000
METRICS_MAX_JITTER_MS = 5_000

#: Every canonical metrics field, in order. Row identity is all of them, `ts_ms` included,
#: and null counts as a value: a missing ratio and a present one are different
#: observations, not a repeat.
METRICS_CANONICAL = ("ts_ms", "ts", "open_interest", "open_interest_value",
                     "toptrader_accounts_ls", "toptrader_positions_ls",
                     "all_accounts_ls", "taker_volume_ls")


def _as_int(value: object) -> int:
    """A Series aggregate that must be an integer, checked rather than cast blindly."""
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool):
        raise MetricsOwnershipError(
            f"grid jitter aggregated to {type(value).__name__}, expected an integer "
            f"millisecond count; the slot columns are not Int64"
        )
    return value


def _stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")


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
    """What we know about a metrics series, judged on the observation grid.

    A metrics observation's identity is its five-minute **slot**, not the exact stamp the
    venue happened to publish. Counting expected rows from the raw endpoints is wrong in
    both directions: a pair jittered +5 s and -5 s spans 290,000 ms, which floor-divides to
    `expected_rows=1` for two rows and reports 200% complete. Duplicates, monotonicity and
    gaps are all questions about observations, so all four are asked of the slot.

    The reported range stays raw — that is what the venue said — and L1 keeps the raw
    stamps. Only identity is normalised.
    """
    violations: list[str] = []
    if frame.height == 0:
        return QualityReport(0, "", "", 0, violations=["empty dataset"])

    slotted = frame.with_columns(
        (((pl.col("ts_ms") + METRICS_GRID_MS // 2) // METRICS_GRID_MS) * METRICS_GRID_MS)
        .alias("_slot")
    ).with_columns((pl.col("ts_ms") - pl.col("_slot")).abs().alias("_jitter"))

    if frame.filter(pl.col("open_interest") < 0).height:
        violations.append("negative open interest")

    off_grid = slotted.filter(pl.col("_jitter") > METRICS_MAX_JITTER_MS).height
    if off_grid:
        violations.append(
            f"{off_grid} observation(s) sit further than {METRICS_MAX_JITTER_MS} ms from "
            f"the {METRICS_GRID_MS // 60_000}-minute grid"
        )

    slots = slotted["_slot"]
    duplicates = frame.height - slots.n_unique()
    non_monotonic = int((slots.diff().drop_nulls() <= 0).sum())
    if duplicates:
        violations.append(f"{duplicates} duplicate observation slots")
    if non_monotonic:
        violations.append(f"{non_monotonic} non-monotonic observation slots")

    gaps, jitter_steps = _gap_scan(
        slotted.select(pl.col("_slot").alias("ts_ms")), METRICS_GRID_MS)
    low_slot, high_slot = _epoch_bounds(slots)
    expected = (high_slot - low_slot) // METRICS_GRID_MS + 1

    raw_low, raw_high = _epoch_bounds(frame["ts_ms"])
    notes: dict[str, object] = {"off_grid": int(
        slotted.filter(pl.col("_jitter") > 0).height)}
    if jitter_steps:
        notes["irregular_slot_steps"] = jitter_steps
    for column in ("toptrader_accounts_ls", "toptrader_positions_ls",
                   "all_accounts_ls", "taker_volume_ls"):
        if column in frame.columns and frame[column].null_count():
            notes[f"nulls_{column}"] = frame[column].null_count()

    return QualityReport(
        rows=frame.height,
        start=datetime.fromtimestamp(raw_low / 1000, UTC).isoformat(),
        end=datetime.fromtimestamp(raw_high / 1000, UTC).isoformat(),
        expected_rows=expected,
        gaps=gaps,
        duplicates=duplicates,
        non_monotonic=non_monotonic,
        violations=violations,
        notes=notes,
    )


class BuildRefused(RuntimeError):
    """L1 was not written, and the existing one was not touched.

    Carries the report when one exists, so the caller can show what the validator found
    rather than only that it objected.
    """

    def __init__(self, message: str, report: QualityReport | None = None) -> None:
        super().__init__(message)
        self.report = report


def canonical_metrics(
    archives: list[Archive],
) -> tuple[pl.DataFrame, dict[str, object]]:
    """Reconcile daily metrics archives into one canonical series, with audit facts.

    The rule, in order:

    1. Every raw observation is mapped to its nearest five-minute **slot**. A stamp further
       than `METRICS_MAX_JITTER_MS` from any slot is refused - it is not that slot's
       observation, and guessing which one it is would invent a measurement time.
    2. The slot's UTC date names the **owning archive**.
    3. Candidates delivered by the owner are preferred.
    4. If the owner supplies none, a candidate delivered by another archive is kept as an
       explicit **delivery fallback**. Binance has published a slot's only observation in
       the previous day's file; dropping it would lose a real measurement and open a gap
       that nothing caused.
    5. Among the preferred candidates, rows collapse only when identical in every canonical
       field, null state included. Disagreement refuses and names the slot, the delivering
       archives and the differing fields.

    Delivery order is never a tie-break: which row appeared first says nothing about which
    is true. The retained row keeps its own timestamp - the slot is identity and ownership
    metadata, not a replacement for what the venue reported.
    """
    frames = []
    for archive in archives:
        frame = _read_all([archive], read_metrics)[0]
        slot = (((pl.col("ts_ms") + METRICS_GRID_MS // 2) // METRICS_GRID_MS)
                * METRICS_GRID_MS)
        frames.append(
            frame.with_columns(
                slot.alias("_slot"),
                pl.lit(archive.period).alias("_delivered_by"),
                pl.lit(archive.stem).alias("_source"),
            ).with_columns(
                (pl.col("ts_ms") - pl.col("_slot")).abs().alias("_jitter")
            )
        )
    combined = pl.concat(frames)

    stray = combined.filter(pl.col("_jitter") > METRICS_MAX_JITTER_MS)
    if stray.height:
        first = stray.sort("ts_ms").row(0, named=True)
        raise MetricsOwnershipError(
            f"{first['_source']} holds {stray.height} observation(s) further than "
            f"{METRICS_MAX_JITTER_MS} ms from any five-minute slot, the first at "
            f"{_stamp(first['ts_ms'])} ({first['_jitter']} ms off). A stamp that far from "
            f"the grid is not that slot's observation, and choosing a slot for it would "
            f"invent a measurement time the venue never reported."
        )

    # Measured here, over every candidate that cleared the tolerance check and before
    # preference or deduplication removes any. Taken later it would describe what survived
    # rather than what was observed - and the most jittered row is often precisely the
    # non-owner copy that gets ignored.
    max_jitter = _as_int(combined["_jitter"].max())

    combined = combined.with_columns(
        (pl.from_epoch("_slot", time_unit="ms").dt.date() == pl.col("_delivered_by"))
        .alias("_owner")
    )
    owned_slots = combined.group_by("_slot").agg(pl.col("_owner").any().alias("_has_owner"))
    combined = combined.join(owned_slots, on="_slot", how="left")

    # A fallback is only a fallback if it is a neighbour AND the owner was actually asked.
    # Without both, "the owner supplied none" is a claim about an archive that was never
    # read, and any file could vouch for any day.
    supplied = {a.period for a in archives}
    combined = combined.with_columns(
        pl.from_epoch("_slot", time_unit="ms").dt.date().alias("_owner_day")
    ).with_columns(
        ((pl.col("_owner_day") - pl.col("_delivered_by")).dt.total_days().abs() == 1)
        .alias("_adjacent"),
        pl.col("_owner_day").is_in(list(supplied)).alias("_owner_supplied"),
    )

    ineligible = combined.filter(
        ~pl.col("_owner") & ~pl.col("_has_owner")
        & (~pl.col("_adjacent") | ~pl.col("_owner_supplied"))
    )
    if ineligible.height:
        row = ineligible.sort("ts_ms").row(0, named=True)
        why = ("was not supplied, so it cannot be said to have supplied nothing"
               if not row["_owner_supplied"]
               else "is not adjacent to the delivering archive, so this is not a boundary "
                    "overlap")
        # The owner's name is derived from a supplied archive's own market, symbol and
        # kind. Spelling it out would name a file that does not exist for any symbol but
        # the one that happened to be hardcoded.
        template = archives[0]
        owner_name = dataclasses.replace(template, period=row["_owner_day"]).stem
        raise MetricsOwnershipError(
            f"{ineligible.height} observation(s) have no eligible source. At "
            f"{_stamp(int(row['_slot']))} the only candidate was delivered by "
            f"{row['_source']}, but the owning archive {owner_name} {why}."
        )

    preferred = combined.filter(pl.col("_owner") | ~pl.col("_has_owner"))
    non_owner_ignored = combined.height - preferred.height

    # Exact-row identity over the canonical fields only: the delivering archive is
    # metadata, so the same observation shipped in two files is one observation.
    # `keep="any"` because for identical rows the choice is immaterial by definition -
    # that is the absence of a tie-break, not the use of one.
    distinct = preferred.unique(subset=list(METRICS_CANONICAL), keep="any")
    exact_repeats = preferred.height - distinct.height

    clashes = (
        distinct.group_by("_slot")
        .agg(pl.len().alias("n"),
             *[pl.col(c).n_unique().alias(c) for c in METRICS_CANONICAL if c != "ts"],
             pl.col("_source").unique().sort().alias("_sources"))
        .filter(pl.col("n") > 1)
        .sort("_slot")
    )
    if clashes.height:
        row = clashes.row(0, named=True)
        fields = [c for c in METRICS_CANONICAL if c not in ("ts",) and row[c] > 1]
        raise MetricsOwnershipError(
            f"{clashes.height} five-minute slot(s) have candidates that disagree. At "
            f"{_stamp(int(row['_slot']))}, delivered by {', '.join(row['_sources'])}, they "
            f"differ in: {', '.join(fields) or 'null state'}. Two rows claiming one "
            f"observation with different values is not a repeat, and the order they "
            f"appeared in says nothing about which is right - the venue must be asked."
        )

    fallback_rows = distinct.filter(~pl.col("_has_owner")).height
    facts: dict[str, object] = {
        "exact_repeat_rows_removed": exact_repeats,
        "non_owner_candidates_ignored": non_owner_ignored,
        "delivery_fallback_rows": fallback_rows,
        # `Series.max()` is typed as the union of every dtype a Series may hold, so the
        # int is asserted rather than assumed: a non-integer here would mean `_jitter`
        # was built from something other than two Int64 columns.
        "max_grid_jitter_ms": max_jitter,
    }
    return distinct.select(METRICS_CANONICAL).sort("ts_ms"), facts


def _require_archives(archives: list[Archive]) -> None:
    """Every requested archive present, or nothing is built.

    Skipping absent ones measures completeness between the observed first and last row, so
    a truncated window reports as complete.
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


def _read_all(
    archives: list[Archive], reader: Callable[[Archive], pl.DataFrame]
) -> list[pl.DataFrame]:
    """Read every archive, naming the one that fails."""
    frames = []
    for archive in archives:
        try:
            frames.append(reader(archive))
        except BuildRefused:
            raise
        except Exception as exc:
            raise BuildRefused(
                f"{archive.stem} is present but could not be read: {exc}"
            ) from exc
    return frames


#: Where computed normalisation facts live inside the Parquet footer, and exactly which
#: keys a reader will accept back. The file is untrusted input like any other: `card`
#: builds a fresh report and merges these in, so an unknown or wrongly-typed key is
#: dropped rather than printed onto a dataset card as though the build had produced it.
FACTS_KEY = "supercortex.build_facts"
FACT_FIELDS: dict[str, type] = {
    "exact_repeat_rows_removed": int,
    "non_owner_candidates_ignored": int,
    "delivery_fallback_rows": int,
    "max_grid_jitter_ms": int,
}


#: What a metrics L1 must record about how it was normalised before it may be carded. The
#: card's prose states all four appear under Notes; registering one whose metadata cannot
#: supply them publishes that claim without the evidence.
REQUIRED_METRICS_FACTS = ("exact_repeat_rows_removed", "non_owner_candidates_ignored",
                          "delivery_fallback_rows", "max_grid_jitter_ms")


class FactsUnavailable(ValueError):
    """An L1 file records no usable build facts, so it cannot be carded."""


def require_metrics_facts(path: Path) -> dict[str, int]:
    """The four normalisation facts, or a refusal naming exactly what is wrong.

    Separate from `stored_facts` on purpose. That one is a lenient parser - it answers
    "what does this file record?" and a file recording nothing is a legitimate answer. This
    one is a gate, and it has to tell "records nothing" apart from "records four good
    numbers", which a dictionary that is empty in both cases cannot.
    """
    import json

    try:
        raw = pl.read_parquet_metadata(path).get(FACTS_KEY)
    except Exception as exc:
        raise FactsUnavailable(
            f"{path.name} could not be read for build facts: {exc}"
        ) from exc
    if raw is None:
        raise FactsUnavailable(
            f"{path.name} carries no build facts. It was written before this metadata "
            f"existed, or by something other than the metrics build."
        )
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise FactsUnavailable(
            f"the build facts in {path.name} could not be read as JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise FactsUnavailable(
            f"the build facts in {path.name} are a {type(payload).__name__}, expected an "
            f"object"
        )

    facts: dict[str, int] = {}
    problems: list[str] = []
    for key in REQUIRED_METRICS_FACTS:
        if key not in payload:
            problems.append(f"{key} is missing")
            continue
        value = payload[key]
        # `True` is an `int` in Python, and a flag recorded where a count belongs is a
        # different defect from a count that is merely wrong.
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{key} is {type(value).__name__}, expected an integer")
        elif value < 0:
            problems.append(f"{key} is {value}, which cannot be a count")
        else:
            facts[key] = value

    jitter = facts.get("max_grid_jitter_ms")
    if jitter is not None and jitter > METRICS_MAX_JITTER_MS:
        problems.append(
            f"max_grid_jitter_ms is {jitter}, beyond the {METRICS_MAX_JITTER_MS} ms "
            f"tolerance this build enforces"
        )
    if problems:
        raise FactsUnavailable(
            f"the build facts in {path.name} are unusable: {'; '.join(problems)}"
        )
    return facts


def stored_facts(path: Path) -> dict[str, object]:
    """The build facts recorded with an L1 file: known keys, right types, nothing else."""
    import json

    # `pl.read_parquet_metadata` rather than pyarrow: pyarrow ships no py.typed marker, and
    # ADR-014's rule is that an untyped boundary stays visible rather than being silenced
    # with a per-module override. Polars is typed and returns the same footer map.
    try:
        raw = pl.read_parquet_metadata(path).get(FACTS_KEY)
    except Exception:
        return {}
    if raw is None:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: value for key, value in payload.items()
        if key in FACT_FIELDS
        and isinstance(value, FACT_FIELDS[key])
        and not isinstance(value, bool)
    }


def _validate_and_write(
    combined: pl.DataFrame,
    validator: Callable[[pl.DataFrame], QualityReport],
    out: Path,
    notes: dict[str, object] | None = None,
    facts: dict[str, object] | None = None,
) -> tuple[Path, QualityReport]:
    """Validate a canonical frame and, only if it passes, put it at `out` atomically.

    The single implementation of that step. Every kind reaches Parquet through here, which
    is what stops two copies of the same operation drifting apart again.
    """
    report = validator(combined)
    if notes:
        report.notes.update(notes)
    if report.violations:
        # Before anything else. Rechunking materialises a full copy and staging puts bytes
        # on disk beside a good L1, both for a frame already known to be invalid.
        raise BuildRefused(
            f"{out.name} was not written: {'; '.join(report.violations)}", report
        )

    # One contiguous chunk before writing, and only after the validator is satisfied.
    # `pl.concat` yields a chunk per archive and polars writes row groups on chunk
    # boundaries, so without this the bytes encode how the request was decomposed rather
    # than what the data is. Deliberately after validation - this materialises a full copy,
    # and a frame about to be refused should not pay for one.
    combined = combined.rechunk()

    out.parent.mkdir(parents=True, exist_ok=True)
    staged = out.with_name(out.name + ".building")
    try:
        combined.write_parquet(
            staged, compression="zstd", statistics=True,
            # Only when a build computed facts, so no other kind's bytes move.
            metadata={FACTS_KEY: __import__("json").dumps(facts, sort_keys=True)}
            if facts else None,
        )
        # Same directory, so this is an atomic rename rather than a copy: either the old
        # L1 is intact or the new one is complete, never a half-written file.
        staged.replace(out)
    finally:
        staged.unlink(missing_ok=True)
    return out, report


def build_generic(
    archives: list[Archive],
    reader: Callable[[Archive], pl.DataFrame],
    validator: Callable[[pl.DataFrame], QualityReport],
    out: Path,
) -> tuple[Path, QualityReport]:
    """Concatenate the requested L0 archives into one validated L1 Parquet file.

    Deliberately performs no deduplication. `unique(subset=["ts_ms"])` here erased exactly
    the defect the validators exist to find, and chose which of two rows claiming one
    instant to keep - a decision nobody made and nobody could see. Where a series genuinely
    needs cross-archive reconciliation, that belongs in a reader that knows the series, not
    in this boundary: see `build_metrics`.
    """
    _require_archives(archives)
    frames = _read_all(archives, reader)
    combined = pl.concat(frames).sort("ts_ms", maintain_order=True)
    return _validate_and_write(combined, validator, out)


def build_metrics(
    archives: list[Archive], out: Path
) -> tuple[Path, QualityReport]:
    """L0 -> L1 for daily metrics, with slot ownership resolved across archives.

    Ownership cannot be settled inside a single-archive read: whether a boundary row should
    be kept depends on whether the OWNING archive supplies one, which is only knowable once
    the neighbours have been read. So the reconciliation happens here, and the
    validate-and-write step below is the same one every other kind uses.
    """
    _require_archives(archives)
    try:
        combined, facts = canonical_metrics(archives)
    except MetricsOwnershipError as exc:
        # The CLI reports refusals; an ownership error escaping as itself surfaced as an
        # empty message and a bare non-zero exit.
        raise BuildRefused(str(exc)) from exc
    return _validate_and_write(combined, validate_metrics, out,
                               notes=facts, facts=facts)
