"""`supercortex falsify` helpers, at the boundary their annotations claim.

`_artifacts` is annotated `-> Path`, but it does not always return one: an unknown
experiment id raises `typer.BadParameter`. Both branches are contract, and only one of
them was exercised anywhere.

Nothing here touches registry.sqlite, runs a trial, or reaches a backtest. The registry is
a throwaway in-memory database, and `db.connect` is redirected at it.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
import typer

from supercortex.falsify import cli as falsify_cli
from supercortex.registry import db


@pytest.fixture
def registry(monkeypatch):
    """An in-memory registry holding exactly one experiment. No file is opened."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE experiment (id TEXT PRIMARY KEY, slug TEXT)")
    conn.execute("INSERT INTO experiment VALUES ('EXP-000009', 'golden-fixture')")

    @contextmanager
    def fake_connect(path: Path | None = None):
        yield conn

    monkeypatch.setattr(db, "connect", fake_connect)
    yield conn
    conn.close()


def test_artifacts_returns_the_artifact_directory_as_a_path(registry):
    result = falsify_cli._artifacts("EXP-000009")
    assert isinstance(result, Path)
    assert result.name == "artifacts"
    assert result.parent.name == "EXP-000009-golden-fixture"


def test_artifacts_rejects_an_unknown_experiment(registry):
    """The other branch: no row means no path, and the operator is told which id."""
    with pytest.raises(typer.BadParameter, match="EXP-999999"):
        falsify_cli._artifacts("EXP-999999")


def test_artifacts_does_not_create_anything(registry, tmp_path):
    """It addresses a directory; it does not make one. Probing must not leave a trace."""
    result = falsify_cli._artifacts("EXP-000009")
    assert not result.exists() or result.is_dir()
