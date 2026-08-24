"""Turn our L1 Parquet into Nautilus objects.

The only subtle thing here is `ts_event`, and getting it wrong is silent lookahead:

Our archives stamp a bar with its **open** time. Nautilus delivers a bar to the strategy at
its `ts_event`, and the bar carries the whole interval's high, low and close. Stamp a
09:00 hourly bar with 09:00 and the strategy learns the 09:59 close at 09:00 — an hour of
free foresight, compounding across every bar, producing a backtest that looks superb and
is worthless. `ts_event` must be the interval's **close**.
"""
from __future__ import annotations

import random
from pathlib import Path

import polars as pl
from nautilus_trader.model.data import Bar, BarType, FundingRateUpdate
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from supercortex.registry.db import PROJECT_ROOT

CATALOG = PROJECT_ROOT / "data" / "catalog"
MS_TO_NS = 1_000_000

RESAMPLE = {"1h": "1h", "4h": "4h", "1d": "1d"}

#: Binance USD-M funding settles every 8 hours. Verified constant across the whole
#: BTCUSDT history in DATA-000003 (`funding_intervals_seen: [8]`).
FUNDING_INTERVAL_MS = 8 * 3_600_000


def load_bars_frame(symbol: str, market: str, interval: str) -> pl.DataFrame:
    """Load 1-minute L1 and aggregate to `interval`, keeping open time in `ts_ms`."""
    path = CATALOG / "klines" / f"{symbol}.{market}" / "1m.parquet"
    frame = pl.read_parquet(path)
    if interval == "1m":
        return frame

    every = RESAMPLE[interval]
    return (
        frame.sort("ts")
        .group_by_dynamic("ts", every=every, closed="left", label="left")
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.col("volume").sum(),
            pl.len().alias("minutes"),
        )
        .with_columns(pl.col("ts").dt.epoch(time_unit="ms").alias("ts_ms"))
        .sort("ts_ms")
    )


def interval_ms(interval: str) -> int:
    return {"1m": 60_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[interval]


def to_bars(
    frame: pl.DataFrame,
    bar_type: BarType,
    price_precision: int,
    size_precision: int,
    interval: str,
    *,
    shift_bars: int = 0,
    scramble: bool = False,
    seed: int = 42,
) -> list[Bar]:
    """Build Nautilus bars, stamping `ts_event` at the interval CLOSE.

    Probe control. ``shift_bars > 0`` moves the stamp **earlier**, handing the strategy
    each bar before it completed — granting look-ahead. ``shift_bars < 0`` delays it.

    The sign convention is deliberately the same as `load_funding`, and is preserved from
    the sealed EXP-000001 probe artifact so historical runs stay interpretable.
    """
    step = interval_ms(interval)
    if scramble:
        # Permute the OHLC block across rows, keeping timestamps and ordering. Destroys
        # the price series' information while preserving its shape and cadence.
        rng = random.Random(seed)
        order = list(range(frame.height))
        rng.shuffle(order)
        price_cols = ["open", "high", "low", "close", "volume"]
        shuffled = frame.select(price_cols)[order]
        frame = frame.drop(price_cols).hstack(shuffled)

    bars: list[Bar] = []
    for row in frame.iter_rows(named=True):
        close_ms = row["ts_ms"] + step - shift_bars * step
        bars.append(
            Bar(
                bar_type,
                Price(row["open"], price_precision),
                Price(row["high"], price_precision),
                Price(row["low"], price_precision),
                Price(row["close"], price_precision),
                Quantity(row["volume"], size_precision),
                ts_event=close_ms * MS_TO_NS,
                ts_init=close_ms * MS_TO_NS,
            )
        )
    return bars


def load_funding(
    symbol: str,
    market: str,
    instrument_id: InstrumentId,
    *,
    shift_intervals: int = 0,
    scramble: bool = False,
    seed: int = 42,
) -> list[FundingRateUpdate]:
    """Funding events as native Nautilus data.

    `calc_time` is the settlement instant, so it is already the event time — no bar-close
    adjustment applies.

    Probe controls, using the same sign convention as `to_bars`:

    * ``shift_intervals > 0`` — events arrive **earlier** than they settled, granting
      look-ahead into the funding channel.
    * ``shift_intervals < 0`` — events arrive **later**, delaying the information.
    * ``scramble`` — permute the rates while keeping the timestamps. This is the test that
      asks whether the strategy uses the series' *information* at all, as opposed to merely
      its presence. Without it, a shift probe that shows nothing is uninterpretable.
    """
    path = CATALOG / "funding_rate" / f"{symbol}.{market}" / "funding.parquet"
    frame = pl.read_parquet(path).sort("ts_ms")

    rates = [float(r) for r in frame["funding_rate"].to_list()]
    if scramble:
        rng = random.Random(seed)
        rng.shuffle(rates)

    step = FUNDING_INTERVAL_MS
    stamps = [int(t) - shift_intervals * step for t in frame["ts_ms"].to_list()]

    events = [
        FundingRateUpdate(instrument_id, rate, ts_event=ts * MS_TO_NS, ts_init=ts * MS_TO_NS)
        for ts, rate in zip(stamps, rates, strict=True)
    ]
    events.sort(key=lambda e: e.ts_event)
    return events


def common_window(symbol: str, market: str) -> tuple[int, int]:
    """The span over which klines and funding both exist, in epoch ms."""
    bars = pl.read_parquet(CATALOG / "klines" / f"{symbol}.{market}" / "1m.parquet")
    fund = pl.read_parquet(CATALOG / "funding_rate" / f"{symbol}.{market}" / "funding.parquet")
    return (
        max(int(bars["ts_ms"].min()), int(fund["ts_ms"].min())),
        min(int(bars["ts_ms"].max()), int(fund["ts_ms"].max())),
    )


def catalog_path(symbol: str, market: str, kind: str, interval: str = "1m") -> Path:
    folder = {"klines": "klines", "funding": "funding_rate", "metrics": "metrics"}[kind]
    filename = f"{interval}.parquet" if kind == "klines" else f"{kind}.parquet"
    return CATALOG / folder / f"{symbol}.{market}" / filename
