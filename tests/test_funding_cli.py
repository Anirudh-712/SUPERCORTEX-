"""The two funding-settlement CLI commands.

These are the surface an operator actually touches, and the properties that matter here
are boundary properties rather than arithmetic: the commands take local paths and nothing
else, they refuse rather than half-succeed, and once a capture is stored the *stored*
bytes are the subject — the source bundle is never consulted again.

Every test redirects the lake roots at tmp_path. The real `data/` and `registry.sqlite`
are never opened.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from supercortex.data import canonical, capture, cli, sources

REPO = Path(__file__).resolve().parents[1]
GOOD = REPO / "tests" / "fixtures" / "funding_settlement" / "good"
SYMBOL = "BTCUSDT"
runner = CliRunner()


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both lake roots at tmp_path. Nothing here may touch the real lake."""
    monkeypatch.setattr(sources, "RAW_ROOT", tmp_path / "raw")
    monkeypatch.setattr(canonical, "CATALOG_ROOT", tmp_path / "catalog")
    monkeypatch.setattr(cli.db, "PROJECT_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    dest = tmp_path / "capture"
    shutil.copytree(GOOD, dest)
    return dest


def records() -> list[dict]:
    return json.loads((GOOD / "body.json").read_bytes())


def write_archive(lake: Path, recs: list[dict]) -> Path:
    path = lake / "catalog" / "funding_rate" / f"{SYMBOL}.um" / "funding.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {"ts_ms": [r["fundingTime"] for r in recs],
         "funding_interval_hours": [8] * len(recs),
         "funding_rate": [float(r["fundingRate"]) for r in recs]},
        schema={"ts_ms": pl.Int64, "funding_interval_hours": pl.Int32,
                "funding_rate": pl.Float64},
    ).write_parquet(path)
    return path


def make_bundle(dest: Path, recs: list[dict]) -> Path:
    """A self-consistent capture bundle for an arbitrary record slice."""
    dest.mkdir(parents=True, exist_ok=True)
    body = json.dumps(recs, separators=(",", ":")).encode()
    dest.joinpath("body.json").write_bytes(body)
    dest.joinpath("headers.txt").write_text(
        re.sub(r"(?im)^content-length:.*$", f"content-length: {len(body)}",
               (GOOD / "headers.txt").read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    dest.joinpath("meta.txt").write_bytes((GOOD / "meta.txt").read_bytes())
    lines = [
        f"{hashlib.sha256(dest.joinpath(n).read_bytes()).hexdigest()}  /x/{n}"
        for n in capture.MANIFEST_FILES
    ]
    dest.joinpath("SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


#: Exactly 2026-03-13T00:00:00.000Z. Real settlement instants jitter by milliseconds, so
#: none of them ever lands exactly on a UTC midnight — which is the only instant a
#: date-bounded `--end` can name. A boundary test built on the real fixture therefore
#: cannot place a settlement *on* the bound, and proves nothing. These few records are
#: synthetic for that reason, and only for the boundary tests.
MIDNIGHT = 1_773_360_000_000
HOUR8 = 28_800_000


def synthetic(timestamps: list[int]) -> list[dict]:
    return [
        {"symbol": SYMBOL, "fundingTime": t,
         "fundingRate": f"0.0000{i + 1}000", "markPrice": f"7000{i}.00000000",
         "rateType": "Regular"}
        for i, t in enumerate(timestamps)
    ]


def import_page(bundle: Path) -> object:
    return runner.invoke(
        cli.app,
        ["import-funding-page", "--capture-dir", str(bundle), "--symbol", SYMBOL],
    )


def build(*args: str) -> object:
    return runner.invoke(cli.app, ["build-funding-settlement", "--symbol", SYMBOL, *args])


def flat(result: object) -> str:
    """Console output with wrapping removed — rich breaks lines wherever it likes."""
    return re.sub(r"\s+", " ", result.output)  # type: ignore[attr-defined]


def stamp(ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


# ── import ──────────────────────────────────────────────────────────────────────

def test_import_succeeds_and_reports_the_trust_position(lake: Path, bundle: Path) -> None:
    result = import_page(bundle)
    assert result.exit_code == 0, result.output
    assert "VENUE_API_CAPTURE" in flat(result)
    assert "NONE" in flat(result)
    # The refusal to overclaim must reach the operator, not just the docstring.
    assert "does not authenticate the publisher" in flat(result)


def test_import_stores_under_the_full_content_address(lake: Path, bundle: Path) -> None:
    assert import_page(bundle).exit_code == 0
    root = capture.capture_root(sources.RAW_ROOT, "um", SYMBOL)
    stored = list(root.iterdir())
    assert len(stored) == 1
    assert stored[0].name == hashlib.sha256((GOOD / "body.json").read_bytes()).hexdigest()


def test_import_refuses_a_broken_bundle_with_a_clean_exit(lake: Path, bundle: Path) -> None:
    (bundle / "body.json").write_bytes(b"[]")
    result = import_page(bundle)
    assert result.exit_code == 1
    assert "capture refused" in flat(result)
    assert not capture.capture_root(sources.RAW_ROOT, "um", SYMBOL).exists()


def test_import_refuses_a_missing_directory(lake: Path, tmp_path: Path) -> None:
    result = import_page(tmp_path / "absent")
    assert result.exit_code == 1
    assert "capture refused" in flat(result)


def test_source_bundle_is_not_read_after_storage(
    lake: Path, bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`admit` used to run twice: once inside `store`, then again on the source purely to
    print. The stored bytes are the subject once storage succeeds."""
    admitted: list[Path] = []
    verified: list[Path] = []
    real_admit, real_verify = capture.admit, capture.verify_stored

    def rec_admit(directory: Path, *, symbol: str) -> capture.CaptureFacts:
        admitted.append(Path(directory))
        return real_admit(directory, symbol=symbol)

    def rec_verify(directory: Path) -> capture.CaptureFacts:
        verified.append(Path(directory))
        return real_verify(directory)

    monkeypatch.setattr(capture, "admit", rec_admit)
    monkeypatch.setattr(capture, "verify_stored", rec_verify)
    assert import_page(bundle).exit_code == 0

    assert admitted.count(bundle) == 1, f"source admitted {admitted.count(bundle)} times"
    assert verified, "the stored capture was never verified"
    assert all(p != bundle for p in verified), "verification ran against the source"


# ── build: preconditions ────────────────────────────────────────────────────────

def test_build_without_captures_refuses(lake: Path) -> None:
    write_archive(lake, records())
    result = build()
    assert result.exit_code == 1
    assert "no stored captures" in flat(result)


def test_build_without_the_archive_refuses(lake: Path, bundle: Path) -> None:
    assert import_page(bundle).exit_code == 0
    result = build()
    assert result.exit_code == 1
    assert "DATA-000003 not built" in flat(result)


# ── build: bounds ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["2026/03/12", "12-03-2026", "yesterday", ""])
def test_malformed_bounds_are_rejected(lake: Path, bundle: Path, bad: str) -> None:
    assert import_page(bundle).exit_code == 0
    write_archive(lake, records())
    result = build("--start", bad, "--end", "2026-08-26")
    assert result.exit_code != 0
    assert "YYYY-MM-DD" in flat(result)


def test_reversed_bounds_are_rejected(lake: Path, bundle: Path) -> None:
    assert import_page(bundle).exit_code == 0
    write_archive(lake, records())
    result = build("--start", "2026-08-26", "--end", "2026-03-12")
    assert result.exit_code == 1
    assert "not before" in flat(result)


def test_end_is_documented_as_exclusive() -> None:
    help_text = runner.invoke(cli.app, ["build-funding-settlement", "--help"]).output
    assert "EXCLUSIVE" in re.sub(r"\s+", " ", help_text)


# ── build: completeness ─────────────────────────────────────────────────────────

def test_build_succeeds_as_partial_without_bounds(lake: Path, bundle: Path) -> None:
    assert import_page(bundle).exit_code == 0
    write_archive(lake, records())
    result = build()
    assert result.exit_code == 0, result.output
    assert "PARTIAL" in flat(result)
    assert "VENUE_API_CAPTURE" in flat(result)


def test_require_complete_refuses_a_partial_range(lake: Path, bundle: Path) -> None:
    assert import_page(bundle).exit_code == 0
    write_archive(lake, records()[:3])
    recs = records()
    result = build("--start", stamp(recs[0]["fundingTime"]),
                   "--end", stamp(recs[-1]["fundingTime"] + 86_400_000),
                   "--require-complete")
    assert result.exit_code == 1
    assert "build refused" in flat(result)
    out = canonical.CATALOG_ROOT / "funding_settlement" / f"{SYMBOL}.um"
    assert not out.exists() or not list(out.glob("*.parquet"))


def test_require_complete_without_bounds_refuses(lake: Path, bundle: Path) -> None:
    assert import_page(bundle).exit_code == 0
    write_archive(lake, records())
    result = build("--require-complete")
    assert result.exit_code == 1
    assert "--start" in flat(result) or "explicit" in flat(result)


def test_exclusive_end_excludes_the_boundary_settlement(
    lake: Path, tmp_path: Path
) -> None:
    """CLI-level proof that `--end` is exclusive, arranged so it can fail.

    A settlement sits at exactly 2026-03-14T00:00:00.000Z — the instant `--end 2026-03-14`
    names. The archive keeps it; the imported capture omits it. Half-open therefore
    excludes it and the range is COMPLETE. Were `--end` inclusive it would be inside the
    window and missing from the capture, and `--require-complete` would refuse.
    """
    boundary = MIDNIGHT + 3 * HOUR8                    # 2026-03-14T00:00:00.000Z
    all_ts = [MIDNIGHT, MIDNIGHT + HOUR8, MIDNIGHT + 2 * HOUR8, boundary,
              boundary + HOUR8]
    recs = synthetic(all_ts)
    assert boundary % 86_400_000 == 0, "the boundary must land exactly on UTC midnight"
    assert stamp(boundary) == "2026-03-14"

    captured = [r for r in recs if r["fundingTime"] != boundary]
    assert import_page(make_bundle(tmp_path / "partial-capture", captured)).exit_code == 0
    write_archive(lake, recs)                          # archive still holds the boundary

    result = build("--start", "2026-03-13", "--end", "2026-03-14", "--require-complete")
    assert result.exit_code == 0, result.output
    assert "COMPLETE" in flat(result)


def test_moving_end_past_the_boundary_makes_it_required(
    lake: Path, tmp_path: Path
) -> None:
    """Same capture and archive, `--end` one day later: the omitted settlement is now
    inside the window and the build must refuse."""
    boundary = MIDNIGHT + 3 * HOUR8
    recs = synthetic([MIDNIGHT, MIDNIGHT + HOUR8, MIDNIGHT + 2 * HOUR8, boundary,
                      boundary + HOUR8])
    captured = [r for r in recs if r["fundingTime"] != boundary]
    assert import_page(make_bundle(tmp_path / "partial-capture", captured)).exit_code == 0
    write_archive(lake, recs)

    result = build("--start", "2026-03-13", "--end", "2026-03-15", "--require-complete")
    assert result.exit_code == 1
    assert "build refused" in flat(result)


def test_build_writes_l1_with_lineage(lake: Path, bundle: Path) -> None:
    assert import_page(bundle).exit_code == 0
    write_archive(lake, records())
    assert build().exit_code == 0
    out = (canonical.CATALOG_ROOT / "funding_settlement" / f"{SYMBOL}.um"
           / "funding_settlement.parquet")
    frame = pl.read_parquet(out)
    assert frame.height == len(records())
    assert frame["capture_sha256s"].dtype == pl.List(pl.Utf8)
    assert frame["provenance"].unique().to_list() == ["VENUE_API_CAPTURE"]
    assert frame["mark_price_corroboration"].unique().to_list() == ["NONE"]


def test_build_refuses_a_tampered_stored_capture(lake: Path, bundle: Path) -> None:
    """End to end: the CLI must not bless edited L0 either."""
    assert import_page(bundle).exit_code == 0
    write_archive(lake, records())
    stored = next(capture.capture_root(sources.RAW_ROOT, "um", SYMBOL).iterdir())
    forged = [dict(r) for r in records()]
    forged[0]["markPrice"] = "99999.99999999"
    (stored / "body.json").write_bytes(json.dumps(forged, separators=(",", ":")).encode())

    result = build()
    assert result.exit_code == 1
    out = canonical.CATALOG_ROOT / "funding_settlement" / f"{SYMBOL}.um"
    assert not out.exists() or not list(out.glob("*.parquet"))
