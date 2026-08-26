"""The registry's guarantees, as executable claims.

These tests are load-bearing. If any of them fails, every significance test the desk
produces downstream is unsound, because the trial count feeding the Deflated Sharpe
Ratio can no longer be trusted.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

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


def test_dirty_tree_message_names_real_files(tmp_path, monkeypatch):
    """Regression: `run()` strips the whole porcelain output, removing the leading
    status space from the first line only, so a fixed-width line[3:] slice ate a
    character off exactly one filename. An error naming a file that does not exist
    sends you looking in the wrong place."""
    import subprocess

    calls = {}

    def fake_run(args, **kwargs):
        sub = args[1]
        if sub == "rev-parse":
            out = "abc123\n"
        else:
            # exactly what `git status --porcelain` emits, before stripping
            out = " M src/supercortex/cli.py\n?? src/new_module.py\n"
        calls[sub] = True
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        db.git_commit(strict=True)
    message = str(exc.value)
    # Assert the exact rendered list. A substring check is useless here: the old buggy
    # output "rc/supercortex/cli.py" is itself a substring of the correct path, so
    # `"rc/supercortex" not in message` passes even when the bug is present.
    listed = message.split("inputs: ", 1)[1].split(". A trial", 1)[0]
    assert listed == "src/supercortex/cli.py, src/new_module.py"


# ── an experiment with no trials has no open trials ─────────────────────────────
# `experiment_trials` LEFT JOINs `trial`, so an experiment with no rows still produces one
# synthetic joined row whose `t.outcome` is NULL. Counting that as an unsealed trial told
# the operator to reap something that never existed.

def test_experiment_with_no_trials_reports_no_open_trials(tmp_path: Path) -> None:
    registry = db.init(tmp_path / "registry.sqlite")
    with sqlite3.connect(registry) as conn:
        conn.execute(
            "INSERT INTO experiment (id, title, slug, status, trial_budget, market, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            ("EXP-000001", "No trials yet", "none", "RESEARCH", 10, "crypto",
             "2026-01-01T00:00:00Z"),
        )
    with sqlite3.connect(registry) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM experiment_trials").fetchone()

    assert row["trials_consumed"] == 0
    assert row["trials_open"] == 0, (
        "the LEFT JOIN's synthetic NULL row is being counted as an unsealed trial"
    )


def test_open_and_sealed_trials_are_still_counted(tmp_path: Path) -> None:
    """The fix must not stop counting genuinely unsealed trials."""
    registry = db.init(tmp_path / "registry.sqlite")
    with sqlite3.connect(registry) as conn:
        conn.execute(
            "INSERT INTO experiment (id, title, slug, status, trial_budget, market, "
            "created_at) VALUES ('EXP-000002','Two','t','RESEARCH',10,'crypto',"
            "'2026-01-01T00:00:00Z')")
        for seq, finished, outcome in ((1, "2026-01-01T01:00:00Z", "ok"), (2, None, None)):
            conn.execute(
                "INSERT INTO trial (experiment_id, seq, config_hash, code_commit, "
                "data_snapshot, cost_model_version, seed, started_at, finished_at, "
                "outcome) VALUES ('EXP-000002',?,?,?,?,?,?,?,?,?)",
                (seq, "c" * 8, "d" * 7, "e" * 8, "v1", 1, "2026-01-01T00:30:00Z",
                 finished, outcome),
            )
    with sqlite3.connect(registry) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM experiment_trials").fetchone()
    assert row["trials_consumed"] == 2
    assert row["trials_open"] == 1
    assert row["trials_ok"] == 1


# ── the superseded-view migration contract ──────────────────────────────────────
# `CREATE VIEW IF NOT EXISTS` never replaces an existing view, so correcting a view in
# `schema.sql` leaves every existing registry holding the old text. Without a contract that
# is reported as incompatible and `init` cannot repair it — a dead end. Recording the exact
# superseded definition makes this one upgrade repairable while leaving genuine drift
# fail-closed.

OLD_VIEW = """
CREATE VIEW experiment_trials AS
SELECT e.id, e.title, e.status, e.trial_budget,
       COUNT(t.id) AS trials_consumed,
       SUM(CASE WHEN t.outcome = 'ok' THEN 1 ELSE 0 END) AS trials_ok,
       SUM(CASE WHEN t.outcome = 'abandoned' THEN 1 ELSE 0 END) AS trials_abandoned,
       SUM(CASE WHEN t.outcome IS NULL THEN 1 ELSE 0 END) AS trials_open,
       CASE WHEN COUNT(t.id) > e.trial_budget THEN 1 ELSE 0 END AS over_budget
FROM experiment e
LEFT JOIN trial t ON t.experiment_id = e.id
GROUP BY e.id
"""


def _downgrade_view(registry: Path, sql: str = OLD_VIEW) -> None:
    with sqlite3.connect(registry) as conn:
        conn.execute("DROP VIEW experiment_trials")
        conn.execute(sql)


def _view_sql(registry: Path) -> str:
    with sqlite3.connect(registry) as conn:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='experiment_trials'").fetchone()[0]


def test_a_fresh_registry_is_current(tmp_path: Path) -> None:
    registry = db.init(tmp_path / "fresh.sqlite")
    assert db.schema_status(registry).current


def test_the_old_view_is_repairable_not_incompatible(tmp_path: Path) -> None:
    registry = db.init(tmp_path / "old.sqlite")
    _downgrade_view(registry)
    status = db.schema_status(registry)

    assert not status.current
    assert status.repairable, "the superseded view must be repairable by `init`"
    assert not status.incompatible
    assert "experiment_trials" in status.stale_views
    assert "experiment_trials" in status.summary()


def test_init_rebuilds_the_superseded_view(tmp_path: Path) -> None:
    registry = db.init(tmp_path / "old.sqlite")
    with sqlite3.connect(registry) as conn:
        conn.execute(
            "INSERT INTO experiment (id, title, slug, status, trial_budget, market, "
            "created_at) VALUES ('EXP-000001','Kept','k','RESEARCH',9,'crypto',"
            "'2026-01-01T00:00:00Z')")
    _downgrade_view(registry)

    db.init(registry)

    assert db.schema_status(registry).current
    with sqlite3.connect(registry) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM experiment_trials").fetchone()
    assert row["trials_open"] == 0                      # the bug the rebuild fixes
    assert row["id"] == "EXP-000001"                    # data untouched
    assert row["trial_budget"] == 9


def test_fresh_and_migrated_views_are_identical(tmp_path: Path) -> None:
    """A migrated database must be indistinguishable from one created today."""
    fresh = db.init(tmp_path / "fresh.sqlite")
    migrated = db.init(tmp_path / "migrated.sqlite")
    _downgrade_view(migrated)
    db.init(migrated)
    assert db._normalise(_view_sql(migrated)) == db._normalise(_view_sql(fresh))


def test_rebuilding_is_idempotent(tmp_path: Path) -> None:
    registry = db.init(tmp_path / "r.sqlite")
    before = _view_sql(registry)
    db.init(registry)
    db.init(registry)
    assert _view_sql(registry) == before


def test_unrelated_view_drift_stays_incompatible(tmp_path: Path) -> None:
    """A hand-edited view is a statement someone made deliberately; no migration may
    silently discard it."""
    registry = db.init(tmp_path / "drift.sqlite")
    _downgrade_view(registry, """
        CREATE VIEW experiment_trials AS
        SELECT e.id, e.title, e.status, e.trial_budget,
               0 AS trials_consumed, 0 AS trials_ok, 0 AS trials_abandoned,
               0 AS trials_open, 0 AS over_budget
        FROM experiment e
    """)
    status = db.schema_status(registry)

    assert not status.current
    assert not status.repairable
    assert status.incompatible
    assert "experiment_trials" not in status.stale_views


def test_unrelated_drift_is_not_silently_rebuilt(tmp_path: Path) -> None:
    registry = db.init(tmp_path / "drift.sqlite")
    hand_written = """
        CREATE VIEW experiment_trials AS
        SELECT e.id, e.title, e.status, e.trial_budget,
               0 AS trials_consumed, 0 AS trials_ok, 0 AS trials_abandoned,
               0 AS trials_open, 0 AS over_budget
        FROM experiment e
    """
    _downgrade_view(registry, hand_written)
    db.init(registry)
    assert db._normalise(_view_sql(registry)) == db._normalise(hand_written)
