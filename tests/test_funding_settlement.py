"""Parsing, validation, corroboration and completeness for the funding-settlement dataset.

The dataset exists for one field the checksum-authenticated archive does not carry: the
venue-reported mark price at each settlement. That asymmetry drives every rule here.

`fundingTime` and `funding_rate` can be checked against DATA-000003, and on the operator's
capture they agree exactly — 424 of 424 settlements, zero millisecond delta. That is
**corroboration**: it would have caught disagreement in those two fields. It does not
authenticate the capture, and it says nothing at all about `mark_price`, which has no
counterpart to be compared with. Tests below pin that distinction so it cannot quietly
erode into a claim of authenticity.

Nothing here touches the real data lake or registry: every test writes to tmp_path.
"""
from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from supercortex.data import canonical, capture, settlement

REPO = Path(__file__).resolve().parents[1]
GOOD = REPO / "tests" / "fixtures" / "funding_settlement" / "good"
SYMBOL = "BTCUSDT"
HOUR8 = 28_800_000


def records() -> list[dict]:
    return json.loads((GOOD / "body.json").read_bytes())


def body_of(recs: list[dict]) -> bytes:
    return json.dumps(recs, separators=(",", ":")).encode()


def archive_from(recs: list[dict], *, rate_shift: float = 0.0) -> pl.DataFrame:
    """A stand-in DATA-000003 agreeing with `recs`, in the real L1's schema."""
    return pl.DataFrame(
        {
            "ts_ms": [r["fundingTime"] for r in recs],
            "funding_interval_hours": [8] * len(recs),
            "funding_rate": [float(r["fundingRate"]) + rate_shift for r in recs],
        },
        schema={"ts_ms": pl.Int64, "funding_interval_hours": pl.Int32,
                "funding_rate": pl.Float64},
    )


def parse(recs: list[dict], sha: str = "a" * 64) -> pl.DataFrame:
    return settlement.parse_body(body_of(recs), symbol=SYMBOL, capture_sha256=sha)


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    dest = tmp_path / "capture"
    shutil.copytree(GOOD, dest)
    return dest


@pytest.fixture
def stored(bundle: Path, tmp_path: Path) -> Path:
    return capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")


# ── the module must not fetch, either ───────────────────────────────────────────

def test_settlement_module_references_no_network_client() -> None:
    source = Path(settlement.__file__).read_text(encoding="utf-8")
    for banned in ("urllib", "urlopen", "requests", "httpx", "socket", "aiohttp"):
        assert banned not in source, banned


# ── happy path ──────────────────────────────────────────────────────────────────

def test_real_excerpt_parses(stored: Path) -> None:
    frame = parse(records())
    assert frame.height == len(records())
    assert set(settlement.PARSED_COLUMNS) <= set(frame.columns)


def test_every_row_is_venue_api_capture(stored: Path) -> None:
    frame = settlement.corroborate(parse(records()), archive_from(records()))
    assert frame["provenance"].unique().to_list() == ["VENUE_API_CAPTURE"]


# ── decimals are preserved exactly; floats are labelled derived ─────────────────

def test_original_decimal_strings_are_preserved_verbatim() -> None:
    recs = records()
    frame = parse(recs)
    assert frame["funding_rate_dec"].to_list() == [r["fundingRate"] for r in recs]
    assert frame["mark_price_dec"].to_list() == [r["markPrice"] for r in recs]
    assert frame.schema["funding_rate_dec"] == pl.Utf8
    assert frame.schema["mark_price_dec"] == pl.Utf8


def test_float_columns_are_derived_approximations() -> None:
    """Named `_f64` and documented lossy. `0.00007381` has no exact binary form."""
    frame = parse(records())
    assert frame.schema["funding_rate_f64"] == pl.Float64
    assert frame.schema["mark_price_f64"] == pl.Float64
    assert "approximation" in settlement.__doc__.lower()


def test_duplicate_comparison_uses_decimal_not_float() -> None:
    """Two strings that differ but collide as floats must count as a conflict."""
    a, b = records()[0], dict(records()[0])
    b["markPrice"] = "70285.700862320000000001"
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)conflict"):
        parse([a, b])


# ── top-level and element shape ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "body, match",
    [
        (b"{}", "(?i)array"),
        (b'"string"', "(?i)array"),
        (b"[]", "(?i)empty"),
        (b"[1,2]", "(?i)object"),
        (b"not json", "(?i)json"),
        (b'[{"symbol":"BTCUSDT"', "(?i)json|truncat"),
    ],
    ids=["object", "scalar", "empty", "non-object-element", "not-json", "truncated"],
)
def test_bad_top_level_shapes_are_refused(body: bytes, match: str) -> None:
    with pytest.raises(settlement.SettlementRefused, match=match):
        settlement.parse_body(body, symbol=SYMBOL, capture_sha256="a" * 64)


@pytest.mark.parametrize("field", settlement.REQUIRED_FIELDS)
def test_each_required_field_is_required(field: str) -> None:
    rec = dict(records()[0])
    del rec[field]
    with pytest.raises(settlement.SettlementRefused, match=field):
        parse([rec])


def test_rate_type_is_optional_and_uninterpreted() -> None:
    """Only 'Regular' was ever observed and we have not verified what it means."""
    rec = dict(records()[0])
    del rec["rateType"]
    frame = parse([rec])
    assert frame["rate_type_raw"].to_list() == [None]

    exotic = dict(records()[0])
    exotic["rateType"] = "SomethingWeNeverSaw"
    kept = parse([exotic])
    assert kept["rate_type_raw"].to_list() == ["SomethingWeNeverSaw"]
    assert kept.height == 1, "rate_type must never filter rows"


# ── types and numerics ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "field, value, match",
    [
        ("fundingTime", "1773331200000", "(?i)fundingTime"),
        ("fundingTime", 1.5, "(?i)fundingTime"),
        ("fundingRate", 0.001, "(?i)fundingRate"),
        ("markPrice", 70285.7, "(?i)markPrice"),
        ("symbol", 1, "(?i)symbol"),
    ],
    ids=["ts-as-str", "ts-as-float", "rate-as-float", "mark-as-float", "symbol-as-int"],
)
def test_wrong_types_are_refused(field: str, value: object, match: str) -> None:
    rec = dict(records()[0])
    rec[field] = value
    with pytest.raises(settlement.SettlementRefused, match=match):
        parse([rec])


@pytest.mark.parametrize(
    "value", ["", "   ", "NaN", "Infinity", "-Infinity", "abc", "1.2.3"]
)
def test_non_finite_or_unparseable_decimals_are_refused(value: str) -> None:
    rec = dict(records()[0])
    rec["fundingRate"] = value
    with pytest.raises(settlement.SettlementRefused):
        parse([rec])


@pytest.mark.parametrize("value", ["", "0", "0.00000000", "-1.5"])
def test_missing_or_non_positive_mark_is_refused(value: str) -> None:
    """The mark price is the reason this dataset exists; absent, it is not a settlement."""
    rec = dict(records()[0])
    rec["markPrice"] = value
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)mark"):
        parse([rec])


# ── symbol ──────────────────────────────────────────────────────────────────────

def test_mixed_symbol_capture_is_refused() -> None:
    a, b = records()[0], dict(records()[1])
    b["symbol"] = "ETHUSDT"
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)symbol"):
        parse([a, b])


def test_wrong_symbol_against_flag_is_refused() -> None:
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)symbol"):
        settlement.parse_body(body_of(records()), symbol="ETHUSDT", capture_sha256="a" * 64)


# ── duplicates ──────────────────────────────────────────────────────────────────

def test_identical_duplicate_collapses_and_is_counted() -> None:
    recs = records()
    frame = parse([*recs, recs[0]])
    assert frame.height == len(recs)
    assert frame["ts_ms"].n_unique() == len(recs)


@pytest.mark.parametrize("field", ["fundingRate", "markPrice", "symbol"])
def test_conflicting_duplicate_is_refused(field: str) -> None:
    a = records()[0]
    b = dict(a)
    b[field] = "ETHUSDT" if field == "symbol" else "0.12345678"
    match = "(?i)symbol" if field == "symbol" else "(?i)conflict"
    with pytest.raises(settlement.SettlementRefused, match=match):
        parse([a, b])


def test_conflict_message_names_the_instant_and_both_values() -> None:
    a = records()[0]
    b = dict(a)
    b["markPrice"] = "99999.00000000"
    with pytest.raises(settlement.SettlementRefused) as exc:
        parse([a, b])
    assert a["markPrice"] in str(exc.value)
    assert "99999.00000000" in str(exc.value)


# ── ordering ────────────────────────────────────────────────────────────────────

def test_out_of_order_input_is_sorted_and_reported() -> None:
    frame = parse(list(reversed(records())))
    assert frame["ts_ms"].is_sorted()


# ── corroboration against DATA-000003 ───────────────────────────────────────────

def test_matching_rows_are_flagged_archive_matched() -> None:
    recs = records()
    frame = settlement.corroborate(parse(recs), archive_from(recs))
    assert frame["funding_rate_f64_archive_matched"].all()


def test_rows_outside_archive_coverage_are_not_matched() -> None:
    recs = records()
    frame = settlement.corroborate(parse(recs), archive_from(recs[:3]))
    flags = frame["funding_rate_f64_archive_matched"].to_list()
    assert flags[:3] == [True, True, True]
    assert flags[3:] == [False, False, False]


def test_disagreement_with_the_archive_is_refused() -> None:
    recs = records()
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)disagree|mismatch"):
        settlement.corroborate(parse(recs), archive_from(recs, rate_shift=1e-6))


def test_mark_price_corroboration_is_always_none() -> None:
    """DATA-000003 holds no mark price, so nothing can corroborate ours."""
    recs = records()
    frame = settlement.corroborate(parse(recs), archive_from(recs))
    assert frame["mark_price_corroboration"].unique().to_list() == ["NONE"]
    assert "mark_price_archive_matched" not in frame.columns


def test_matched_flag_never_upgrades_provenance() -> None:
    recs = records()
    frame = settlement.corroborate(parse(recs), archive_from(recs))
    assert frame.filter(pl.col("funding_rate_f64_archive_matched"))["provenance"] \
        .unique().to_list() == ["VENUE_API_CAPTURE"]


# ── completeness ────────────────────────────────────────────────────────────────

def stamp(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def test_explicit_range_inside_archive_coverage_is_complete() -> None:
    recs = records()
    verdict = settlement.completeness(
        settlement.corroborate(parse(recs), archive_from(recs)),
        archive_from(recs),
        start=stamp(recs[0]["fundingTime"]),
        end=stamp(recs[-1]["fundingTime"]),
    )
    assert verdict.status == "COMPLETE"


def test_without_explicit_bounds_the_verdict_is_partial() -> None:
    """Inferring the range from the data is not a completeness claim about anything."""
    recs = records()
    verdict = settlement.completeness(
        settlement.corroborate(parse(recs), archive_from(recs)),
        archive_from(recs), start=None, end=None,
    )
    assert verdict.status == "PARTIAL"
    assert any("explicit" in r for r in verdict.reasons)


def test_range_extending_past_archive_coverage_is_partial() -> None:
    recs = records()
    verdict = settlement.completeness(
        settlement.corroborate(parse(recs), archive_from(recs[:3])),
        archive_from(recs[:3]),
        start=stamp(recs[0]["fundingTime"]),
        end=stamp(recs[-1]["fundingTime"]),
    )
    assert verdict.status == "PARTIAL"
    assert any("coverage" in r for r in verdict.reasons)


def test_settlement_missing_from_the_range_is_partial() -> None:
    """`end` is exclusive, so it must sit past the settlement that is meant to be missing
    — otherwise the range excludes it and the test asserts nothing."""
    recs = records()
    verdict = settlement.completeness(
        settlement.corroborate(parse(recs[:-1]), archive_from(recs)),
        archive_from(recs),
        start=stamp(recs[0]["fundingTime"]),
        end=stamp(recs[-1]["fundingTime"] + 1),
    )
    assert verdict.status == "PARTIAL"


# ── build: atomicity and the real-capture verdict ───────────────────────────────

def test_build_writes_l1_atomically(stored: Path, tmp_path: Path) -> None:
    out = tmp_path / "catalog" / "funding_settlement.parquet"
    path, _ = settlement.build([stored], symbol=SYMBOL, archive=archive_from(records()),
                               out=out)
    assert path.exists()
    assert not list(out.parent.glob("*.building"))


def test_refused_build_leaves_no_output(stored: Path, tmp_path: Path) -> None:
    out = tmp_path / "catalog" / "funding_settlement.parquet"
    with pytest.raises(settlement.SettlementRefused):
        settlement.build([stored], symbol=SYMBOL,
                         archive=archive_from(records(), rate_shift=1e-6), out=out)
    assert not out.exists()
    assert not list(out.parent.glob("*.building")) if out.parent.exists() else True


def test_require_complete_demands_explicit_bounds(stored: Path, tmp_path: Path) -> None:
    out = tmp_path / "catalog" / "funding_settlement.parquet"
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)explicit|--start"):
        settlement.build([stored], symbol=SYMBOL, archive=archive_from(records()),
                         out=out, require_complete=True)
    assert not out.exists()


def test_require_complete_refuses_a_partial_range(stored: Path, tmp_path: Path) -> None:
    recs = records()
    out = tmp_path / "catalog" / "funding_settlement.parquet"
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)partial"):
        settlement.build(
            [stored], symbol=SYMBOL, archive=archive_from(recs[:3]), out=out,
            start=stamp(recs[0]["fundingTime"]), end=stamp(recs[-1]["fundingTime"]),
            require_complete=True,
        )
    assert not out.exists()


def test_partial_build_succeeds_and_is_labelled(stored: Path, tmp_path: Path) -> None:
    """The operator's capture may be built — it just may not claim completeness."""
    recs = records()
    out = tmp_path / "catalog" / "funding_settlement.parquet"
    path, report = settlement.build([stored], symbol=SYMBOL,
                                    archive=archive_from(recs[:3]), out=out)
    assert path.exists()
    assert report.notes["completeness"] == "PARTIAL"
    assert report.notes["provenance"] == "VENUE_API_CAPTURE"
    assert report.notes["mark_price_corroboration"] == "NONE"


def test_build_combines_multiple_captures(stored: Path, tmp_path: Path,
                                          bundle: Path) -> None:
    """Overlapping pages reconcile by fundingTime; identical rows collapse."""
    second = capture.store(bundle, symbol=SYMBOL, market="um",
                           raw_root=tmp_path / "raw2")
    out = tmp_path / "catalog" / "funding_settlement.parquet"
    path, report = settlement.build([stored, second], symbol=SYMBOL,
                                    archive=archive_from(records()), out=out)
    assert pl.read_parquet(path).height == len(records())
    assert report.rows == len(records())


# ── the builder must authenticate stored L0, not hash whatever it finds ─────────

def make_bundle(dest: Path, recs: list[dict]) -> Path:
    """A self-consistent capture bundle for an arbitrary record slice."""
    import hashlib
    import re

    dest.mkdir(parents=True, exist_ok=True)
    body = body_of(recs)
    dest.joinpath("body.json").write_bytes(body)
    headers = re.sub(
        r"(?im)^content-length:.*$", f"content-length: {len(body)}",
        (GOOD / "headers.txt").read_text(encoding="utf-8"),
    )
    dest.joinpath("headers.txt").write_text(headers, encoding="utf-8")
    dest.joinpath("meta.txt").write_bytes((GOOD / "meta.txt").read_bytes())
    lines = [
        f"{hashlib.sha256(dest.joinpath(n).read_bytes()).hexdigest()}  /x/{n}"
        for n in capture.MANIFEST_FILES
    ]
    dest.joinpath("SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def test_build_refuses_a_stored_capture_whose_body_was_edited(
    stored: Path, tmp_path: Path
) -> None:
    """The reported blocker: edited bytes were hashed afresh and written to L1."""
    forged = [dict(r) for r in records()]
    forged[0]["markPrice"] = "99999.99999999"
    (stored / "body.json").write_bytes(body_of(forged))

    out = tmp_path / "catalog" / "funding_settlement.parquet"
    with pytest.raises((settlement.SettlementRefused, capture.CaptureRefused)):
        settlement.build([stored], symbol=SYMBOL, archive=archive_from(records()), out=out)
    assert not out.exists()
    assert not list(out.parent.glob("*.building")) if out.parent.exists() else True


def test_build_refuses_an_edited_body_even_with_a_rewritten_manifest(
    stored: Path, tmp_path: Path
) -> None:
    import hashlib

    forged = [dict(r) for r in records()]
    forged[0]["markPrice"] = "99999.99999999"
    (stored / "body.json").write_bytes(body_of(forged))
    lines = [
        f"{hashlib.sha256((stored / n).read_bytes()).hexdigest()}  /x/{n}"
        for n in capture.MANIFEST_FILES
    ]
    (stored / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")

    out = tmp_path / "catalog" / "funding_settlement.parquet"
    with pytest.raises((settlement.SettlementRefused, capture.CaptureRefused)):
        settlement.build([stored], symbol=SYMBOL, archive=archive_from(records()), out=out)
    assert not out.exists()


def test_build_uses_the_verified_digest_as_lineage(stored: Path, tmp_path: Path) -> None:
    out = tmp_path / "catalog" / "funding_settlement.parquet"
    path, _ = settlement.build([stored], symbol=SYMBOL, archive=archive_from(records()),
                               out=out)
    lineage = pl.read_parquet(path)["capture_sha256s"].to_list()
    assert all(entry == [stored.name] for entry in lineage)


# ── lineage across overlapping pages ────────────────────────────────────────────

def test_overlapping_pages_keep_every_contributing_capture(tmp_path: Path) -> None:
    """Collapsing an identical duplicate must not discard where the second copy came from."""
    recs = records()
    raw = tmp_path / "raw"
    first = capture.store(make_bundle(tmp_path / "b1", recs[:4]),
                          symbol=SYMBOL, market="um", raw_root=raw)
    second = capture.store(make_bundle(tmp_path / "b2", recs[2:]),
                           symbol=SYMBOL, market="um", raw_root=raw)

    out = tmp_path / "catalog" / "funding_settlement.parquet"
    path, report = settlement.build([first, second], symbol=SYMBOL,
                                    archive=archive_from(recs), out=out)
    frame = pl.read_parquet(path).sort("ts_ms")
    lineage = frame["capture_sha256s"].to_list()

    assert frame.height == len(recs)
    assert lineage[0] == [first.name]                       # only in page one
    assert lineage[-1] == [second.name]                     # only in page two
    overlap = sorted([first.name, second.name])
    assert lineage[2] == overlap and lineage[3] == overlap  # in both, sorted
    assert report.notes["identical_duplicates_collapsed"] == 2


def test_lineage_order_is_deterministic_regardless_of_page_order(tmp_path: Path) -> None:
    recs = records()
    raw = tmp_path / "raw"
    a = capture.store(make_bundle(tmp_path / "b1", recs[:4]),
                      symbol=SYMBOL, market="um", raw_root=raw)
    b = capture.store(make_bundle(tmp_path / "b2", recs[2:]),
                      symbol=SYMBOL, market="um", raw_root=raw)
    forward, _ = settlement.build([a, b], symbol=SYMBOL, archive=archive_from(recs),
                                  out=tmp_path / "f" / "o.parquet")
    reverse, _ = settlement.build([b, a], symbol=SYMBOL, archive=archive_from(recs),
                                  out=tmp_path / "r" / "o.parquet")
    assert pl.read_parquet(forward)["capture_sha256s"].to_list() == \
        pl.read_parquet(reverse)["capture_sha256s"].to_list()


# ── completeness: explicit half-open UTC range ──────────────────────────────────

def test_reversed_range_is_rejected() -> None:
    recs = records()
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)start|before|end"):
        settlement.completeness(
            settlement.corroborate(parse(recs), archive_from(recs)), archive_from(recs),
            start=stamp(recs[-1]["fundingTime"]), end=stamp(recs[0]["fundingTime"]),
        )


def test_zero_length_range_is_rejected() -> None:
    recs = records()
    moment = stamp(recs[0]["fundingTime"])
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)start|before|end"):
        settlement.completeness(
            settlement.corroborate(parse(recs), archive_from(recs)), archive_from(recs),
            start=moment, end=moment,
        )


def test_range_with_no_authenticated_settlements_is_partial() -> None:
    """A valid but empty window proves nothing, so it cannot be COMPLETE."""
    recs = records()
    gap_start = stamp(recs[0]["fundingTime"] + 1)
    gap_end = stamp(recs[1]["fundingTime"])          # exclusive: excludes recs[1]
    verdict = settlement.completeness(
        settlement.corroborate(parse(recs), archive_from(recs)), archive_from(recs),
        start=gap_start, end=gap_end,
    )
    assert verdict.status == "PARTIAL"
    assert any("zero" in r or "no settlements" in r for r in verdict.reasons)


def test_end_bound_is_exclusive_and_the_rule_bites() -> None:
    """`end` is exclusive, and this test can tell the difference.

    The archive holds a settlement stamped exactly at `end`; the capture does not. Under
    half-open semantics that settlement is outside the window, so the range is COMPLETE.
    Under inclusive semantics it would be inside and missing, and this would fail.
    """
    recs = records()
    boundary = recs[4]
    captured = [r for i, r in enumerate(recs) if i != 4]     # capture omits the boundary
    archive = archive_from(recs)                             # archive keeps it
    frame = settlement.corroborate(parse(captured), archive)

    # The premise that makes the assertion below meaningful.
    assert boundary["fundingTime"] in set(archive["ts_ms"])
    assert boundary["fundingTime"] not in set(frame["ts_ms"])

    verdict = settlement.completeness(
        frame, archive,
        start=stamp(recs[1]["fundingTime"]), end=stamp(boundary["fundingTime"]),
    )
    assert verdict.status == "COMPLETE", verdict.reasons


def test_a_settlement_just_inside_end_is_still_required() -> None:
    """The mirror image: move `end` one millisecond past the boundary and the same
    missing settlement must make the range PARTIAL."""
    recs = records()
    boundary = recs[4]
    captured = [r for i, r in enumerate(recs) if i != 4]
    archive = archive_from(recs)
    verdict = settlement.completeness(
        settlement.corroborate(parse(captured), archive), archive,
        start=stamp(recs[1]["fundingTime"]),
        end=stamp(boundary["fundingTime"] + 1),
    )
    assert verdict.status == "PARTIAL"


def test_half_open_range_missing_an_interior_settlement_is_partial() -> None:
    recs = records()
    without_middle = [r for i, r in enumerate(recs) if i != 2]
    verdict = settlement.completeness(
        settlement.corroborate(parse(without_middle), archive_from(recs)),
        archive_from(recs),
        start=stamp(recs[0]["fundingTime"]), end=stamp(recs[-1]["fundingTime"]),
    )
    assert verdict.status == "PARTIAL"


@pytest.mark.parametrize(
    "case",
    ["no-bounds", "beyond-coverage", "missing-settlement", "empty-window"],
)
def test_require_complete_refuses_every_non_complete_case(
    stored: Path, tmp_path: Path, case: str
) -> None:
    recs = records()
    out = tmp_path / "catalog" / f"{case}.parquet"
    kwargs: dict = {"start": None, "end": None}
    archive = archive_from(recs)
    if case == "beyond-coverage":
        archive = archive_from(recs[:3])
        kwargs = {"start": stamp(recs[0]["fundingTime"]),
                  "end": stamp(recs[-1]["fundingTime"] + 1)}
    elif case == "missing-settlement":
        archive = archive_from([*recs, {"fundingTime": recs[-1]["fundingTime"] + HOUR8,
                                        "fundingRate": "0.00001000"}])
        kwargs = {"start": stamp(recs[0]["fundingTime"]),
                  "end": stamp(recs[-1]["fundingTime"] + HOUR8 + 1)}
    elif case == "empty-window":
        kwargs = {"start": stamp(recs[0]["fundingTime"] + 1),
                  "end": stamp(recs[1]["fundingTime"])}

    with pytest.raises(settlement.SettlementRefused):
        settlement.build([stored], symbol=SYMBOL, archive=archive, out=out,
                         require_complete=True, **kwargs)
    assert not out.exists()
    assert not list(out.parent.glob("*.building")) if out.parent.exists() else True


def test_require_complete_accepts_a_genuinely_complete_range(
    stored: Path, tmp_path: Path
) -> None:
    recs = records()
    out = tmp_path / "catalog" / "ok.parquet"
    path, report = settlement.build(
        [stored], symbol=SYMBOL, archive=archive_from(recs), out=out,
        start=stamp(recs[0]["fundingTime"]), end=stamp(recs[-1]["fundingTime"] + 1),
        require_complete=True,
    )
    assert path.exists()
    assert report.notes["completeness"] == "COMPLETE"


# ── rateType participates in duplicate identity ─────────────────────────────────

def test_differing_rate_type_is_a_conflicting_duplicate() -> None:
    """Same instant, same rate, same mark, different `rateType`: keeping the first
    silently discards a difference the venue reported. Preservation, not interpretation."""
    a = records()[0]
    b = dict(a)
    b["rateType"] = "SomethingElse"
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)conflict"):
        parse([a, b])


def test_rate_type_conflict_names_both_values() -> None:
    a = records()[0]
    b = dict(a)
    b["rateType"] = "SomethingElse"
    with pytest.raises(settlement.SettlementRefused) as exc:
        parse([a, b])
    assert "Regular" in str(exc.value) and "SomethingElse" in str(exc.value)


def test_identical_rate_type_still_collapses() -> None:
    a = records()[0]
    assert parse([a, dict(a)]).height == 1


def test_absent_rate_type_on_both_sides_collapses() -> None:
    a = {k: v for k, v in records()[0].items() if k != "rateType"}
    assert parse([a, dict(a)]).height == 1


def test_rate_type_present_on_one_side_only_is_a_conflict() -> None:
    a = records()[0]
    b = {k: v for k, v in a.items() if k != "rateType"}
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)conflict"):
        parse([a, b])


# ── interval: measured, or honestly unknown ─────────────────────────────────────

def test_single_observation_has_no_measurable_interval() -> None:
    """One row yields no delta. Returning eight hours here would be an assumption."""
    assert settlement._interval_ms([1_773_331_200_000]) == (None, None)


def test_two_observations_give_one_unambiguous_interval() -> None:
    t = 1_773_331_200_000
    assert settlement._interval_ms([t, t + HOUR8]) == (HOUR8, 0)


def test_a_clear_mode_across_two_buckets_no_longer_wins() -> None:
    """This used to accept 8h because it out-voted 1h two-to-one.

    A clear winner is not the same as a stationary grid: applying 8h to a span containing
    a 1h delta under-counts the expected rows. The mode is no longer sufficient on its own.
    """
    t = 1_773_331_200_000
    ts = [t, t + HOUR8, t + 2 * HOUR8, t + 2 * HOUR8 + 3_600_000]
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)stationary|mixed"):
        settlement._interval_ms(ts)


def test_a_clear_grid_with_whole_gaps_is_accepted() -> None:
    """What a genuine single-interval measurement looks like: one bucket, gaps that span
    a whole number of settlements."""
    t = 1_773_331_200_000
    interval, deviation = settlement._interval_ms(
        [t, t + HOUR8, t + 3 * HOUR8, t + 4 * HOUR8]
    )
    assert interval == HOUR8
    assert deviation == 0


def test_a_tied_mode_is_ambiguous_and_refuses() -> None:
    """Two deltas, one each: set iteration order used to pick the winner silently."""
    t = 1_773_331_200_000
    ts = [t, t + HOUR8, t + HOUR8 + 3_600_000]
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)ambiguous|tie"):
        settlement._interval_ms(ts)


def test_single_row_capture_refuses_the_l1_build(tmp_path: Path) -> None:
    """One observation cannot yield an interval, so completeness has no denominator.

    Setting `expected_rows` to the row count made this report 100% complete while the
    interval was unmeasurable. Refusing is the honest answer: the capture is still valid
    L0 evidence, it just cannot yet support a claim about coverage.
    """
    raw = tmp_path / "raw"
    one = capture.store(make_bundle(tmp_path / "b1", records()[:1]),
                        symbol=SYMBOL, market="um", raw_root=raw)
    out = tmp_path / "catalog" / "one.parquet"
    with pytest.raises(canonical.BuildRefused, match=r"(?i)interval cannot be measured"):
        settlement.build([one], symbol=SYMBOL, archive=archive_from(records()[:1]), out=out)
    assert not out.exists()
    assert not list(out.parent.glob("*.building")) if out.parent.exists() else True
    # The capture itself remains admissible evidence.
    assert capture.verify_stored(one).body_sha256 == one.name


def test_two_row_capture_builds_with_a_measured_interval(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    two = capture.store(make_bundle(tmp_path / "b2", records()[:2]),
                        symbol=SYMBOL, market="um", raw_root=raw)
    out = tmp_path / "catalog" / "two.parquet"
    _, report = settlement.build([two], symbol=SYMBOL,
                                 archive=archive_from(records()[:2]), out=out)
    assert report.notes["interval_ms"] == HOUR8
    assert report.expected_rows == 2


# ── corroboration wording: what is actually compared ────────────────────────────

def test_matched_column_names_the_representation_compared() -> None:
    recs = records()
    frame = settlement.corroborate(parse(recs), archive_from(recs))
    assert "funding_rate_f64_archive_matched" in frame.columns
    # The imprecise name must be gone: it implied the original decimal was checked.
    assert "funding_fields_archive_matched" not in frame.columns


def test_notes_do_not_claim_decimal_authentication(stored: Path, tmp_path: Path) -> None:
    """DATA-000003 stores Float64. The original venue decimal string is not what is
    compared, and the wording must not imply otherwise."""
    out = tmp_path / "catalog" / "o.parquet"
    _, report = settlement.build([stored], symbol=SYMBOL,
                                 archive=archive_from(records()), out=out)
    assert "funding_rate_f64_archive_matched_rows" in report.notes
    text = (settlement.__doc__ or "").lower()
    assert "float64" in text
    assert report.notes["provenance"] == "VENUE_API_CAPTURE"
    assert report.notes["mark_price_corroboration"] == "NONE"


# ── verification and consumption must not be separated ──────────────────────────

def test_mutation_between_verification_and_parsing_is_not_consumed(
    stored: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported race: `verify_stored()` then a separate `read_bytes()` left a window
    in which a forged mark price could be swapped in and written to L1 carrying the
    previously verified capture's hash as lineage."""
    original = json.loads((stored / "body.json").read_bytes())
    forged = [dict(r) for r in original]
    forged[0]["markPrice"] = "99999.99999999"

    real = capture.read_verified_stored

    def mutate_after_verifying(directory: Path) -> capture.VerifiedCapture:
        verified = real(directory)
        # The file changes the instant verification finishes.
        (directory / "body.json").write_bytes(body_of(forged))
        return verified

    monkeypatch.setattr(capture, "read_verified_stored", mutate_after_verifying)

    out = tmp_path / "catalog" / "race.parquet"
    try:
        path, _ = settlement.build([stored], symbol=SYMBOL,
                                   archive=archive_from(original), out=out)
    except (settlement.SettlementRefused, capture.CaptureRefused):
        assert not out.exists()
        return                                   # refusing is an acceptable outcome

    frame = pl.read_parquet(path).sort("ts_ms")
    marks = frame["mark_price_dec"].to_list()
    assert "99999.99999999" not in marks, "the mutation was consumed after verification"
    assert marks[0] == original[0]["markPrice"]
    # And the lineage must still describe bytes that really do hash to it.
    assert frame["capture_sha256s"].to_list()[0] == [stored.name]


def test_build_never_reopens_body_json(stored: Path, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Structural guarantee: after verification the file is not touched again."""
    opened: list[str] = []
    real_read = Path.read_bytes

    def watched(self: Path) -> bytes:
        opened.append(str(self))
        return real_read(self)

    verified = capture.read_verified_stored(stored)
    archive = archive_from(records())          # built before watching: the fixture read
    out = tmp_path / "catalog" / "once.parquet"                # here is the test's own

    monkeypatch.setattr(Path, "read_bytes", watched)
    monkeypatch.setattr(capture, "read_verified_stored", lambda _d: verified)
    settlement.build([stored], symbol=SYMBOL, archive=archive, out=out)

    assert not [p for p in opened if p.endswith("body.json")], opened


# ── expected rows must be a grid count, not a floor division ────────────────────
# The interval detector tolerates small jitter, so the span between first and last stamp
# is rarely an exact multiple of the interval. `(high - low) // step + 1` floors that span
# and can report fewer expected rows than actually exist — five real observations against
# four expected, i.e. 125% "completeness", which is not a number that can mean anything.

def grid(count: int, *, step: int = HOUR8, last_offset_ms: int = 0) -> list[dict]:
    """`count` settlements on a clean grid, with the final stamp nudged by `last_offset_ms`."""
    base = 1_773_360_000_000
    stamps = [base + i * step for i in range(count)]
    stamps[-1] += last_offset_ms
    return [
        {"symbol": SYMBOL, "fundingTime": t,
         "fundingRate": f"0.0000{i + 1}000", "markPrice": f"7000{i}.00000000",
         "rateType": "Regular"}
        for i, t in enumerate(stamps)
    ]


def report_for(recs: list[dict], tmp_path: Path, name: str) -> object:
    raw = tmp_path / f"raw-{name}"
    stored = capture.store(make_bundle(tmp_path / f"b-{name}", recs),
                           symbol=SYMBOL, market="um", raw_root=raw)
    _, report = settlement.build([stored], symbol=SYMBOL, archive=archive_from(recs),
                                 out=tmp_path / f"c-{name}" / "o.parquet")
    return report


@pytest.mark.parametrize(
    "offset, label",
    [(-5, "inward"), (5, "outward"), (0, "exact"), (-999, "inward-at-tolerance"),
     (999, "outward-at-tolerance")],
)
def test_endpoint_jitter_does_not_distort_expected_rows(
    tmp_path: Path, offset: int, label: str
) -> None:
    report = report_for(grid(5, last_offset_ms=offset), tmp_path, label)
    assert report.rows == 5
    assert report.expected_rows == 5, f"{label} jitter changed the grid count"


@pytest.mark.parametrize("offset", [-5, 5, 0, -999, 999])
def test_completeness_never_exceeds_one_hundred_percent(
    tmp_path: Path, offset: int
) -> None:
    """For a unique, monotonic, gapless frame the ceiling is exactly 1.0."""
    report = report_for(grid(5, last_offset_ms=offset), tmp_path, f"c{offset}")
    assert report.completeness <= 1.0
    assert report.completeness == pytest.approx(1.0)


def test_a_real_gap_still_raises_expected_rows(tmp_path: Path) -> None:
    """The fix must not blunt genuine gap detection: a missing settlement is still one
    more expected row than observed."""
    recs = [r for i, r in enumerate(grid(6)) if i != 3]      # drop one interior settlement
    report = report_for(recs, tmp_path, "gap")
    assert report.rows == 5
    assert report.expected_rows == 6
    assert report.completeness < 1.0


def test_a_multi_interval_gap_is_counted_in_full(tmp_path: Path) -> None:
    recs = [r for i, r in enumerate(grid(7)) if i not in (2, 3)]   # two in a row missing
    report = report_for(recs, tmp_path, "gap2")
    assert report.rows == 5
    assert report.expected_rows == 7


def test_jitter_and_a_gap_together_stay_consistent(tmp_path: Path) -> None:
    recs = [r for i, r in enumerate(grid(6, last_offset_ms=-7)) if i != 2]
    report = report_for(recs, tmp_path, "both")
    assert report.rows == 5
    assert report.expected_rows == 6


# ── one interval must describe the whole span ───────────────────────────────────
# A single winning bucket is not enough. `_validator` applies one interval across the
# entire span, so the grid must actually be stationary: every delta a whole multiple of
# that interval. Without both conditions the span can be shorter than the row count
# implies and completeness climbs above 100%, which is not a number that can mean anything.
#
# Clamping with `max(expected, rows)` is deliberately NOT the fix: it would paper over a
# genuinely missing settlement, which is the exact defect this dataset exists to surface.

HOUR1 = 3_600_000
HOUR4 = 14_400_000
BASE = 1_773_360_000_000


def stamps_at(hours: list[int]) -> list[int]:
    return [BASE + h * HOUR1 for h in hours]


def records_at(hours: list[int]) -> list[dict]:
    return [
        {"symbol": SYMBOL, "fundingTime": t,
         "fundingRate": f"0.0000{i + 1}000", "markPrice": f"7000{i}.00000000",
         "rateType": "Regular"}
        for i, t in enumerate(stamps_at(hours))
    ]


def test_the_reported_mixed_interval_sequence_is_refused() -> None:
    """0h, 1h, 5h, 9h: deltas 1h/4h/4h. Every delta is a supported interval, one bucket
    wins outright, and applying it to the whole span reported 4 rows against 3 expected."""
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)mixed|stationary|gap"):
        settlement._interval_ms(stamps_at([0, 1, 5, 9]))


def test_mixed_regime_diagnostic_is_distinct_from_the_tie_diagnostic() -> None:
    """Unequal votes across buckets is a different fault from equal votes, and the
    operator needs to be told which one they have."""
    with pytest.raises(settlement.SettlementRefused) as mixed:
        settlement._interval_ms(stamps_at([0, 1, 5, 9]))            # votes 4h:2, 1h:1
    with pytest.raises(settlement.SettlementRefused) as tie:
        settlement._interval_ms(stamps_at([0, 8, 9]))               # votes 8h:1, 1h:1
    assert "ambiguous" in str(tie.value).lower()
    assert str(mixed.value) != str(tie.value)


def test_a_gap_that_looks_like_another_supported_interval_is_refused() -> None:
    """A 4h schedule missing one settlement yields an 8h delta. That is genuinely
    ambiguous between a schedule change and a gap, and must not be silently classified."""
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)mixed|gap|stationary"):
        settlement._interval_ms(stamps_at([0, 4, 12, 16, 20]))      # 4h,8h,4h,4h


def test_a_delta_matching_no_multiple_of_the_interval_is_refused() -> None:
    """8h,8h,8h,3h: the 3h delta votes for nothing, so one bucket still wins, but the
    span is shorter than four settlements on an 8h grid."""
    with pytest.raises(settlement.SettlementRefused, match=r"(?i)stationary|multiple"):
        settlement._interval_ms(stamps_at([0, 8, 16, 24, 27]))


def test_whole_multiples_of_the_interval_remain_gaps_not_faults() -> None:
    """A missing 8h settlement gives 16h — a clean multiple, and still a gap."""
    interval, _ = settlement._interval_ms(stamps_at([0, 8, 24, 32]))
    assert interval == HOUR8


@pytest.mark.parametrize(
    "hours, label",
    [
        ([0, 8, 16, 24, 32], "clean-grid"),
        ([0, 8, 24, 32], "one-gap"),
        ([0, 8, 32, 40], "two-gap"),
        ([0, 4, 8, 12], "four-hour-grid"),
        ([0, 1, 2, 3], "one-hour-grid"),
    ],
)
def test_accepted_frames_never_report_more_rows_than_expected(
    tmp_path: Path, hours: list[int], label: str
) -> None:
    """The invariant: for anything the validator accepts, expected_rows >= rows and
    completeness <= 1.0. Proven on the built report, not on the helper alone."""
    report = report_for(records_at(hours), tmp_path, label)
    assert report.expected_rows >= report.rows, label
    assert report.completeness <= 1.0, label


@pytest.mark.parametrize(
    "hours",
    [[0, 1, 5, 9], [0, 8, 16, 24, 27], [0, 4, 12, 16, 20], [0, 8, 9]],
    ids=["mixed-1h-4h", "non-multiple-tail", "gap-as-interval", "tied"],
)
def test_no_l1_survives_a_non_stationary_grid(tmp_path: Path, hours: list[int]) -> None:
    raw = tmp_path / "raw"
    stored = capture.store(make_bundle(tmp_path / "b", records_at(hours)),
                           symbol=SYMBOL, market="um", raw_root=raw)
    out = tmp_path / "catalog" / "o.parquet"
    with pytest.raises((canonical.BuildRefused, settlement.SettlementRefused)):
        settlement.build([stored], symbol=SYMBOL, archive=archive_from(records_at(hours)),
                         out=out)
    assert not out.exists()
    assert not list(out.parent.glob("*.building")) if out.parent.exists() else True
