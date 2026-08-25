"""The sealed artifact must be provably the one that was sealed.

`close_trial` stored `metrics_path` and nothing about the bytes at it, so the loader was
verifying a self-consistent *mutable document*. Editing `cost_model.maker_fee_bps` to 999.0
after sealing produced a fully verifying trial with a cost model the run never used - every
internal cross-check agreed, because the forger edited one field and nothing referenced it.

The fix is an immutable digest of the exact serialized bytes, written by the same seal
operation that writes `outcome` and `metrics_path`, and protected by the same trigger that
makes the rest of the row immutable.
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from supercortex.registry import db, experiments


@pytest.fixture
def sealed(tmp_path, monkeypatch):
    registry = tmp_path / "registry.sqlite"
    db.init(registry)
    monkeypatch.setattr(db, "DB_PATH", registry)
    monkeypatch.setattr(db, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(experiments, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(experiments, "EXPERIMENTS_DIR", tmp_path / "02-experiments")
    with db.connect(registry) as conn:
        conn.execute(
            "INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
            " VALUES ('EXP-000009','probe','probe','BACKTESTING',10,?)", (db.now(),))
        conn.execute(
            "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
            "cost_model_version,seed,started_at) VALUES"
            " ('EXP-000009',1,'CFG','SHA','SNAP','india-vda-1.0.0',42,?)", (db.now(),))
    path = experiments.close_trial("EXP-000009", 1, "ok", {"trades": {"n_trades": 3}})
    return tmp_path, path


def row(seq: int = 1):
    with db.connect() as conn:
        return conn.execute(
            "SELECT outcome, metrics_path, artifact_sha256 FROM trial"
            " WHERE experiment_id = 'EXP-000009' AND seq = ?", (seq,)).fetchone()


# ── the column exists and is migrated by code ───────────────────────────────────
def test_the_trial_row_carries_an_artifact_hash(sealed):
    assert row()["artifact_sha256"] is not None
    assert len(row()["artifact_sha256"]) == 64


def test_the_hash_covers_the_exact_bytes_written(sealed):
    """Not a re-serialized dict: the file on disk is the thing being authenticated."""
    _root, path = sealed
    assert row()["artifact_sha256"] == db.sha256_file(path)


def test_an_older_database_is_migrated_by_code(tmp_path):
    """A faithful older registry: `schema.sql` as it stood before `artifact_sha256`.

    This used to rename `trial` aside and hand-write a replacement with no NOT NULLs, no
    UNIQUE and no triggers, leaving `trial_old` behind. That is not a database this code
    ever produced, so migrating it proved nothing about migrating a real one — and the
    schema check now refuses it outright, correctly.
    """
    legacy = tmp_path / "legacy.sqlite"
    current = db.SCHEMA_PATH.read_text(encoding="utf-8")
    older = re.sub(r"\n\s*artifact_sha256\s+TEXT,", "", current)
    assert older != current, "the column is no longer where this test expects it"

    conn = sqlite3.connect(legacy)
    conn.executescript(older)
    conn.commit()
    conn.close()

    assert not db.schema_status(legacy).current
    db.init(legacy)                      # must add the column, not fail
    assert db.schema_status(legacy).current

    with db.connect(legacy) as conn:
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(trial)")]
    assert columns[-1] == "artifact_sha256"


def test_the_artifact_hash_cannot_be_changed_after_sealing(sealed):
    with db.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="already sealed"):
        conn.execute("UPDATE trial SET artifact_sha256 = ? WHERE seq = 1", ("f" * 64,))


def test_an_error_seal_records_no_artifact_hash(sealed):
    _root, _path = sealed
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
            "cost_model_version,seed,started_at) VALUES"
            " ('EXP-000009',2,'CFG','SHA','SNAP','india-vda-1.0.0',42,?)", (db.now(),))
    experiments.close_trial("EXP-000009", 2, "error")
    assert row(2)["outcome"] == "error"
    assert row(2)["artifact_sha256"] is None
    assert row(2)["metrics_path"] is None


def test_a_headline_copy_is_not_the_registered_artifact(sealed):
    """metrics.json is a convenience copy. The row points at the per-trial file."""
    _root, path = sealed
    assert path.name == "metrics-trial-0001.json"
    assert (path.parent / "metrics.json").exists()
    assert row()["metrics_path"].endswith("metrics-trial-0001.json")
