"""`supercortex data` — L0 ingestion, L1 build, verification, dataset cards."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import typer
from rich.console import Console
from rich.table import Table

from supercortex.data import canonical, capture, settlement, sources
from supercortex.registry import db

app = typer.Typer(no_args_is_help=True, help="Market data: fetch, build, verify.")
console = Console()

CARDS_DIR = db.PROJECT_ROOT / "05-data"

#: What a kind's fields do and do not mean. Stated on the card because the alternative is
#: that a reader assumes, and the assumption that matters here is expensive.
SEMANTICS = {
    "metrics": """
## How a repeated or late observation is resolved

Binance publishes these observations one file per day. The files repeat timestamps, overlap
at the boundary, and jitter a second or two off the five-minute grid. All three are settled
explicitly when the archives are read together — never by a deduplication further down the
pipeline, which cannot tell a harmless repeat from a real disagreement.

Each raw observation is mapped to its nearest **five-minute slot**; a stamp further than
5,000 ms from any slot **refuses**, because it is not that slot's observation and choosing
one for it would invent a measurement time. The slot's UTC date names the **owning
archive**, and candidates delivered by the owner are preferred.

Where the owner supplies no candidate, one delivered by a neighbouring archive is kept as
an explicit **delivery fallback**: the venue has published a slot's only observation in the
previous day's file, and dropping it would lose a real measurement and open a gap that
nothing caused. The retained row keeps **its own timestamp** — the slot is identity and
ownership metadata, not a replacement for what the venue reported.

Among the preferred candidates, rows collapse into one **only when identical in every
field, null state included**. A null ratio and a present one are different observations.

If candidates for one slot disagree anywhere, the build **refuses** and names the slot, the
delivering archives and the differing fields. This is not resolved for you: there is no
honest way to choose between two conflicting measurements of one instant, and the order
they happened to appear in says nothing about which is right. The venue has to be asked.

The counts under **Notes** — `exact_repeat_rows_removed`, `non_owner_candidates_ignored`,
`delivery_fallback_rows` and `max_grid_jitter_ms` — are computed by the build that produced
this file, not carried over from a previous one.
""",
    "mark_price": """
## What these fields are, and what they are not

This is the venue's published **mark-price kline** series. `open`, `high`, `low` and
`close` are that series' own interval fields; `open` is **the archive's interval open
field** and nothing more.

Binance settles a perpetual funding payment on the mark price at the settlement instant.
Whether this archive's `open` is that exact settlement mark is **unresolved**: the venue's
kline documentation defines the fields but does not say whether the open is the mark
observed at the interval's open time or the first sample taken inside the interval. This
dataset therefore stores what Binance publishes and asserts no relationship to a funding
settlement.

Nothing in this dataset is aligned, adjusted or filled. Specifically there is no
forward-fill, no interpolation, no traded-price substitution, and no settlement alignment
of any kind. A missing minute is inventoried below and stays missing.

The venue documents fields 6 and 8-12 of the raw archive as *Ignore*. They are discarded
rather than renamed: a mark-price series has no traded volume and no trade count, and a
column carrying those names would be a number somebody eventually believes.
""",
}


def _parse_month(value: str) -> date:
    return datetime.strptime(value, "%Y-%m").date().replace(day=1)


@app.command("fetch")
def fetch(
    symbol: str = typer.Argument(..., help="e.g. BTCUSDT"),
    start: str = typer.Option(..., "--start", help="YYYY-MM"),
    end: str = typer.Option(..., "--end", help="YYYY-MM"),
    market: str = typer.Option("um", "--market", help="spot | um | cm"),
    interval: str = typer.Option("1m", "--interval"),
) -> None:
    """Download monthly archives into L0. Checksum-verified. Idempotent."""
    periods = sources.months(_parse_month(start), _parse_month(end))
    got, missing, failed = 0, [], []
    with console.status(f"fetching {symbol} {interval}…") as status:
        for period in periods:
            archive = sources.Archive(market, symbol.upper(), "klines", period, interval)
            status.update(f"{symbol} {period:%Y-%m}  ({got}/{len(periods)})")
            try:
                sources.fetch(archive)
                got += 1
            except sources.NotPublished:
                missing.append(f"{period:%Y-%m}")
            except Exception as exc:
                failed.append(f"{period:%Y-%m}: {exc}")

    free = db.free_gb()
    console.print(f"[green]{got}[/] archives in L0  ({free:.1f} GB free)")
    if missing:
        console.print(f"[dim]not published (symbol likely not listed yet): {', '.join(missing)}[/]")
    for line in failed:
        console.print(f"[red]failed[/] {line}")
    if free < 4:
        console.print("[yellow]disk is getting tight — 2 GB is the hard floor[/]")


@app.command("build")
def build(
    symbol: str = typer.Argument(...),
    start: str = typer.Option(..., "--start"),
    end: str = typer.Option(..., "--end"),
    market: str = typer.Option("um", "--market"),
    interval: str = typer.Option("1m", "--interval"),
) -> None:
    """L0 → L1. Normalise, validate, inventory gaps, write Parquet.

    Nothing is deduplicated: two rows claiming one instant both reach the validator, which
    reports them rather than one being silently dropped.
    """
    symbol = symbol.upper()
    archives = [
        sources.Archive(market, symbol, "klines", p, interval)
        for p in sources.months(_parse_month(start), _parse_month(end))
    ]
    out = canonical.CATALOG_ROOT / "klines" / f"{symbol}.{market}" / f"{interval}.parquet"
    _built(archives, canonical.read_klines,
           lambda frame: canonical.validate(frame, interval), out)


KINDS = {
    "klines":     ("klines",       "{interval}.parquet", "OHLCV bars"),
    "funding":    ("funding_rate", "funding.parquet",    "perpetual funding rate"),
    "mark_price": ("mark_price",   "{interval}.parquet", "mark-price klines"),
    "metrics":    ("metrics",      "metrics.parquet",    "open interest + long/short ratios"),
}

#: The archive kind each registered kind is built from. The registered name is the desk's
#: (`mark_price`); the source name is the venue's (`markPriceKlines`). Keeping both is what
#: lets a card say where its bytes came from without the two ever being confused.
SOURCE_KIND = {
    "klines": "klines", "funding": "fundingRate",
    "mark_price": "markPriceKlines", "metrics": "metrics",
}


#: The only interval this slice models. `validate_mark_price` assumes a sixty-second step
#: unconditionally, so handing it a five-minute archive would report a clean series as
#: one-minute data riddled with gaps. Widening this means widening the validator.
MARK_PRICE_INTERVAL = "1m"


def _built(
    archives: list[sources.Archive],
    reader: Callable[[sources.Archive], pl.DataFrame],
    validator: Callable[[pl.DataFrame], canonical.QualityReport],
    out: Path,
) -> Path:
    """Run a build, or turn its refusal into a diagnosis and a clean exit.

    `build_generic` raises rather than returning a half-built dataset, and a raise that
    escapes the CLI is a stack trace where an operator needed a sentence.
    """
    try:
        path, report = canonical.build_generic(archives, reader, validator, out)
    except canonical.BuildRefused as refusal:
        for violation in (refusal.report.violations if refusal.report else []):
            console.print(f"[red]VIOLATION[/] {violation}")
        console.print(f"[red]build refused[/] — {refusal}")
        raise typer.Exit(1) from refusal
    console.print(_report_table(report, path))
    console.print(f"[green]L1 written[/] {path.relative_to(db.PROJECT_ROOT)}")
    return path


def _built_metrics(archives: list[sources.Archive], out: Path) -> Path:
    """`build_metrics`, with the same refusal reporting as every other build."""
    try:
        path, report = canonical.build_metrics(archives, out)
    except canonical.BuildRefused as refusal:
        for violation in (refusal.report.violations if refusal.report else []):
            console.print(f"[red]VIOLATION[/] {violation}")
        console.print(f"[red]build refused[/] — {refusal}")
        raise typer.Exit(1) from refusal
    console.print(_report_table(report, path))
    console.print(f"[green]L1 written[/] {path.relative_to(db.PROJECT_ROOT)}")
    return path


def _require_mark_price_interval(interval: str) -> None:
    """Refuse an interval the validator does not model, before anything else happens."""
    if interval != MARK_PRICE_INTERVAL:
        console.print(
            f"[red]interval {interval!r} is not supported for mark-price klines[/] — "
            f"this slice models {MARK_PRICE_INTERVAL!r} only, and the validator assumes "
            f"that step unconditionally."
        )
        raise typer.Exit(1)


#: The command that rebuilds each kind, and the granularity its --start/--end take. Both
#: are written onto every card, so the command has to exist and the dates have to parse.
#: `"build-" + kind` silently produced `build-mark_price`, which is not a command.
REBUILD = {
    "klines": ("build", "month"),
    "funding": ("build-funding", "month"),
    "mark_price": ("build-mark-price", "month"),
    "metrics": ("build-metrics", "day"),
}


def _interval_label(kind: str, interval: str) -> str:
    """A dataset's `interval` column: the real interval where the L1 file is per-interval,
    the kind otherwise. Derived from the filename template rather than a second list."""
    return interval if "{interval}" in KINDS[kind][1] else kind


def _l1_path(symbol: str, market: str, kind: str, interval: str) -> Path:
    folder, filename, _ = KINDS[kind]
    return (canonical.CATALOG_ROOT / folder / f"{symbol}.{market}"
            / filename.format(interval=interval))


def _validate_for(
    kind: str, frame: pl.DataFrame, interval: str
) -> canonical.QualityReport:
    if kind == "klines":
        return canonical.validate(frame, interval)
    if kind == "funding":
        return canonical.validate_funding(frame)
    if kind == "mark_price":
        return canonical.validate_mark_price(frame)
    return canonical.validate_metrics(frame)


@app.command("card")
def card(
    symbol: str = typer.Argument(...),
    kind: str = typer.Option("klines", "--kind",
                             help="klines | funding | mark_price | metrics"),
    market: str = typer.Option("um", "--market"),
    interval: str = typer.Option("1m", "--interval", help="klines and mark_price only"),
) -> None:
    """Register a dataset and write its DATA-NNNNNN card.

    A dataset that has not been carded cannot be referenced by a trial.
    """
    import polars as pl

    if kind not in KINDS:
        console.print(f"[red]unknown kind {kind!r}[/] — one of {', '.join(KINDS)}")
        raise typer.Exit(1)

    if kind == "mark_price":
        _require_mark_price_interval(interval)

    symbol = symbol.upper()
    path = _l1_path(symbol, market, kind, interval)
    if not path.exists():
        console.print(f"[red]no L1 file at {path.relative_to(db.PROJECT_ROOT)}[/]")
        raise typer.Exit(1)

    frame = pl.read_parquet(path)
    report = _validate_for(kind, frame, interval)
    # `card` re-reads the file and builds a FRESH report, so anything a build computed
    # about normalisation exists only in that build's memory by now. The facts it recorded
    # with the artifact are merged back in.
    #
    # For metrics the card's own prose states all four appear under Notes, so they are
    # REQUIRED: registering a file whose metadata cannot supply them would publish that
    # claim without the evidence. Every other kind merges leniently - they claim nothing.
    if kind == "metrics":
        try:
            report.notes.update(canonical.require_metrics_facts(path))
        except canonical.FactsUnavailable as missing:
            console.print(f"[red]{path.name} cannot be carded[/] — {missing}")
            console.print("Rebuild L1 with `uv run supercortex data build-metrics` so the "
                          "normalisation facts are recorded with the file.")
            raise typer.Exit(1) from missing
    else:
        report.notes.update(canonical.stored_facts(path))
    if report.violations:
        # Before the ID is allocated, before the row is written, before the card exists. A
        # card is a claim that a dataset may back a trial; registering one the validator
        # objected to makes that claim without the evidence.
        for violation in report.violations:
            console.print(f"[red]VIOLATION[/] {violation}")
        console.print(f"[red]{path.name} is not fit to register[/] — rebuild it from L0.")
        raise typer.Exit(1)

    digest = db.sha256_file(path)
    label = _interval_label(kind, interval)

    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM dataset WHERE venue='binance' AND symbol=? AND kind=? AND interval=?",
            (f"{symbol}.{market}", kind, label),
        ).fetchone()
        data_id = existing["id"] if existing else db.allocate_id(conn, "DATA")

    slug = f"{data_id}-binance-{market}-{symbol.lower()}-{label}"
    card_path = CARDS_DIR / f"{slug}.md"

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO dataset(id,title,venue,symbol,kind,interval,parquet_path,sha256,rows,"
            "start_ts,end_ts,schema_path,gaps_json,verified_at,card_path)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET sha256=excluded.sha256, rows=excluded.rows,"
            " start_ts=excluded.start_ts, end_ts=excluded.end_ts, gaps_json=excluded.gaps_json,"
            " verified_at=excluded.verified_at, parquet_path=excluded.parquet_path,"
            " kind=excluded.kind",
            (data_id, f"Binance {market} {symbol} {KINDS[kind][2]}", "binance",
             f"{symbol}.{market}", kind, label,
             path.relative_to(db.PROJECT_ROOT).as_posix(), digest, report.rows,
             report.start, report.end, "src/supercortex/data/canonical.py",
             json.dumps(report.gaps[:300]), db.now(),
             card_path.relative_to(db.PROJECT_ROOT).as_posix()),
        )

    missing_total = sum(g["missing_bars"] for g in report.gaps)
    top_gaps = sorted(report.gaps, key=lambda g: -g["missing_bars"])[:10]
    gap_table = "\n".join(
        f"| {g['from'][:16]} | {g['to'][:16]} | {g['missing_bars']} | |" for g in top_gaps
    ) or "| — | — | 0 | none |"
    notes = "\n".join(f"- **{k}**: `{v}`" for k, v in report.notes.items()) or "- none"
    # Read off the built frame, never typed. A schema section that disagrees with the file
    # is worse than no schema section.
    schema_table = "\n".join(
        f"| `{name}` | `{dtype}` |" for name, dtype in frame.schema.items())
    semantics = SEMANTICS.get(kind, "")
    # A complete command, from the dataset's own coverage. "--start ..." is not a
    # reproducible instruction, and the sentence under it claimed it was.
    command, grain = REBUILD[kind]
    span = 7 if grain == "month" else 10
    rebuild = " ".join([
        "uv run supercortex data", command, symbol,
        f"--start {report.start[:span]}", f"--end {report.end[:span]}",
        f"--market {market}",
        *([f"--interval {interval}"] if "{interval}" in KINDS[kind][1] else []),
    ])

    card_path.write_text(
        f"""---
id: {data_id}
type: dataset
title: Binance {market} {symbol} {KINDS[kind][2]}
venue: binance
market: {market}
symbol: {symbol}
kind: {kind}
interval: {label}
sha256: {digest}
rows: {report.rows}
start: {report.start}
end: {report.end}
completeness: {report.completeness:.6f}
gap_count: {len(report.gaps)}
verified: {db.now()}
---

<!-- GENERATED by supercortex — do not edit -->

# {data_id} — Binance {market} {symbol} {KINDS[kind][2]}

**Source:** `data.binance.vision`, SHA256-verified against the venue's own published
checksums. Public data only; no credentials involved.
**L1 file:** `{path.relative_to(db.PROJECT_ROOT)}`

## Contents

| | |
|---|---|
| Rows | {report.rows:,} |
| Range (UTC) | {report.start[:19]} → {report.end[:19]} |
| Completeness | {report.completeness:.4%} |
| Gaps | {len(report.gaps)} spanning {missing_total:,} intervals |
| Duplicates | {report.duplicates} |
| Non-monotonic | {report.non_monotonic} |
| SHA256 | `{digest}` |

## Schema

| Column | Type |
|---|---|
{schema_table}
{semantics}
## Notes

{notes}

## Gap inventory (10 largest)

A gap is not automatically a defect — venues halt and feeds drop. But an *unexplained* gap
is, and you cannot explain what you never listed. Each row needs a one-line reason before
this dataset backs a promoted strategy.

| From (UTC) | To (UTC) | Missing | Explanation |
|---|---|---|---|
{gap_table}

## Rebuild

```
{rebuild}
```

L1 is rebuildable from L0 by that command alone. If it stops reproducing this file, treat
it as a defect in the pipeline, not a quirk of the data.
""",
        encoding="utf-8",
    )
    console.print(f"[green]{data_id}[/] carded → {card_path.relative_to(db.PROJECT_ROOT)}")
    console.print(f"  sha256 {digest[:16]}…  {report.rows:,} rows  "
                  f"{report.completeness:.4%} complete  {len(report.gaps)} gaps")


@app.command("verify")
def verify(dataset_id: str | None = typer.Argument(None)) -> None:
    """Re-hash every registered dataset against its recorded sha256."""
    with db.connect() as conn:
        query = "SELECT * FROM dataset" + (" WHERE id = ?" if dataset_id else "")
        rows = conn.execute(query, (dataset_id,) if dataset_id else ()).fetchall()
    if not rows:
        console.print("no datasets registered — `supercortex data card`")
        raise typer.Exit(1)

    table = Table(show_header=True, header_style="dim")
    for col in ("ID", "Dataset", "Rows", "Hash", ""):
        table.add_column(col)
    failed = 0
    for row in rows:
        path = db.PROJECT_ROOT / (row["parquet_path"] or "")
        if not row["parquet_path"] or not path.exists():
            table.add_row(row["id"], row["symbol"], "—", "FILE MISSING", "🔴")
            failed += 1
            continue
        ok = db.sha256_file(path) == row["sha256"]
        failed += not ok
        table.add_row(row["id"], f"{row['symbol']} {row['interval']}",
                      f"{row['rows']:,}", "match" if ok else "MISMATCH", "🟢" if ok else "🔴")
    console.print(table)
    if failed:
        console.print(f"[red]{failed} dataset(s) failed verification[/] — rebuild from L0")
        raise typer.Exit(1)
    console.print("[green]all datasets verified[/]")


@app.command("ls")
def ls() -> None:
    """Registered datasets and what L0 costs on disk."""
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM dataset ORDER BY id").fetchall()
    table = Table(show_header=True, header_style="dim")
    for col in ("ID", "Symbol", "Interval", "Rows", "Start", "End", "Gaps"):
        table.add_column(col)
    for row in rows:
        gaps = len(json.loads(row["gaps_json"] or "[]"))
        table.add_row(row["id"], row["symbol"], row["interval"], f"{row['rows']:,}",
                      (row["start_ts"] or "")[:10], (row["end_ts"] or "")[:10], str(gaps))
    console.print(table if rows else "no datasets yet")

    l0 = sum(p.stat().st_size for p in Path(sources.RAW_ROOT).rglob("*.zip"))
    l1 = sum(p.stat().st_size for p in Path(canonical.CATALOG_ROOT).rglob("*.parquet"))
    console.print(f"\nL0 raw {l0 / 1024**2:,.0f} MB   L1 catalog {l1 / 1024**2:,.0f} MB   "
                  f"{db.free_gb():.1f} GB free")


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


@app.command("fetch-funding")
def fetch_funding(
    symbol: str = typer.Argument(...),
    start: str = typer.Option(..., "--start", help="YYYY-MM"),
    end: str = typer.Option(..., "--end", help="YYYY-MM"),
    market: str = typer.Option("um", "--market"),
) -> None:
    """Download monthly fundingRate archives into L0."""
    archives = [
        sources.Archive(market, symbol.upper(), "fundingRate", p)
        for p in sources.months(_parse_month(start), _parse_month(end))
    ]
    with console.status(f"fetching {symbol} funding…"):
        got, missing, failed = sources.fetch_many(archives, workers=8)
    console.print(f"[green]{got}[/] funding archives in L0  ({db.free_gb():.1f} GB free)")
    if missing:
        console.print(f"[dim]not published: {', '.join(f'{d:%Y-%m}' for d in missing[:12])}[/]")
    for line in failed[:10]:
        console.print(f"[red]failed[/] {line}")


@app.command("fetch-metrics")
def fetch_metrics(
    symbol: str = typer.Argument(...),
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD"),
    market: str = typer.Option("um", "--market"),
    workers: int = typer.Option(8, "--workers", help="Concurrent downloads."),
) -> None:
    """Download daily metrics archives (open interest + long/short ratios) into L0.

    Binance publishes no monthly metrics archive, so this is one file per day.
    """
    archives = [
        sources.Archive(market, symbol.upper(), "metrics", d)
        for d in sources.days(_parse_day(start), _parse_day(end))
    ]
    console.print(f"{len(archives)} daily files to reconcile (already-present ones are skipped)")
    with console.status(f"fetching {symbol} metrics…"):
        got, missing, failed = sources.fetch_many(archives, workers=workers)
    console.print(f"[green]{got}[/] metric archives in L0  ({db.free_gb():.1f} GB free)")
    if missing:
        first, last = min(missing), max(missing)
        console.print(f"[dim]{len(missing)} days not published ({first} … {last}) — "
                      f"open interest starts {sources.FIRST_AVAILABLE['metrics']}[/]")
    for line in failed[:10]:
        console.print(f"[red]failed[/] {line}")


def _report_table(report: canonical.QualityReport, path: Path) -> Table:
    table = Table(show_header=False)
    table.add_row("rows", f"{report.rows:,}")
    table.add_row("range", f"{report.start[:19]} → {report.end[:19]}")
    table.add_row("completeness", f"{report.completeness:.4%}")
    table.add_row("gaps", f"{len(report.gaps)} spanning "
                          f"{sum(g['missing_bars'] for g in report.gaps):,} intervals")
    table.add_row("duplicates", str(report.duplicates))
    for key, value in report.notes.items():
        table.add_row(key, str(value))
    table.add_row("size", f"{path.stat().st_size / 1024**2:.2f} MB")
    return table


@app.command("fetch-mark-price")
def fetch_mark_price(
    symbol: str = typer.Argument(..., help="e.g. BTCUSDT"),
    start: str = typer.Option(..., "--start", help="YYYY-MM"),
    end: str = typer.Option(..., "--end", help="YYYY-MM"),
    market: str = typer.Option("um", "--market"),
    interval: str = typer.Option("1m", "--interval"),
) -> None:
    """Download monthly mark-price kline archives into L0. Checksum-verified."""
    _require_mark_price_interval(interval)
    symbol = symbol.upper()
    periods = sources.months(_parse_month(start), _parse_month(end))
    got, missing, failed = 0, [], []
    with console.status(f"fetching {symbol} mark price {interval}…") as status:
        for period in periods:
            archive = sources.Archive(
                market, symbol, SOURCE_KIND["mark_price"], period, interval)
            status.update(f"{symbol} {period:%Y-%m}  ({got}/{len(periods)})")
            try:
                sources.fetch(archive)
                got += 1
            except sources.NotPublished:
                missing.append(f"{period:%Y-%m}")
            except Exception as exc:
                failed.append(f"{period:%Y-%m}: {exc}")

    console.print(f"[green]{got}[/] mark-price archives in L0  ({db.free_gb():.1f} GB free)")
    for line in failed:
        console.print(f"[red]failed[/] {line}")
    if missing:
        console.print(f"[red]not published[/]: {', '.join(missing)}")
    if missing or failed:
        # A partial fetch must not look successful. The next build refuses anyway, but
        # only after the operator has been told L0 is complete.
        console.print(f"[red]{len(missing) + len(failed)} of {len(periods)} archives did "
                      f"not arrive[/] — L0 is incomplete and a build will refuse it.")
        raise typer.Exit(1)


@app.command("build-mark-price")
def build_mark_price(
    symbol: str = typer.Argument(...),
    start: str = typer.Option(..., "--start"),
    end: str = typer.Option(..., "--end"),
    market: str = typer.Option("um", "--market"),
    interval: str = typer.Option("1m", "--interval"),
) -> None:
    """L0 → L1 for mark-price klines.

    Stores what the venue publishes. No alignment to a funding settlement happens here or
    anywhere else in this slice.
    """
    _require_mark_price_interval(interval)
    symbol = symbol.upper()
    archives = [
        sources.Archive(market, symbol, SOURCE_KIND["mark_price"], p, interval)
        for p in sources.months(_parse_month(start), _parse_month(end))
    ]
    out = _l1_path(symbol, market, "mark_price", interval)
    _built(archives, canonical.read_mark_price, canonical.validate_mark_price, out)


@app.command("build-funding")
def build_funding(
    symbol: str = typer.Argument(...),
    start: str = typer.Option(..., "--start"),
    end: str = typer.Option(..., "--end"),
    market: str = typer.Option("um", "--market"),
) -> None:
    """L0 → L1 for funding rates."""
    symbol = symbol.upper()
    archives = [
        sources.Archive(market, symbol, "fundingRate", p)
        for p in sources.months(_parse_month(start), _parse_month(end))
    ]
    out = canonical.CATALOG_ROOT / "funding_rate" / f"{symbol}.{market}" / "funding.parquet"
    _built(archives, canonical.read_funding, canonical.validate_funding, out)


@app.command("build-metrics")
def build_metrics(
    symbol: str = typer.Argument(...),
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD"),
    market: str = typer.Option("um", "--market"),
) -> None:
    """L0 → L1 for open interest and crowding ratios."""
    symbol = symbol.upper()
    archives = [
        sources.Archive(market, symbol, "metrics", d)
        for d in sources.days(_parse_day(start), _parse_day(end))
    ]
    out = canonical.CATALOG_ROOT / "metrics" / f"{symbol}.{market}" / "metrics.parquet"
    _built_metrics(archives, out)


# ── funding settlements (VENUE_API_CAPTURE) ─────────────────────────────────────
# Two commands, deliberately: importing a page stores one immutable capture and makes no
# claim about coverage; building combines stored captures into L1. Collapsing them would
# make every build depend on whatever happened to be captured last.


def _parse_bound(value: str | None, flag: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise typer.BadParameter(f"{flag} must be YYYY-MM-DD, got {value!r}") from exc


@app.command("import-funding-page")
def import_funding_page(
    capture_dir: str = typer.Option(..., "--capture-dir", help="Local capture bundle"),
    symbol: str = typer.Option(..., "--symbol"),
    market: str = typer.Option("um", "--market"),
) -> None:
    """Store one human-captured funding page in L0. Local paths only; never fetches.

    Takes no URL and no credential: the operator performs the public request and this
    command admits the bytes. See `supercortex.data.capture` for what the digests do and
    do not prove.
    """
    symbol = symbol.upper()
    try:
        bundle = Path(capture_dir)
        stored = capture.store(
            bundle, symbol=symbol, market=market, raw_root=sources.RAW_ROOT
        )
    except capture.CaptureRefused as refusal:
        console.print(f"[red]capture refused[/] — {refusal}")
        raise typer.Exit(1) from refusal

    # Deliberately NOT `admit(bundle)`: once the capture is stored, the stored bytes
    # are the subject. Re-reading the source would report on a file that is no longer
    # what this command is responsible for, and would miss staging corruption.
    facts = capture.verify_stored(stored)
    table = Table(show_header=False, box=None)
    table.add_row("stored", str(stored.relative_to(db.PROJECT_ROOT)))
    table.add_row("body sha256", facts.body_sha256)
    table.add_row("captured at (local clock)", facts.captured_at_local)
    table.add_row("server date (venue clock)", facts.server_date or "—")
    table.add_row("provenance", facts.provenance)
    table.add_row("mark price corroboration", facts.mark_price_corroboration)
    console.print(table)
    console.print(
        "[yellow]note[/] the digest fixes these bytes as received; it does not "
        "authenticate the publisher."
    )


@app.command("build-funding-settlement")
def build_funding_settlement(
    symbol: str = typer.Option(..., "--symbol"),
    market: str = typer.Option("um", "--market"),
    start: str | None = typer.Option(
        None, "--start", help="YYYY-MM-DD (UTC), inclusive."),
    end: str | None = typer.Option(
        None, "--end", help="YYYY-MM-DD (UTC), EXCLUSIVE: the range is start <= t < end."),
    require_complete: bool = typer.Option(
        False, "--require-complete",
        help="Refuse unless the explicit half-open range [--start, --end) is fully "
             "corroborated by DATA-000003.",
    ),
) -> None:
    """Combine stored funding captures into L1. Reads only local files."""
    symbol = symbol.upper()
    root = capture.capture_root(sources.RAW_ROOT, market, symbol)
    captures = sorted(
        p for p in root.glob("*") if p.is_dir() and (p / "body.json").is_file()
    ) if root.exists() else []
    if not captures:
        console.print(
            f"[red]no stored captures[/] for {symbol}.{market} — run "
            f"`supercortex data import-funding-page` first."
        )
        raise typer.Exit(1)

    archive_path = (
        canonical.CATALOG_ROOT / "funding_rate" / f"{symbol}.{market}" / "funding.parquet"
    )
    if not archive_path.exists():
        console.print(
            f"[red]DATA-000003 not built[/] at {archive_path.relative_to(db.PROJECT_ROOT)}; "
            f"corroboration needs it."
        )
        raise typer.Exit(1)

    out = (
        canonical.CATALOG_ROOT / "funding_settlement" / f"{symbol}.{market}"
        / "funding_settlement.parquet"
    )
    try:
        path, report = settlement.build(
            captures, symbol=symbol, archive=pl.read_parquet(archive_path), out=out,
            start=_parse_bound(start, "--start"), end=_parse_bound(end, "--end"),
            require_complete=require_complete,
        )
    except settlement.SettlementRefused as refusal:
        console.print(f"[red]build refused[/] — {refusal}")
        raise typer.Exit(1) from refusal
    except canonical.BuildRefused as refusal:
        for violation in (refusal.report.violations if refusal.report else []):
            console.print(f"[red]VIOLATION[/] {violation}")
        console.print(f"[red]build refused[/] — {refusal}")
        raise typer.Exit(1) from refusal

    console.print(_report_table(report, path))
    console.print(f"[green]L1 written[/] {path.relative_to(db.PROJECT_ROOT)}")
    console.print(
        f"completeness [bold]{report.notes['completeness']}[/] · provenance "
        f"[bold]{report.notes['provenance']}[/] · mark-price corroboration "
        f"[bold]{report.notes['mark_price_corroboration']}[/]"
    )
    reasons = report.notes.get("completeness_reasons")
    if isinstance(reasons, list):
        for reason in reasons:
            console.print(f"  [yellow]PARTIAL[/] {reason}")
