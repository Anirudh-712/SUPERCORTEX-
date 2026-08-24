"""`supercortex data` — L0 ingestion, L1 build, verification, dataset cards."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from supercortex.data import canonical, sources
from supercortex.registry import db

app = typer.Typer(no_args_is_help=True, help="Market data: fetch, build, verify.")
console = Console()

CARDS_DIR = db.PROJECT_ROOT / "05-data"


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
    """L0 → L1. Normalise, dedupe, validate, inventory gaps, write Parquet."""
    symbol = symbol.upper()
    archives = [
        sources.Archive(market, symbol, "klines", p, interval)
        for p in sources.months(_parse_month(start), _parse_month(end))
    ]
    out = canonical.CATALOG_ROOT / "klines" / f"{symbol}.{market}" / f"{interval}.parquet"
    path, report = canonical.build(archives, interval, out)

    table = Table(show_header=False)
    table.add_row("rows", f"{report.rows:,}")
    table.add_row("range", f"{report.start[:19]} → {report.end[:19]}")
    table.add_row("completeness", f"{report.completeness:.4%}")
    table.add_row("gaps", f"{len(report.gaps)} spanning "
                          f"{sum(int(g['missing_bars']) for g in report.gaps):,} bars")
    table.add_row("duplicates", str(report.duplicates))
    table.add_row("size", f"{path.stat().st_size / 1024**2:.1f} MB")
    console.print(table)

    if report.violations:
        for violation in report.violations:
            console.print(f"[red]VIOLATION[/] {violation}")
        raise typer.Exit(1)
    console.print(f"[green]L1 written[/] {path.relative_to(db.PROJECT_ROOT)}")


KINDS = {
    "klines":  ("klines",       "{interval}.parquet",  "OHLCV bars"),
    "funding": ("funding_rate", "funding.parquet",     "perpetual funding rate"),
    "metrics": ("metrics",      "metrics.parquet",     "open interest + long/short ratios"),
}


def _l1_path(symbol: str, market: str, kind: str, interval: str) -> Path:
    folder, filename, _ = KINDS[kind]
    return (canonical.CATALOG_ROOT / folder / f"{symbol}.{market}"
            / filename.format(interval=interval))


def _validate_for(kind: str, frame, interval: str):
    if kind == "klines":
        return canonical.validate(frame, interval)
    if kind == "funding":
        return canonical.validate_funding(frame)
    return canonical.validate_metrics(frame)


@app.command("card")
def card(
    symbol: str = typer.Argument(...),
    kind: str = typer.Option("klines", "--kind", help="klines | funding | metrics"),
    market: str = typer.Option("um", "--market"),
    interval: str = typer.Option("1m", "--interval", help="klines only"),
) -> None:
    """Register a dataset and write its DATA-NNNNNN card.

    A dataset that has not been carded cannot be referenced by a trial.
    """
    import polars as pl

    if kind not in KINDS:
        console.print(f"[red]unknown kind {kind!r}[/] — one of {', '.join(KINDS)}")
        raise typer.Exit(1)

    symbol = symbol.upper()
    path = _l1_path(symbol, market, kind, interval)
    if not path.exists():
        console.print(f"[red]no L1 file at {path.relative_to(db.PROJECT_ROOT)}[/]")
        raise typer.Exit(1)

    frame = pl.read_parquet(path)
    report = _validate_for(kind, frame, interval)
    digest = db.sha256_file(path)
    label = interval if kind == "klines" else kind

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

    missing_total = sum(int(g["missing_bars"]) for g in report.gaps)  # type: ignore[arg-type]
    top_gaps = sorted(  # type: ignore[arg-type]
        report.gaps, key=lambda g: -int(g["missing_bars"])
    )[:10]
    gap_table = "\n".join(
        f"| {g['from'][:16]} | {g['to'][:16]} | {g['missing_bars']} | |" for g in top_gaps
    ) or "| — | — | 0 | none |"
    notes = "\n".join(f"- **{k}**: `{v}`" for k, v in report.notes.items()) or "- none"

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
supercortex data build{'' if kind == 'klines' else '-' + kind} {symbol} --start ... --end ...
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
                          f"{sum(int(g['missing_bars']) for g in report.gaps):,} intervals")
    table.add_row("duplicates", str(report.duplicates))
    for key, value in report.notes.items():
        table.add_row(key, str(value))
    table.add_row("size", f"{path.stat().st_size / 1024**2:.2f} MB")
    return table


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
    path, report = canonical.build_generic(
        archives, canonical.read_funding, canonical.validate_funding, out
    )
    console.print(_report_table(report, path))
    if report.violations:
        for violation in report.violations:
            console.print(f"[red]VIOLATION[/] {violation}")
        raise typer.Exit(1)
    console.print(f"[green]L1 written[/] {path.relative_to(db.PROJECT_ROOT)}")


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
    path, report = canonical.build_generic(
        archives, canonical.read_metrics, canonical.validate_metrics, out
    )
    console.print(_report_table(report, path))
    if report.violations:
        for violation in report.violations:
            console.print(f"[red]VIOLATION[/] {violation}")
        raise typer.Exit(1)
    console.print(f"[green]L1 written[/] {path.relative_to(db.PROJECT_ROOT)}")
