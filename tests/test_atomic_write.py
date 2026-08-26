"""The public atomic-write boundary.

`write_validated_parquet` was private until a second dataset family needed it. Promoting
it is only defensible if the guarantee callers rely on is pinned by tests rather than by
the reader's memory of the implementation: **a refused build leaves nothing behind, and an
existing L1 survives it untouched.**

Everything here writes to tmp_path. The real catalog is never opened.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from supercortex.data import canonical


def frame(rows: int = 3) -> pl.DataFrame:
    return pl.DataFrame(
        {"ts_ms": [1_600_000_000_000 + i * 60_000 for i in range(rows)],
         "value": list(range(rows))},
        schema={"ts_ms": pl.Int64, "value": pl.Int64},
    )


def clean(_: pl.DataFrame) -> canonical.QualityReport:
    return canonical.QualityReport(rows=3, start="a", end="b", expected_rows=3)


def refusing(_: pl.DataFrame) -> canonical.QualityReport:
    return canonical.QualityReport(
        rows=3, start="a", end="b", expected_rows=3, violations=["deliberate refusal"]
    )


def test_public_name_exists_and_is_the_documented_boundary() -> None:
    assert callable(canonical.write_validated_parquet)
    assert "atomically" in (canonical.write_validated_parquet.__doc__ or "")


def test_private_wrapper_is_retained_for_existing_callers() -> None:
    assert callable(canonical._validate_and_write)


def test_wrapper_and_public_function_agree(tmp_path: Path) -> None:
    a, _ = canonical.write_validated_parquet(frame(), clean, tmp_path / "a.parquet")
    b, _ = canonical._validate_and_write(frame(), clean, tmp_path / "b.parquet")
    assert pl.read_parquet(a).equals(pl.read_parquet(b))


def test_successful_write_produces_the_file_and_no_staging(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "out.parquet"
    path, report = canonical.write_validated_parquet(frame(), clean, out)
    assert path == out and out.exists()
    assert report.ok
    assert not list(out.parent.glob("*.building"))


def test_refusal_writes_nothing_at_all(tmp_path: Path) -> None:
    out = tmp_path / "out.parquet"
    with pytest.raises(canonical.BuildRefused, match=r"deliberate refusal"):
        canonical.write_validated_parquet(frame(), refusing, out)
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []


def test_refusal_leaves_an_existing_l1_untouched(tmp_path: Path) -> None:
    """The failure that matters: a bad rebuild must not damage yesterday's good dataset."""
    out = tmp_path / "out.parquet"
    canonical.write_validated_parquet(frame(rows=3), clean, out)
    original = out.read_bytes()

    with pytest.raises(canonical.BuildRefused):
        canonical.write_validated_parquet(frame(rows=9), refusing, out)

    assert out.read_bytes() == original
    assert not list(tmp_path.glob("*.building"))


def test_refusal_carries_the_report(tmp_path: Path) -> None:
    with pytest.raises(canonical.BuildRefused) as exc:
        canonical.write_validated_parquet(frame(), refusing, tmp_path / "out.parquet")
    assert exc.value.report is not None
    assert exc.value.report.violations == ["deliberate refusal"]


def test_notes_reach_the_report(tmp_path: Path) -> None:
    _, report = canonical.write_validated_parquet(
        frame(), clean, tmp_path / "out.parquet", notes={"provenance": "TEST"}
    )
    assert report.notes["provenance"] == "TEST"
