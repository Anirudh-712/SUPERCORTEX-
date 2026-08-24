---
paths:
  - "src/supercortex/data/**"
  - "src/supercortex/falsify/**"
  - "02-experiments/**"
  - "05-data/**"
  - "**/*.sql"
---

# Data rules

## Timezone — the one that will bite you silently

**Every time-bucketing operation must state UTC explicitly.**

This machine's timezone is `Asia/Kolkata` (**UTC+5:30**), and DuckDB evaluates naive
date functions in the *session* timezone. A real example from this project:

```sql
-- WRONG: silently buckets in IST. 2020 came back 330 bars short.
SELECT year(ts) AS yr, count(*) FROM read_parquet('...') GROUP BY 1;

-- RIGHT
SELECT year(ts AT TIME ZONE 'UTC') AS yr, count(*) FROM read_parquet('...') GROUP BY 1;
```

330 minutes is exactly 5.5 hours — the IST offset. The data was intact; the query was not.

This is worse than the usual timezone trap because **+5:30 is not a whole number of hours**,
so a shifted daily bar does not even align to an hourly boundary. A strategy specified as
"rebalance daily at 00:00 UTC" that is bucketed naively rebalances at 05:30 IST and its
backtest is quietly measuring a different strategy.

Rules:
- Store timestamps as **UTC-aware**. `canonical.read_klines` does this; keep it that way.
- In DuckDB: always `AT TIME ZONE 'UTC'`, or `SET TimeZone='UTC'` at session start.
- In Polars: `.dt.replace_time_zone("UTC")` on ingest, `.dt.convert_time_zone` to move.
- Never use `datetime.now()`. Use `db.now()` (UTC, SQLite-comparable format).
- Session boundaries, funding times and expiries are **venue** times. State the venue's
  timezone in `methodology.md` and convert explicitly.

## Layers

`L0 data/raw/` is **immutable** — exactly what the venue served, plus its `.CHECKSUM`.
A `PreToolUse` hook denies writes there. `L1 data/catalog/` is rebuildable from L0 by
`supercortex data build` alone. If that ever stops reproducing L1, it is a defect in the
pipeline, not a quirk of the data.

## Binance archive quirks (verified 2026-08-24, not remembered)

| Archive | Header | Timestamp units |
|---|---|---|
| `futures/um` klines 2024→2026 | yes | milliseconds |
| `spot` klines 2024-06 | no | milliseconds |
| `spot` klines 2026-06 | no | **microseconds** |
| `futures/um` fundingRate | yes | milliseconds |

Normalise by **magnitude**, never by filename or date — `canonical._to_millis`.
Column 0 of an `aggTrades` CSV is the aggregate trade **id**, not a timestamp.

## "Complete" does not mean "liquid"

Binance emits a kline for every minute even when nothing traded, so **gap count is a weak
quality signal for klines**. BTCUSDT perp 2020–2026 has 0 gaps and still contains 369
zero-volume bars. For bar data the real checks are stale bars (`volume = 0`,
`open=high=low=close`), not missing timestamps. Report both.

## Before a dataset backs a promoted strategy

Every gap in the card's inventory needs a one-line explanation. A gap is not automatically
a defect — venues halt, symbols relist — but an *unexplained* gap is, and you cannot
explain what you never listed.
