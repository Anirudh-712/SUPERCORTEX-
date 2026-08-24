"""The registry's guarantees, as executable claims.

These tests are load-bearing. If any of them fails, every significance test the desk
produces downstream is unsound, because the trial count feeding the Deflated Sharpe
Ratio can no longer be trusted.
"""
from __future__ import annotations

import sqlite3

import pytest

from supercortex.registry import db


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "registry.sqlite"
    db.init(path)
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
            " VALUES ('EXP-000001','probe','probe','RESEARCH',10,?)",
            (db.now(),),
        )
        yield conn


def _insert_trial(conn: sqlite3.Connection, seq: int) -> None:
    conn.execute(
        "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
        "cost_model_version,seed,started_at) VALUES ('EXP-000001',?,'cfg','abc','snap','v1',1,?)",
        (seq, db.now()),
    )


# ── the append-only guarantee ───────────────────────────────────────────────────
def test_trial_cannot_be_deleted(registry):
    _insert_trial(registry, 1)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        registry.execute("DELETE FROM trial WHERE seq = 1")


def test_sealed_trial_cannot_be_modified(registry):
    _insert_trial(registry, 1)
    registry.execute("UPDATE trial SET finished_at = ?, outcome = 'ok' WHERE seq = 1", (db.now(),))
    with pytest.raises(sqlite3.IntegrityError, match="already sealed"):
        registry.execute("UPDATE trial SET outcome = 'error' WHERE seq = 1")


def test_trial_identity_is_immutable(registry):
    _insert_trial(registry, 1)
    with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
        registry.execute("UPDATE trial SET config_hash = 'tampered' WHERE seq = 1")


def test_open_trial_may_be_sealed_once(registry):
    _insert_trial(registry, 1)
    registry.execute("UPDATE trial SET finished_at = ?, outcome = 'ok' WHERE seq = 1", (db.now(),))
    row = registry.execute("SELECT outcome FROM trial WHERE seq = 1").fetchone()
    assert row["outcome"] == "ok"


# ── the count that feeds the Deflated Sharpe Ratio ──────────────────────────────
def test_abandoned_and_errored_trials_still_count(registry):
    """The whole design rests on this: a look at the data counts, however it ended."""
    for seq, outcome in enumerate(["ok", "error", "abandoned", None], start=1):
        _insert_trial(registry, seq)
        if outcome:
            registry.execute(
                "UPDATE trial SET finished_at = ?, outcome = ? WHERE seq = ?",
                (db.now(), outcome, seq),
            )
    row = registry.execute("SELECT * FROM experiment_trials WHERE id = 'EXP-000001'").fetchone()
    assert row["trials_consumed"] == 4
    assert row["trials_ok"] == 1
    assert row["trials_abandoned"] == 1
    assert row["trials_open"] == 1


def test_over_budget_is_flagged_not_blocked(registry):
    """Overruns are allowed — and recorded, so the multiple-testing penalty grows."""
    for seq in range(1, 12):
        _insert_trial(registry, seq)
    row = registry.execute("SELECT * FROM experiment_trials WHERE id = 'EXP-000001'").fetchone()
    assert row["trials_consumed"] == 11 > row["trial_budget"]
    assert row["over_budget"] == 1


def test_duplicate_sequence_rejected(registry):
    _insert_trial(registry, 1)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_trial(registry, 1)


def test_trial_requires_known_experiment(registry):
    with pytest.raises(sqlite3.IntegrityError):
        registry.execute(
            "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
            "cost_model_version,seed,started_at) VALUES ('EXP-999999',1,'c','g','d','v',1,?)",
            (db.now(),),
        )


# ── identity ────────────────────────────────────────────────────────────────────
def test_ids_are_monotonic_and_zero_padded(registry):
    assert db.allocate_id(registry, "EXP") == "EXP-000001"
    assert db.allocate_id(registry, "EXP") == "EXP-000002"
    assert db.allocate_id(registry, "DEC") == "DEC-000001"


def test_unknown_prefix_rejected(registry):
    with pytest.raises(ValueError, match="unknown prefix"):
        db.allocate_id(registry, "NOPE")


def test_invalid_outcome_rejected(registry):
    _insert_trial(registry, 1)
    with pytest.raises(sqlite3.IntegrityError):
        registry.execute(
            "UPDATE trial SET finished_at=?, outcome='great' WHERE seq=1", (db.now(),)
        )


def test_invalid_status_rejected(registry):
    with pytest.raises(sqlite3.IntegrityError):
        registry.execute(
            "INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
            " VALUES ('EXP-000002','x','x','PROFITABLE',1,?)",
            (db.now(),),
        )


# ── timestamps must be comparable to SQLite's own clock ─────────────────────────
def test_timestamps_compare_against_sqlite_now(registry):
    """Regression: ISO 'T' + '+00:00' never sorted against datetime('now'),
    which silently made trial reaping dead code."""
    _insert_trial(registry, 1)
    row = registry.execute(
        "SELECT started_at <= datetime('now')            AS not_future,"
        "       started_at >  datetime('now','-1 hour')  AS not_ancient,"
        "       started_at <  datetime('now','+1 hour')  AS orders_forward"
        " FROM trial WHERE seq = 1"
    ).fetchone()
    # With the old 'T'/'+00:00' format every one of these was 0 or 1 regardless of the
    # real instant, because the comparison was lexical nonsense rather than temporal.
    assert (row["not_future"], row["not_ancient"], row["orders_forward"]) == (1, 1, 1)


def test_reap_marks_old_open_trials_abandoned(registry, monkeypatch, tmp_path):
    from supercortex.registry import experiments

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "registry.sqlite")
    registry.execute(
        "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
        "cost_model_version,seed,started_at) VALUES ('EXP-000001',1,'c','g','d','v',1,"
        "datetime('now','-9 hours'))"
    )
    _insert_trial(registry, 2)  # fresh, must survive
    reaped = experiments.reap_stale(6.0)
    assert reaped == [("EXP-000001", 1)]
    outcomes = dict(
        registry.execute("SELECT seq, outcome FROM trial ORDER BY seq").fetchall()
    )
    assert outcomes[1] == "abandoned"
    assert outcomes[2] is None


def test_reaped_trials_still_count_toward_the_total(registry, monkeypatch, tmp_path):
    from supercortex.registry import experiments

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "registry.sqlite")
    registry.execute(
        "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
        "cost_model_version,seed,started_at) VALUES ('EXP-000001',1,'c','g','d','v',1,"
        "datetime('now','-9 hours'))"
    )
    experiments.reap_stale(6.0)
    row = registry.execute(
        "SELECT * FROM experiment_trials WHERE id = 'EXP-000001'"
    ).fetchone()
    assert row["trials_consumed"] == 1
    assert row["trials_abandoned"] == 1
