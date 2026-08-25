"""Slice A — Binance Vision mark-price klines as a first-class, checksummed dataset.

This slice stores **what Binance publishes** and nothing more. It deliberately does not
claim that any OHLC field is the mark price used to settle a funding payment: the venue's
own kline documentation defines the fields but never says whether `open` is the mark at the
interval's open time or the first sample inside it, and that question is open. Nothing here
aligns a mark to a settlement, forward-fills, interpolates, or substitutes a traded price.

The archive layout is byte-identical to a traded kline's twelve fields, which is exactly
why the parser is separate. Reusing `read_klines` would name field 6 `volume` and field 9
`trades` on a series that has neither — the venue documents both as Ignore — and a column
called `volume` on a mark-price series is a number someone will eventually use.
"""
from __future__ import annotations

import ast
import inspect
import zipfile
from pathlib import Path

import polars as pl
import pytest

from supercortex.data import canonical, sources

MINUTE = 60_000
BASE = 1_646_092_800_000          # 2022-03-01 00:00:00 UTC, an 8h boundary


def write_archive(archive: sources.Archive, rows: list[str]) -> Path:
    """One L0 zip holding a single headerless CSV, the shape Vision publishes."""
    archive.local.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive.local, "w") as zf:
        zf.writestr(f"{archive.stem}.csv", "\n".join(rows) + "\n")
    return archive.local


def raw_row(ts: int, o: float, h: float, low: float, c: float) -> str:
    """A real archive line: prices, then the six fields Binance documents as Ignore."""
    return f"{ts},{o:.8f},{h:.8f},{low:.8f},{c:.8f},0,{ts + MINUTE - 1},0,60,0,0,0"


def clean_rows(n: int = 5) -> list[str]:
    return [raw_row(BASE + i * MINUTE, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i)
            for i in range(n)]


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """An `Archive` whose `.local` points inside the test's own directory."""
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")

    def make(rows: list[str], *, month: int = 3) -> sources.Archive:
        """`month` picks a distinct archive. Renaming one file made two `Archive`s for the
        same month, so the second silently overwrote the first and a cross-boundary test
        exercised a single archive."""
        from datetime import date
        a = sources.Archive("um", "BTCUSDT", "markPriceKlines", date(2022, month, 1), "1m")
        write_archive(a, rows)
        return a

    return make


# ── 1. the source: URL shape, cadence, first availability ──────────────────────
def test_the_source_url_is_the_published_mark_price_path():
    from datetime import date
    a = sources.Archive("um", "BTCUSDT", "markPriceKlines", date(2022, 3, 1), "1m")
    assert a.url == (
        "https://data.binance.vision/data/futures/um/monthly/markPriceKlines/"
        "BTCUSDT/1m/BTCUSDT-1m-2022-03.zip"
    )


def test_mark_price_archives_are_monthly():
    from datetime import date
    a = sources.Archive("um", "BTCUSDT", "markPriceKlines", date(2022, 3, 1), "1m")
    assert sources.CADENCE["markPriceKlines"] == "monthly"
    assert a.cadence == "monthly"
    assert "/monthly/" in a.url
    assert "/daily/" not in a.url


def test_the_first_available_month_is_recorded():
    """Measured against the venue's own listing, not assumed."""
    assert sources.FIRST_AVAILABLE["markPriceKlines"] == "2020-01-01"


def test_the_local_path_keeps_mark_price_apart_from_traded_klines(tmp_path, monkeypatch):
    from datetime import date
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path)
    mark = sources.Archive("um", "BTCUSDT", "markPriceKlines", date(2022, 3, 1), "1m")
    trade = sources.Archive("um", "BTCUSDT", "klines", date(2022, 3, 1), "1m")
    assert mark.local != trade.local
    assert "markPriceKlines" in mark.local.as_posix()


# ── 2. reading the archive ─────────────────────────────────────────────────────
def test_a_headerless_twelve_field_archive_is_parsed(archive):
    frame = canonical.read_mark_price(archive(clean_rows()))
    assert frame.height == 5


def test_the_canonical_frame_has_exactly_the_documented_columns(archive):
    """Six columns, in this order. No volume, no trade count, no ignored field: the venue
    documents those as Ignore, and a column named `volume` on a mark series is a number
    somebody will eventually believe."""
    frame = canonical.read_mark_price(archive(clean_rows()))
    assert frame.columns == ["ts_ms", "open", "high", "low", "close", "ts"]
    assert frame.schema["ts_ms"] == pl.Int64
    for name in ("open", "high", "low", "close"):
        assert frame.schema[name] == pl.Float64
    assert frame.schema["ts"] == pl.Datetime(time_unit="us", time_zone="UTC")


def test_timestamps_are_normalised_to_milliseconds(archive):
    """Binance has shipped microsecond epochs on some products. Detect by magnitude."""
    micro = [raw_row((BASE + i * MINUTE) * 1000, 100.0, 101.0, 99.0, 100.5)
             for i in range(3)]
    frame = canonical.read_mark_price(archive(micro))
    assert frame["ts_ms"].to_list() == [BASE, BASE + MINUTE, BASE + 2 * MINUTE]
    assert frame.schema["ts_ms"] == pl.Int64


def test_rows_are_sorted_by_timestamp(archive):
    frame = canonical.read_mark_price(archive(list(reversed(clean_rows()))))
    assert frame["ts_ms"].to_list() == sorted(frame["ts_ms"].to_list())


def test_a_wrong_field_count_is_refused(archive):
    """An archive whose shape changed is a structural surprise, not something to coerce."""
    with pytest.raises(ValueError, match=r"12 columns|expected"):
        canonical.read_mark_price(archive([f"{BASE},1,2,3,4,5"]))


def test_an_archive_with_no_csv_is_refused(tmp_path, monkeypatch):
    from datetime import date
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")
    a = sources.Archive("um", "BTCUSDT", "markPriceKlines", date(2022, 3, 1), "1m")
    a.local.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(a.local, "w") as zf:
        zf.writestr("readme.txt", "not data")
    with pytest.raises(ValueError, match="csv"):
        canonical.read_mark_price(a)


def test_a_headed_archive_is_also_accepted(archive):
    """Vision has shipped both headed and headerless eras for other kinds."""
    header = ",".join(canonical.MARK_PRICE_RAW_COLUMNS)
    frame = canonical.read_mark_price(archive([header, *clean_rows()]))
    assert frame.height == 5
    assert frame["ts_ms"][0] == BASE


# ── 3. validation ──────────────────────────────────────────────────────────────
def frame_of(rows: list[tuple[int, float, float, float, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"ts_ms": [r[0] for r in rows], "open": [r[1] for r in rows],
         "high": [r[2] for r in rows], "low": [r[3] for r in rows],
         "close": [r[4] for r in rows]},
        schema={"ts_ms": pl.Int64, "open": pl.Float64, "high": pl.Float64,
                "low": pl.Float64, "close": pl.Float64},
    ).with_columns(
        pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.replace_time_zone("UTC").alias("ts"))


def clean_frame(n: int = 10) -> pl.DataFrame:
    return frame_of([(BASE + i * MINUTE, 100.0, 101.0, 99.0, 100.5) for i in range(n)])


def test_a_clean_minute_grid_is_complete_with_no_violations():
    report = canonical.validate_mark_price(clean_frame())
    assert report.ok
    assert report.rows == 10
    assert report.gaps == []
    assert report.completeness == 1.0


def test_an_empty_dataset_is_a_violation():
    report = canonical.validate_mark_price(clean_frame(0))
    assert not report.ok
    assert "empty" in " ".join(report.violations)


def test_a_duplicate_timestamp_is_a_violation():
    rows = [(BASE, 100.0, 101.0, 99.0, 100.5), (BASE, 100.0, 101.0, 99.0, 100.5)]
    report = canonical.validate_mark_price(frame_of(rows))
    assert not report.ok
    assert report.duplicates == 1
    assert "duplicate" in " ".join(report.violations)


def test_a_non_monotonic_timestamp_is_a_violation():
    rows = [(BASE + MINUTE, 100.0, 101.0, 99.0, 100.5), (BASE, 100.0, 101.0, 99.0, 100.5)]
    report = canonical.validate_mark_price(frame_of(rows))
    assert not report.ok
    assert report.non_monotonic == 1


def test_a_missing_minute_is_inventoried_as_a_gap():
    rows = [(BASE, 100.0, 101.0, 99.0, 100.5),
            (BASE + 3 * MINUTE, 100.0, 101.0, 99.0, 100.5)]
    report = canonical.validate_mark_price(frame_of(rows))
    assert len(report.gaps) == 1
    assert report.gaps[0]["missing_bars"] == 2
    assert report.expected_rows == 4


def test_an_off_grid_timestamp_is_a_violation():
    """The archive is published on an exact one-minute grid. A timestamp that is not on
    that grid means the reader was bypassed or the archive changed shape."""
    rows = [(BASE, 100.0, 101.0, 99.0, 100.5),
            (BASE + MINUTE + 137, 100.0, 101.0, 99.0, 100.5)]
    report = canonical.validate_mark_price(frame_of(rows))
    assert not report.ok
    assert "grid" in " ".join(report.violations).lower()


@pytest.mark.parametrize("price", [0.0, -1.0])
def test_a_non_positive_price_is_a_violation(price):
    rows = [(BASE, price, 101.0, 99.0, 100.5)]
    report = canonical.validate_mark_price(frame_of(rows))
    assert not report.ok
    assert "positive" in " ".join(report.violations)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_price_is_a_violation(bad):
    rows = [(BASE, bad, 101.0, 99.0, 100.5)]
    report = canonical.validate_mark_price(frame_of(rows))
    assert not report.ok
    assert "finite" in " ".join(report.violations)


def test_a_null_in_a_required_field_is_a_violation():
    frame = clean_frame(3).with_columns(
        pl.when(pl.col("ts_ms") == BASE).then(None).otherwise(pl.col("close")).alias("close"))
    report = canonical.validate_mark_price(frame)
    assert not report.ok
    assert "null" in " ".join(report.violations).lower()


@pytest.mark.parametrize("rows,why", [
    ([(BASE, 100.0, 99.0, 99.5, 100.0)], "high below low"),
    ([(BASE, 102.0, 101.0, 99.0, 100.0)], "open above high"),
    ([(BASE, 100.0, 101.0, 99.0, 98.0)], "close below low"),
])
def test_inconsistent_ohlc_is_a_violation(rows, why):
    report = canonical.validate_mark_price(frame_of(rows))
    assert not report.ok, why
    assert "low <= open/close <= high" in " ".join(report.violations)


# ── 4. building L1 ─────────────────────────────────────────────────────────────
def test_the_parquet_output_is_deterministic(archive, tmp_path):
    a = archive(clean_rows())
    from supercortex.registry import db
    first = tmp_path / "one.parquet"
    second = tmp_path / "two.parquet"
    canonical.build_generic([a], canonical.read_mark_price,
                            canonical.validate_mark_price, first)
    canonical.build_generic([a], canonical.read_mark_price,
                            canonical.validate_mark_price, second)
    assert db.sha256_file(first) == db.sha256_file(second)


def test_the_built_file_round_trips_with_the_canonical_columns(archive, tmp_path):
    out = tmp_path / "1m.parquet"
    _path, report = canonical.build_generic(
        [archive(clean_rows())], canonical.read_mark_price,
        canonical.validate_mark_price, out)
    assert report.ok
    assert pl.read_parquet(out).columns == ["ts_ms", "open", "high", "low", "close", "ts"]


# ── 5. the parsers stay apart ──────────────────────────────────────────────────
def test_the_mark_price_reader_does_not_delegate_to_another_parser():
    """Structural, not stylistic. A delegation here would silently re-label the venue's
    Ignore fields as `volume` and `trades` on a series that has neither."""
    tree = ast.parse(inspect.getsource(canonical.read_mark_price))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not called & {"read_klines", "read_funding", "read_metrics"}


def test_the_mark_price_validator_does_not_delegate_to_another_validator():
    tree = ast.parse(inspect.getsource(canonical.validate_mark_price))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not called & {"validate", "validate_funding", "validate_metrics"}


def test_the_raw_column_names_do_not_claim_meaning_the_venue_denies():
    """Binance documents fields 6 and 8-12 as Ignore. Naming them `volume` and `trades`
    would assert semantics the venue explicitly withholds."""
    raw = canonical.MARK_PRICE_RAW_COLUMNS
    assert len(raw) == 12
    assert raw[:5] == ["open_time", "open", "high", "low", "close"]
    assert "volume" not in raw
    assert "trades" not in raw
    assert "quote_volume" not in raw
    assert raw != canonical.KLINE_COLUMNS


def test_a_mark_price_frame_carries_no_traded_volume_columns(archive):
    frame = canonical.read_mark_price(archive(clean_rows()))
    for forbidden in ("volume", "quote_volume", "trades", "taker_buy_base", "ignore"):
        assert forbidden not in frame.columns


# ── 6. registration, card, and authentication through the existing path ────────
@pytest.fixture
def desk(tmp_path, monkeypatch, archive):
    """A registry, a cards directory and a built L1 mark-price file."""
    from supercortex.data import cli as data_cli
    from supercortex.registry import db

    registry = tmp_path / "registry.sqlite"
    db.init(registry)
    monkeypatch.setattr(db, "DB_PATH", registry)
    monkeypatch.setattr(db, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(canonical, "CATALOG_ROOT", tmp_path / "data" / "catalog")
    monkeypatch.setattr(data_cli, "CARDS_DIR", tmp_path / "05-data")
    (tmp_path / "05-data").mkdir()

    out = (tmp_path / "data" / "catalog" / "mark_price" / "BTCUSDT.um" / "1m.parquet")
    canonical.build_generic([archive(clean_rows(10))], canonical.read_mark_price,
                            canonical.validate_mark_price, out)
    return tmp_path


def run_card(**kw):
    from typer.testing import CliRunner

    from supercortex.data import cli as data_cli
    return CliRunner().invoke(data_cli.app, ["card", "BTCUSDT", "--kind", "mark_price",
                                             "--interval", "1m", *kw.get("extra", [])])


def test_mark_price_is_a_registered_kind():
    from supercortex.data import cli as data_cli
    folder, filename, description = data_cli.KINDS["mark_price"]
    assert folder == "mark_price"
    assert filename == "{interval}.parquet"
    assert "mark-price kline" in description


def test_the_l1_path_is_the_specified_one(desk):
    from supercortex.data import cli as data_cli
    path = data_cli._l1_path("BTCUSDT", "um", "mark_price", "1m")
    assert path.as_posix().endswith("data/catalog/mark_price/BTCUSDT.um/1m.parquet")


def test_carding_registers_the_dataset_with_its_interval(desk):
    from supercortex.registry import db
    result = run_card()
    assert result.exit_code == 0, result.output
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM dataset WHERE kind='mark_price'").fetchone()
    assert row is not None
    assert row["interval"] == "1m"
    assert row["symbol"] == "BTCUSDT.um"
    assert row["rows"] == 10
    assert row["parquet_path"].endswith("data/catalog/mark_price/BTCUSDT.um/1m.parquet")
    assert row["sha256"] == db.sha256_file(db.PROJECT_ROOT / row["parquet_path"])


def test_the_card_lists_the_actual_canonical_fields(desk):
    """Read off the built frame, never typed. A schema section that disagrees with the
    file is worse than none."""
    run_card()
    card = next((desk / "05-data").glob("*.md")).read_text(encoding="utf-8")
    for column in ("ts_ms", "open", "high", "low", "close", "ts"):
        assert f"`{column}`" in card
    for absent in ("volume", "quote_volume", "trades", "taker_buy"):
        assert f"`{absent}`" not in card


def test_the_card_calls_it_a_mark_price_kline_and_makes_no_settlement_claim(desk):
    run_card()
    card = next((desk / "05-data").glob("*.md")).read_text(encoding="utf-8")
    said = " ".join(card.split())
    assert "mark-price kline" in said
    assert "interval open field" in said
    assert "unresolved" in said
    for promise in ("forward-fill", "interpolat", "traded-price substitution",
                    "settlement alignment"):
        assert promise in said, f"the card must state that no {promise} occurs"
    # it must not assert the thing A0 could not establish
    assert "settlement mark" in said          # named, as the open question
    assert "is the settlement mark" not in said


def test_data_verify_authenticates_the_mark_price_dataset(desk):
    from typer.testing import CliRunner

    from supercortex.data import cli as data_cli
    run_card()
    result = CliRunner().invoke(data_cli.app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "all datasets verified" in result.output


def test_data_verify_fails_closed_when_the_parquet_changes(desk):
    from typer.testing import CliRunner

    from supercortex.data import cli as data_cli
    run_card()
    target = desk / "data" / "catalog" / "mark_price" / "BTCUSDT.um" / "1m.parquet"
    target.write_bytes(target.read_bytes() + b"\x00")
    result = CliRunner().invoke(data_cli.app, ["verify"])
    assert result.exit_code == 1
    assert "MISMATCH" in result.output


def test_the_validator_chosen_for_the_kind_is_the_mark_price_one(desk):
    """`_validate_for` dispatches by kind. Falling through to the metrics validator would
    silently validate a mark series against a five-minute grid."""
    from supercortex.data import cli as data_cli
    frame = pl.read_parquet(
        desk / "data" / "catalog" / "mark_price" / "BTCUSDT.um" / "1m.parquet")
    report = data_cli._validate_for("mark_price", frame, "1m")
    assert report.ok
    assert report.rows == 10
    assert report.expected_rows == 10


# ── 7. the commands the card points at must exist ─────────────────────────────
def registered_commands():
    from supercortex.data import cli as data_cli
    return {c.name for c in data_cli.app.registered_commands}


def test_fetch_and_build_commands_exist_for_mark_price():
    assert {"fetch-mark-price", "build-mark-price"} <= registered_commands()


def test_the_card_rebuild_line_names_a_real_command(desk):
    run_card()
    # "uv run supercortex data <command> ..."
    command = rebuild_line(desk).split()[4]
    assert command in registered_commands(), f"the card tells the operator to run {command!r}"


def test_the_build_command_uses_the_venue_archive_kind(monkeypatch, tmp_path):
    """`markPriceKlines` is the venue's name; `mark_price` is the desk's. The fetcher must
    ask for the former or every URL 404s."""
    from typer.testing import CliRunner

    from supercortex.data import cli as data_cli
    assert data_cli.SOURCE_KIND["mark_price"] == "markPriceKlines"

    asked: list[sources.Archive] = []
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path)
    monkeypatch.setattr(sources, "fetch", lambda a, **kw: asked.append(a) or a.local)
    CliRunner().invoke(data_cli.app, ["fetch-mark-price", "BTCUSDT",
                                      "--start", "2022-03", "--end", "2022-03"])
    assert [a.kind for a in asked] == ["markPriceKlines"]
    assert asked[0].interval == "1m"
    assert "markPriceKlines/BTCUSDT/1m" in asked[0].url


# ══════════════════════════════════════════════════════════════════════════════
# 8. the build path must not launder what the validator would catch
# ══════════════════════════════════════════════════════════════════════════════
def build_to(out: Path, archives):
    return canonical.build_generic(archives, canonical.read_mark_price,
                                   canonical.validate_mark_price, out)


def test_duplicates_inside_one_archive_survive_to_the_report(archive, tmp_path):
    """`unique(subset=["ts_ms"])` before validation erased exactly the defect the
    validator exists to find: the direct validator test passed while the real build path
    laundered it."""
    rows = [*clean_rows(3), raw_row(BASE, 100.0, 101.0, 99.0, 100.5)]
    with pytest.raises(canonical.BuildRefused) as caught:
        build_to(tmp_path / "1m.parquet", [archive(rows)])
    report = caught.value.report
    assert report is not None
    assert report.duplicates == 1
    assert "duplicate" in " ".join(report.violations)


def test_duplicates_across_two_archives_survive_to_the_report(archive, tmp_path):
    """The overlap a monthly boundary can produce. Deduplication hid this too."""
    first = archive(clean_rows(3), month=3)
    second = archive([raw_row(BASE + 2 * MINUTE, 102.0, 103.0, 101.0, 102.5),
                      raw_row(BASE + 3 * MINUTE, 103.0, 104.0, 102.0, 103.5)], month=4)
    assert first.local != second.local
    with pytest.raises(canonical.BuildRefused) as caught:
        build_to(tmp_path / "1m.parquet", [first, second])
    assert caught.value.report.duplicates == 1


def test_no_row_is_discarded_by_the_build(archive, tmp_path):
    """Every row read is a row validated. Choosing one of two rows that claim the same
    instant is a decision nobody made and nobody can see."""
    rows = [*clean_rows(3), raw_row(BASE, 999.0, 999.0, 999.0, 999.0)]
    with pytest.raises(canonical.BuildRefused) as caught:
        build_to(tmp_path / "1m.parquet", [archive(rows)])
    assert caught.value.report.rows == 4


# ── missing or unreadable archives ─────────────────────────────────────────────
def months(*mm):
    from datetime import date
    return [date(2022, m, 1) for m in mm]


@pytest.fixture
def three_months(tmp_path, monkeypatch):
    """Three requested archives; `present` says which actually exist on disk."""
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")

    def make(present: set[int]) -> list[sources.Archive]:
        out = []
        for offset, month in enumerate(months(3, 4, 5)):
            a = sources.Archive("um", "BTCUSDT", "markPriceKlines", month, "1m")
            if month.month in present:
                rows = [raw_row(BASE + (offset * 10 + i) * MINUTE,
                                100.0, 101.0, 99.0, 100.5) for i in range(3)]
                a.local.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(a.local, "w") as zf:
                    zf.writestr(f"{a.stem}.csv", "\n".join(rows) + "\n")
            out.append(a)
        return out

    return make


@pytest.mark.parametrize("present,absent", [
    ({4, 5}, "2022-03"), ({3, 5}, "2022-04"), ({3, 4}, "2022-05"),
], ids=["first month missing", "middle month missing", "final month missing"])
def test_a_missing_requested_archive_refuses_the_build(three_months, tmp_path,
                                                       present, absent):
    """Completeness is measured between the observed min and max, so a truncated window
    reports as complete. The requested span is what was asked for, never what happened to
    be on disk."""
    with pytest.raises(canonical.BuildRefused) as caught:
        build_to(tmp_path / "1m.parquet", three_months(present))
    assert absent in str(caught.value)


def test_every_missing_archive_is_named_not_just_the_first(three_months, tmp_path):
    with pytest.raises(canonical.BuildRefused) as caught:
        build_to(tmp_path / "1m.parquet", three_months({4}))
    message = str(caught.value)
    assert "2022-03" in message and "2022-05" in message


def test_no_requested_archives_refuses(tmp_path):
    with pytest.raises(canonical.BuildRefused, match="no archives"):
        build_to(tmp_path / "1m.parquet", [])


def test_an_unreadable_archive_refuses_and_names_it(archive, tmp_path, monkeypatch):
    a = archive(clean_rows())
    a.local.write_bytes(b"not a zip at all")
    with pytest.raises(canonical.BuildRefused) as caught:
        build_to(tmp_path / "1m.parquet", [a])
    assert "2022-03" in str(caught.value)


# ── an invalid build must never replace or create a canonical L1 ───────────────
def test_an_invalid_build_leaves_an_existing_l1_byte_identical(archive, tmp_path):
    from supercortex.registry import db
    out = tmp_path / "1m.parquet"
    build_to(out, [archive(clean_rows(4))])
    before = out.read_bytes()
    digest = db.sha256_file(out)

    bad = archive([*clean_rows(3), raw_row(BASE, 1.0, 1.0, 1.0, 1.0)], month=4)
    with pytest.raises(canonical.BuildRefused):
        build_to(out, [bad])
    assert out.read_bytes() == before
    assert db.sha256_file(out) == digest


def test_an_invalid_build_creates_no_canonical_l1(archive, tmp_path):
    out = tmp_path / "1m.parquet"
    with pytest.raises(canonical.BuildRefused):
        build_to(out, [archive([*clean_rows(3), raw_row(BASE, 1.0, 1.0, 1.0, 1.0)])])
    assert not out.exists()


def test_a_refused_build_leaves_no_temporary_files(archive, tmp_path):
    out = tmp_path / "sub" / "1m.parquet"
    with pytest.raises(canonical.BuildRefused):
        build_to(out, [archive([*clean_rows(3), raw_row(BASE, 1.0, 1.0, 1.0, 1.0)])])
    assert list(out.parent.glob("*")) == []


# ══════════════════════════════════════════════════════════════════════════════
# 9. only the interval this validator actually models
# ══════════════════════════════════════════════════════════════════════════════
def invoke(*args):
    from typer.testing import CliRunner

    from supercortex.data import cli as data_cli
    return CliRunner().invoke(data_cli.app, list(args))


def test_the_supported_interval_is_stated_once():
    from supercortex.data import cli as data_cli
    assert data_cli.MARK_PRICE_INTERVAL == "1m"


@pytest.mark.parametrize("command", ["fetch-mark-price", "build-mark-price"])
def test_an_unmodelled_interval_is_refused_before_anything_happens(command, tmp_path,
                                                                   monkeypatch):
    """`validate_mark_price` assumes a sixty-second step unconditionally. Handing it a
    five-minute archive would report a clean series as one-minute data riddled with gaps."""
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")
    touched: list[object] = []
    monkeypatch.setattr(sources, "fetch", lambda a, **kw: touched.append(a))
    monkeypatch.setattr(canonical, "build_generic",
                        lambda *a, **k: touched.append("built"))
    result = invoke(command, "BTCUSDT", "--start", "2022-03", "--end", "2022-03",
                    "--interval", "5m")
    assert result.exit_code != 0
    assert "5m" in result.output and "1m" in result.output
    assert touched == [], "work began before the interval was checked"


def test_card_refuses_an_unmodelled_interval_before_touching_the_registry(desk):
    """A five-minute L1 is placed where `card` would find it, so a refusal cannot be the
    file merely being absent — without that, this passed with no interval gate at all."""
    from supercortex.registry import db
    five = desk / "data" / "catalog" / "mark_price" / "BTCUSDT.um" / "5m.parquet"
    pl.read_parquet(five.with_name("1m.parquet")).write_parquet(five)
    assert five.exists()

    result = invoke("card", "BTCUSDT", "--kind", "mark_price", "--interval", "5m")
    assert result.exit_code != 0
    said = " ".join(result.output.split())
    assert "5m" in said and "1m" in said
    assert "not supported" in said
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dataset").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM id_counter WHERE prefix='DATA'").fetchone()[0] == 0


def test_the_supported_interval_still_works(desk):
    assert invoke("card", "BTCUSDT", "--kind", "mark_price",
                  "--interval", "1m").exit_code == 0


# ══════════════════════════════════════════════════════════════════════════════
# 10. an invalid dataset must never reach the registry
# ══════════════════════════════════════════════════════════════════════════════
def test_card_refuses_a_violating_l1_before_allocating_anything(desk):
    """A card is a claim that a dataset is fit to back a trial. Registering one whose
    validator objected makes the claim without the evidence."""
    from supercortex.registry import db
    target = desk / "data" / "catalog" / "mark_price" / "BTCUSDT.um" / "1m.parquet"
    frame = pl.read_parquet(target)
    pl.concat([frame, frame.head(1)]).write_parquet(target)   # a duplicate timestamp

    result = invoke("card", "BTCUSDT", "--kind", "mark_price", "--interval", "1m")
    assert result.exit_code != 0
    assert "duplicate" in result.output
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dataset").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM id_counter WHERE prefix='DATA'").fetchone()[0] == 0
    assert list((desk / "05-data").glob("*.md")) == []


# ══════════════════════════════════════════════════════════════════════════════
# 11. the rebuild command on the card must actually run
# ══════════════════════════════════════════════════════════════════════════════
def rebuild_line(root: Path) -> str:
    card = next((root / "05-data").glob("*.md")).read_text(encoding="utf-8")
    return next(ln for ln in card.splitlines() if ln.startswith("uv run supercortex"))


def test_the_rebuild_command_is_complete(desk):
    run_card()
    line = rebuild_line(desk)
    assert "..." not in line and "…" not in line
    assert line.startswith("uv run supercortex data build-mark-price BTCUSDT")
    assert "--market um" in line
    assert "--interval 1m" in line
    assert "--start 2022-03" in line and "--end 2022-03" in line


def test_the_rebuild_command_reproduces_the_same_dataset(desk):
    """The card claims L1 is rebuildable by that command alone. Run it and check."""
    from supercortex.registry import db
    run_card()
    target = desk / "data" / "catalog" / "mark_price" / "BTCUSDT.um" / "1m.parquet"
    original = db.sha256_file(target)
    frame_before = pl.read_parquet(target)

    # drop "uv run supercortex data": the runner is handed the data sub-app itself
    args = rebuild_line(desk).split()[4:]
    target.unlink()
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    assert target.exists()
    assert pl.read_parquet(target).equals(frame_before)
    assert db.sha256_file(target) == original


def test_every_kind_has_a_rebuild_command_that_exists():
    from supercortex.data import cli as data_cli
    for kind in data_cli.KINDS:
        command, grain = data_cli.REBUILD[kind]
        assert command in registered_commands(), kind
        assert grain in ("month", "day")


# ══════════════════════════════════════════════════════════════════════════════
# 12. a refused build reaches the operator as a diagnosis, not a traceback
# ══════════════════════════════════════════════════════════════════════════════
def test_the_build_command_reports_a_refusal_cleanly(tmp_path, monkeypatch):
    """`build_generic` now raises. Every command that calls it must say why and exit,
    never fall out of the CLI with a stack trace."""
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")
    monkeypatch.setattr(canonical, "CATALOG_ROOT", tmp_path / "catalog")
    result = invoke("build-mark-price", "BTCUSDT", "--start", "2022-03",
                    "--end", "2022-03")
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in result.output
    assert "BTCUSDT-1m-2022-03" in result.output


def test_a_violating_build_reports_the_violation_cleanly(archive, tmp_path, monkeypatch):
    monkeypatch.setattr(canonical, "CATALOG_ROOT", tmp_path / "catalog")
    archive([*clean_rows(3), raw_row(BASE, 1.0, 1.0, 1.0, 1.0)])
    result = invoke("build-mark-price", "BTCUSDT", "--start", "2022-03",
                    "--end", "2022-03")
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    assert "duplicate" in result.output
    assert not (tmp_path / "catalog" / "mark_price" / "BTCUSDT.um" / "1m.parquet").exists()


def test_fetch_exits_non_zero_when_a_download_fails(tmp_path, monkeypatch):
    """A partial fetch must never look successful: the next build would refuse, but only
    after the operator believed L0 was complete."""
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")

    def boom(archive, **kw):
        raise RuntimeError("CHECKSUM MISMATCH")

    monkeypatch.setattr(sources, "fetch", boom)
    result = invoke("fetch-mark-price", "BTCUSDT", "--start", "2022-03", "--end", "2022-04")
    assert result.exit_code == 1
    assert "CHECKSUM MISMATCH" in result.output


def test_fetch_exits_non_zero_when_a_month_is_not_published(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")

    def absent(archive, **kw):
        raise sources.NotPublished(archive.url)

    monkeypatch.setattr(sources, "fetch", absent)
    result = invoke("fetch-mark-price", "BTCUSDT", "--start", "2022-03", "--end", "2022-03")
    assert result.exit_code == 1
    assert "2022-03" in result.output


def test_fetch_succeeds_quietly_when_everything_arrives(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")
    monkeypatch.setattr(sources, "fetch", lambda a, **kw: a.local)
    result = invoke("fetch-mark-price", "BTCUSDT", "--start", "2022-03", "--end", "2022-03")
    assert result.exit_code == 0
