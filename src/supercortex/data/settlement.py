"""Funding settlements: the venue-reported mark price at each funding instant.

Why this exists
---------------
DATA-000003 carries the funding *rate* from a checksum-authenticated bulk archive.
DATA-000005 carries minute mark-price bars. Neither gives the mark price the venue used
*at the settlement instant*, and deriving one from a minute bar is inference, not
measurement. Funding cash flow is rate x notional at that mark, so the inference would be
invisible in the arithmetic and wrong in the result.

Trust, stated precisely
-----------------------
Every row here is `VENUE_API_CAPTURE` and stays that way. Nothing in this module upgrades
it.

Two things, and only two, are compared against DATA-000003:

  * `fundingTime`, matched **exactly** - the settlement instants are equal to the
    millisecond (424 of 424 on the operator's capture);
  * the **derived Float64** funding rate, matched against the archive's own **stored
    Float64** value.

That second comparison is deliberately named for what it is. DATA-000003 stores
`funding_rate` as Float64; the venue's original decimal string is not retained there, so
it is not what is being checked. Agreement therefore says the two lossy representations
coincide - it is *not* authentication of the original decimal, and this module must never
be read as claiming that. Hence the column name `funding_rate_f64_archive_matched`.

The comparison is **corroboration**: it would have caught disagreement in those two
representations. It does not authenticate the capture. The venue publishes no digest for
an API response, so nothing available to this desk can.

`mark_price` has no counterpart anywhere. Its corroboration is `NONE`, recorded as a
column so the absence travels with the data rather than living in a docstring. It is
*exactly preserved* from the captured response - byte-identical decimal strings - and its
publisher authenticity is **unproven**.

Numbers
-------
`funding_rate_dec` and `mark_price_dec` hold the venue's own decimal strings, unmodified.
The `_f64` columns are **derived approximations** and are lossy: `0.00007381` has no exact
binary representation. All validation and duplicate comparison uses `Decimal` on the
strings; the floats exist for convenience and must never be the basis of an equality
claim.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path

import polars as pl

from supercortex.data import capture
from supercortex.data.canonical import QualityReport, write_validated_parquet

#: The four fields this dataset cannot do without. `rateType` is deliberately absent:
#: only "Regular" has ever been observed and its meaning is unverified, so it is carried
#: through untouched and never depended on.
REQUIRED_FIELDS = ("symbol", "fundingTime", "fundingRate", "markPrice")

PROVENANCE = capture.PROVENANCE
MARK_PRICE_CORROBORATION = capture.MARK_PRICE_CORROBORATION

#: The label for agreement with DATA-000003 on the settlement instant and on the archive's
#: stored Float64 rate. Named for what it is - corroboration of two representations -
#: because `authenticated` would claim something no check here can deliver, and naming the
#: representation stops the claim drifting up to the original decimal.
ARCHIVE_CORROBORATED = "ARCHIVE_CORROBORATED_TS_AND_STORED_F64_RATE"

PARSED_COLUMNS = (
    "ts_ms", "ts", "symbol",
    "funding_rate_dec", "mark_price_dec",
    "funding_rate_f64", "mark_price_f64",
    "rate_type_raw", "provenance", "mark_price_corroboration", "capture_sha256s",
)

L1_COLUMNS = (*PARSED_COLUMNS, "funding_rate_f64_archive_matched")

#: Funding intervals Binance has actually used for USD-M perps. The interval is not
#: inferred from a nominal grid: it is taken from the observed modal delta and must land
#: on one of these, or the build refuses rather than guessing.
ALLOWED_INTERVALS_MS = (3_600_000, 14_400_000, 28_800_000)

#: How far the modal delta may sit from a named interval. Settlement instants genuinely
#: deviate from a round grid - measured at up to 47 ms across six years of DATA-000003 and
#: up to 16 ms in the operator's capture - so this bounds real venue behaviour, not
#: tolerance borrowed from an unrelated dataset.
INTERVAL_TOLERANCE_MS = 1_000


@dataclass(frozen=True)
class Row:
    """One validated settlement, before it becomes a frame row.

    The `Decimal` values travel beside the original strings so that identity
    comparison never quietly falls back to float.
    """

    ts_ms: int
    symbol: str
    funding_rate_dec: str
    mark_price_dec: str
    rate_value: Decimal
    mark_value: Decimal
    rate_type_raw: str | None
    capture_sha256s: tuple[str, ...]


class SettlementRefused(ValueError):
    """The capture cannot become L1. Raised before anything is written."""


@dataclass(frozen=True)
class Completeness:
    """Whether a range may be called complete, and if not, precisely why not."""

    status: str
    reasons: list[str] = field(default_factory=list)
    requested_start: str | None = None
    requested_end: str | None = None


def _decimal(raw: str, field_name: str, index: int) -> Decimal:
    text = raw.strip()
    if not text:
        raise SettlementRefused(f"record {index}: {field_name} is empty")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise SettlementRefused(
            f"record {index}: {field_name} {raw!r} is not a decimal number"
        ) from exc
    if not value.is_finite():
        raise SettlementRefused(f"record {index}: {field_name} {raw!r} is not finite")
    return value


def _extract_rows(body: bytes, *, symbol: str, capture_sha256: str) -> list[Row]:
    """Validate every record and return rows. No deduplication happens here."""
    try:
        doc = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SettlementRefused(
            f"capture body is not valid JSON (truncated or altered?): {exc}"
        ) from exc

    if not isinstance(doc, list):
        raise SettlementRefused(
            f"capture body top level is {type(doc).__name__}, expected a JSON array"
        )
    if not doc:
        raise SettlementRefused("capture body is an empty array: nothing to import")

    rows: list[Row] = []
    for index, record in enumerate(doc):
        if not isinstance(record, dict):
            raise SettlementRefused(
                f"record {index} is {type(record).__name__}, expected an object"
            )
        for name in REQUIRED_FIELDS:
            if name not in record:
                raise SettlementRefused(f"record {index} is missing required field {name!r}")

        ts = record["fundingTime"]
        if isinstance(ts, bool) or not isinstance(ts, int):
            raise SettlementRefused(
                f"record {index}: fundingTime must be an integer epoch-millisecond, "
                f"got {type(ts).__name__}"
            )

        sym = record["symbol"]
        if not isinstance(sym, str):
            raise SettlementRefused(
                f"record {index}: symbol must be a string, got {type(sym).__name__}"
            )
        if sym != symbol:
            raise SettlementRefused(
                f"record {index}: symbol {sym!r} does not match the requested symbol "
                f"{symbol!r}. A mixed-symbol capture is never split apart here."
            )

        raw_rate, raw_mark = record["fundingRate"], record["markPrice"]
        if not isinstance(raw_rate, str):
            raise SettlementRefused(
                f"record {index}: fundingRate must be the venue's decimal string, got "
                f"{type(raw_rate).__name__} - a float here would already have lost digits"
            )
        if not isinstance(raw_mark, str):
            raise SettlementRefused(
                f"record {index}: markPrice must be the venue's decimal string, got "
                f"{type(raw_mark).__name__} - a float here would already have lost digits"
            )

        rate = _decimal(raw_rate, "fundingRate", index)
        mark = _decimal(raw_mark, "markPrice", index)
        if mark <= 0:
            raise SettlementRefused(
                f"record {index}: markPrice {raw_mark!r} is not a positive price. A "
                f"settlement without a mark is the one row this dataset cannot carry."
            )

        rate_type = record.get("rateType")
        if rate_type is not None and not isinstance(rate_type, str):
            raise SettlementRefused(
                f"record {index}: rateType must be a string when present, got "
                f"{type(rate_type).__name__}"
            )

        rows.append(Row(
            ts_ms=ts, symbol=sym,
            funding_rate_dec=raw_rate, mark_price_dec=raw_mark,
            rate_value=rate, mark_value=mark,
            rate_type_raw=rate_type, capture_sha256s=(capture_sha256,),
        ))
    return rows


def _reconcile(rows: Sequence[Row]) -> tuple[list[Row], int]:
    """Collapse identical settlements; refuse contradictory ones. Returns (rows, collapsed).

    Identity is compared as `Decimal`, never as float: `70285.70086232` and
    `70285.700862320000000001` are the same float and different measurements, and it is
    the second reading that tells you a capture disagrees with itself.
    """
    kept: dict[int, Row] = {}
    collapsed = 0
    for row in rows:
        ts = row.ts_ms
        previous = kept.get(ts)
        if previous is None:
            kept[ts] = row
            continue
        same = (
            previous.symbol == row.symbol
            and previous.rate_value == row.rate_value
            and previous.mark_value == row.mark_value
            # Uninterpreted, but not ignorable: two readings that differ here differ,
            # and keeping the first would silently discard what the venue reported.
            and previous.rate_type_raw == row.rate_type_raw
        )
        if not same:
            when = datetime.fromtimestamp(ts / 1000, UTC).isoformat()
            raise SettlementRefused(
                f"conflicting duplicate settlement at {when} (ts_ms={ts}): "
                f"rate {previous.funding_rate_dec!r} vs {row.funding_rate_dec!r}, "
                f"mark {previous.mark_price_dec!r} vs {row.mark_price_dec!r}, "
                f"rateType {previous.rate_type_raw!r} vs {row.rate_type_raw!r} "
                f"(captures {'/'.join(previous.capture_sha256s)[:16]}… and "
                f"{'/'.join(row.capture_sha256s)[:16]}…). Nothing here picks a winner."
            )
        # Identical reading from another page: keep the row, gain the lineage. Sorted
        # and de-duplicated so the column is a function of the input set, not of the
        # order the pages happened to be listed in.
        merged = tuple(sorted(set(previous.capture_sha256s) | set(row.capture_sha256s)))
        kept[ts] = replace(previous, capture_sha256s=merged)
        collapsed += 1
    return sorted(kept.values(), key=lambda r: r.ts_ms), collapsed


def _to_frame(rows: Sequence[Row]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_ms": [r.ts_ms for r in rows],
            "symbol": [r.symbol for r in rows],
            "funding_rate_dec": [r.funding_rate_dec for r in rows],
            "mark_price_dec": [r.mark_price_dec for r in rows],
            # Derived approximations. Lossy by construction; never an equality basis.
            "funding_rate_f64": [float(r.rate_value) for r in rows],
            "mark_price_f64": [float(r.mark_value) for r in rows],
            "rate_type_raw": [r.rate_type_raw for r in rows],
            "provenance": [PROVENANCE] * len(rows),
            "mark_price_corroboration": [MARK_PRICE_CORROBORATION] * len(rows),
            "capture_sha256s": [list(r.capture_sha256s) for r in rows],
        },
        schema={
            "ts_ms": pl.Int64, "symbol": pl.Utf8,
            "funding_rate_dec": pl.Utf8, "mark_price_dec": pl.Utf8,
            "funding_rate_f64": pl.Float64, "mark_price_f64": pl.Float64,
            "rate_type_raw": pl.Utf8, "provenance": pl.Utf8,
            "mark_price_corroboration": pl.Utf8,
            "capture_sha256s": pl.List(pl.Utf8),
        },
    ).with_columns(
        pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.replace_time_zone("UTC").alias("ts")
    ).select(PARSED_COLUMNS)


def parse_body(body: bytes, *, symbol: str, capture_sha256: str) -> pl.DataFrame:
    """One capture body to a validated, sorted, deduplicated frame."""
    rows, _ = _reconcile(_extract_rows(body, symbol=symbol, capture_sha256=capture_sha256))
    return _to_frame(rows)


def corroborate(frame: pl.DataFrame, archive: pl.DataFrame) -> pl.DataFrame:
    """Compare the settlement instant and the derived Float64 rate against DATA-000003.

    Exact equality, deliberately: the measurement that justified this dataset found 424 of
    424 overlapping settlements agreeing to the millisecond and to the bit. A tolerance
    invented here would be a tolerance for a disagreement nobody has ever observed.

    What is compared is `funding_rate_f64` against the archive's stored Float64 column.
    The venue's original decimal string has no counterpart in DATA-000003, so it is not
    corroborated by this or by anything else available here.
    """
    joined = frame.join(
        archive.select("ts_ms", pl.col("funding_rate").alias("_archive_rate")),
        on="ts_ms", how="left",
    )
    disagreeing = joined.filter(
        pl.col("_archive_rate").is_not_null()
        & (pl.col("_archive_rate") != pl.col("funding_rate_f64"))
    )
    if disagreeing.height:
        first = disagreeing.row(0, named=True)
        when = datetime.fromtimestamp(first["ts_ms"] / 1000, UTC).isoformat()
        raise SettlementRefused(
            f"{disagreeing.height} settlement(s) disagree with DATA-000003. First at "
            f"{when}: capture decimal {first['funding_rate_dec']} converts to "
            f"{first['funding_rate_f64']!r}, the checksum-authenticated archive stores "
            f"{first['_archive_rate']!r}. Both are Float64 representations; the capture is "
            f"not trusted over the archive, and neither is silently preferred."
        )
    return joined.with_columns(
        pl.col("_archive_rate").is_not_null().alias("funding_rate_f64_archive_matched")
    ).drop("_archive_rate").select(L1_COLUMNS)


def completeness(
    frame: pl.DataFrame,
    archive: pl.DataFrame,
    *,
    start: datetime | None,
    end: datetime | None,
) -> Completeness:
    """Whether the requested range may be called complete.

    The range is **half-open UTC**: `start <= t < end`. Half-open because adjacent ranges
    must tile without double-counting the settlement on the seam, and because "up to and
    including midnight" is the kind of boundary people disagree about silently.

    Complete is a strong claim, available in exactly one situation: an explicit range,
    lying wholly inside DATA-000003's authenticated coverage, containing at least one
    authenticated settlement, whose settlement set and funding rates agree with the
    archive exactly. Everything else is PARTIAL - including a run that merely *looks*
    continuous, because an interval that continues is not evidence that nothing is missing.

    An empty or reversed range is rejected outright rather than answered. It used to
    return COMPLETE with no reasons: both sides of the comparison were empty, so nothing
    disagreed, and vacuous truth was reported as a guarantee.
    """
    if start is None or end is None:
        return Completeness(
            "PARTIAL",
            ["no explicit requested range: completeness needs --start and --end. Bounds "
             "inferred from the data describe what arrived, which is not a claim about "
             "what exists."],
            None, None,
        )

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    if start_ms >= end_ms:
        raise SettlementRefused(
            f"requested range is empty: start {start.isoformat()} is not before end "
            f"{end.isoformat()}. The range is half-open (start <= t < end), so a reversed "
            f"or zero-length window selects nothing and cannot be judged complete."
        )

    reasons: list[str] = []
    if archive.height == 0:
        reasons.append("DATA-000003 is empty: nothing can corroborate this range")
        return Completeness("PARTIAL", reasons, start.isoformat(), end.isoformat())

    arc_ts = archive["ts_ms"]
    arc_low, arc_high = int(arc_ts.min()), int(arc_ts.max())  # type: ignore[arg-type]
    # The last instant the half-open range can include is end_ms - 1.
    if start_ms < arc_low or (end_ms - 1) > arc_high:
        reasons.append(
            f"requested range extends beyond DATA-000003 authenticated coverage "
            f"({datetime.fromtimestamp(arc_low / 1000, UTC).isoformat()} to "
            f"{datetime.fromtimestamp(arc_high / 1000, UTC).isoformat()}). Data outside "
            f"that window is VENUE_API_CAPTURE with nothing to corroborate it."
        )

    in_range = (pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") < end_ms)
    expected = set(archive.filter(in_range)["ts_ms"])
    present = set(frame.filter(in_range)["ts_ms"])

    if not expected:
        reasons.append(
            "the requested range contains zero authenticated settlements from "
            "DATA-000003. An empty window is not evidence of completeness - there is "
            "nothing in it to have been complete about."
        )

    if missing := expected - present:
        reasons.append(
            f"{len(missing)} settlement(s) present in DATA-000003 but absent from the "
            f"capture set over the requested range"
        )
    if extra := present - expected:
        reasons.append(
            f"{len(extra)} settlement(s) in the capture have no DATA-000003 counterpart "
            f"inside the requested range"
        )

    rows_in_range = frame.filter(in_range)
    if "funding_rate_f64_archive_matched" in rows_in_range.columns and rows_in_range.height:
        unmatched = int((~rows_in_range["funding_rate_f64_archive_matched"]).sum())
        if unmatched:
            reasons.append(f"{unmatched} row(s) in range are not corroborated by DATA-000003")

    status = "COMPLETE" if not reasons else "PARTIAL"
    return Completeness(status, reasons, start.isoformat(), end.isoformat())


def _interval_ms(ts: list[int]) -> tuple[int | None, int | None]:
    """The one interval that describes the whole span, or a refusal. Never a guess.

    `_validator` applies a single interval across the entire span, so accepting one is a
    claim that the settlement grid is **stationary**. Two conditions are needed, and the
    first alone is not enough:

      1. every delta close to a supported interval votes for exactly one bucket;
      2. every delta is a whole multiple of that interval.

    Without (1), a mixed 1h/4h sequence lets one bucket win outright and the winning
    interval then under-counts the span — four rows against three expected, 133%
    "completeness". Without (2), a delta matching no bucket at all (say 3h on an 8h grid)
    votes for nothing, one bucket still wins, and the span is again too short.

    Together they guarantee `span == (sum of whole multiples) * interval`, so the expected
    row count can never fall below the observed one. That is why this refuses rather than
    clamping with `max(expected, rows)`: clamping would hide a missing settlement, which
    is the one defect this dataset exists to surface.

    The mode is taken over deltas bucketed to their nearest supported interval, not over
    raw values. Settlement instants genuinely jitter — up to 47 ms across six years of
    DATA-000003 — so a mode over raw deltas is a mode over noise.
    """
    if len(ts) < 2:
        return None, None

    deltas = [b - a for a, b in pairwise(ts)]
    votes: Counter[int] = Counter()
    for delta in deltas:
        nearest = min(ALLOWED_INTERVALS_MS, key=lambda c: abs(delta - c))
        if abs(delta - nearest) <= INTERVAL_TOLERANCE_MS:
            votes[nearest] += 1

    if not votes:
        raise SettlementRefused(
            f"no observed delta is within {INTERVAL_TOLERANCE_MS} ms of a known funding "
            f"interval {ALLOWED_INTERVALS_MS}; observed {sorted(set(deltas))}. The "
            f"interval is measured, not assumed, so this refuses rather than guessing."
        )

    if len(votes) > 1:
        top = max(votes.values())
        tied = sorted(interval for interval, count in votes.items() if count == top)
        tally = ", ".join(f"{i} ms x{votes[i]}" for i in sorted(votes))
        if len(tied) > 1:
            raise SettlementRefused(
                f"the settlement interval is ambiguous: intervals {tied} each account "
                f"for {top} of the observed deltas ({tally}), so there is no modal "
                f"interval. The interval is measured, never chosen, so this refuses "
                f"rather than picking one."
            )
        raise SettlementRefused(
            f"a stationary settlement grid cannot be established: the observed deltas "
            f"vote for more than one supported interval ({tally}). That is either a "
            f"change of funding schedule inside this range, or a gap whose length "
            f"happens to equal another supported interval — and the two cannot be told "
            f"apart here. Applying one interval to the whole span would misreport the "
            f"expected row count, so this refuses."
        )

    interval = next(iter(votes))
    for delta in deltas:
        multiple = round(delta / interval)
        if multiple < 1 or abs(delta - multiple * interval) > INTERVAL_TOLERANCE_MS:
            raise SettlementRefused(
                f"a stationary settlement grid cannot be established: the delta "
                f"{delta} ms is not a whole multiple of the measured interval "
                f"{interval} ms. Every gap on a stationary grid spans a whole number of "
                f"settlements, so this is neither an interval nor a gap."
            )

    deviation = max(abs(d - round(d / interval) * interval) for d in deltas)
    return interval, deviation


def _validator(notes: dict[str, object]) -> object:
    def validate(frame: pl.DataFrame) -> QualityReport:
        violations: list[str] = []
        if frame.height == 0:
            return QualityReport(0, "", "", 0, violations=["empty dataset"])
        ts = frame["ts_ms"].to_list()
        step, deviation = _interval_ms(ts)
        low, high = ts[0], ts[-1]
        # `QualityReport` has no way to say "unknown denominator", and inventing one
        # is worse than refusing: setting expected_rows to the row count made a
        # single-row build report 100% completeness while the interval was unmeasurable.
        # Rather than widen a type four other dataset kinds depend on, this refuses the
        # L1 build. The capture remains perfectly good L0 evidence; it simply cannot yet
        # support a claim about how complete it is.
        # Rounded, not floored. The interval detector tolerates real jitter, so the
        # span is rarely an exact multiple of the step; flooring it reported four
        # expected rows for five real observations whenever the final stamp landed
        # a few milliseconds early, and 125% completeness is not a measurement.
        # Rounding asks the grid question instead: how many slots does this span?
        expected = 0 if step is None else round((high - low) / step) + 1
        if step is None:
            violations.append(
                f"the settlement interval cannot be measured from {frame.height} "
                f"observation(s): at least two are needed for a delta. No L1 is written, "
                f"because a dataset whose expected row count is unknown cannot report "
                f"completeness. The stored capture remains valid L0 evidence — import "
                f"another page covering an adjacent window and rebuild."
            )
        duplicates = frame.height - frame["ts_ms"].n_unique()
        non_monotonic = sum(1 for a, b in pairwise(ts) if b <= a)
        if duplicates:
            violations.append(f"{duplicates} duplicate settlement timestamps")
        if non_monotonic:
            violations.append(f"{non_monotonic} non-monotonic timestamps")
        if frame["mark_price_f64"].min() <= 0:  # type: ignore[operator]
            violations.append("non-positive mark price reached the frame")
        if step is not None and expected < frame.height:
            # Unreachable while `_interval_ms` enforces a stationary grid. Kept because
            # the alternative to refusing here is publishing completeness above 100%.
            violations.append(
                f"expected row count {expected} is below the observed {frame.height}: "
                f"the measured interval does not describe this span, so no completeness "
                f"figure can be honest. Not clamped — clamping would hide missing rows."
            )

        report = QualityReport(
            rows=frame.height,
            start=datetime.fromtimestamp(low / 1000, UTC).isoformat(),
            end=datetime.fromtimestamp(high / 1000, UTC).isoformat(),
            expected_rows=expected, duplicates=duplicates,
            non_monotonic=non_monotonic, violations=violations,
        )
        report.notes.update(notes)
        report.notes["interval_ms"] = "unknown" if step is None else step
        report.notes["max_grid_deviation_ms"] = (
            "unknown" if deviation is None else deviation
        )
        return report

    return validate


def build(
    capture_dirs: Sequence[Path],
    *,
    symbol: str,
    archive: pl.DataFrame,
    out: Path,
    start: datetime | None = None,
    end: datetime | None = None,
    require_complete: bool = False,
) -> tuple[Path, QualityReport]:
    """Combine stored L0 captures into one L1, atomically. Reads nothing but local files."""
    if not capture_dirs:
        raise SettlementRefused("no captures given: import at least one page first")

    rows: list[Row] = []
    for directory in capture_dirs:
        # One snapshot, verified and consumed. Two things are deliberately avoided.
        #
        # `sha256(body.json)` computed here would be derived from the very bytes it is
        # meant to authenticate, so an edited body would arrive with a freshly minted,
        # perfectly matching digest.
        #
        # Verifying and then re-reading `body.json` leaves a window between the two in
        # which the file can change — a forged mark price then reaches L1 stamped with the
        # previously verified capture's hash. `read_verified_stored` returns the exact
        # bytes it verified, and nothing below reopens the file.
        verified = capture.read_verified_stored(directory)
        rows.extend(
            _extract_rows(
                verified.body,
                symbol=symbol,
                capture_sha256=verified.facts.body_sha256,
            )
        )

    reconciled, collapsed = _reconcile(rows)
    frame = corroborate(_to_frame(reconciled), archive)
    verdict = completeness(frame, archive, start=start, end=end)

    if require_complete and verdict.status != "COMPLETE":
        raise SettlementRefused(
            "--require-complete was given but this range is "
            f"{verdict.status}: " + "; ".join(verdict.reasons)
        )

    matched = int(frame["funding_rate_f64_archive_matched"].sum())
    notes: dict[str, object] = {
        "provenance": PROVENANCE,
        "mark_price_corroboration": MARK_PRICE_CORROBORATION,
        "completeness": verdict.status,
        "completeness_reasons": verdict.reasons,
        "requested_start": verdict.requested_start,
        "requested_end": verdict.requested_end,
        "captures": len(capture_dirs),
        "identical_duplicates_collapsed": collapsed,
        "funding_rate_f64_archive_matched_rows": matched,
        "uncorroborated_rows": frame.height - matched,
        "corroboration_label": ARCHIVE_CORROBORATED,
        "corroborated_fields": (
            "fundingTime (exact); funding_rate_f64 vs the archive's stored "
            "Float64. The original venue decimal is NOT corroborated."
        ),
    }
    return write_validated_parquet(frame, _validator(notes), out, notes=notes)  # type: ignore[arg-type]
