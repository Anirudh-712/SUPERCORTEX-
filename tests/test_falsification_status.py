"""A verdict must survive the round trip to the registry as the verdict it was.

`_record` stored only `int(v.passed)`, so SUSPICIOUS, INERT and UNDEFINED all collapsed to
`0` and were distinguishable afterwards only by the `statistic IS NULL` convention - which
is an inference, not a record. An INERT channel means "the probe was uninformative"; an
UNDEFINED one means "no comparison exists". Neither is a FAIL, and a lifecycle gate that
cannot tell them apart is reading a number that was never written down.

Two vocabularies, deliberately not merged: a battery test has two outcomes and a channel
verdict has four. One enum for both would let a `TestResult` claim INERT.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from supercortex.falsify.orchestrator import BatteryStatus, TestResult
from supercortex.falsify.probes import ChannelStatus, ChannelVerdict
from supercortex.registry import db, experiments


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.sqlite"
    db.init(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(experiments, "PROJECT_ROOT", tmp_path)
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
            " VALUES ('EXP-000009','probe','probe','FALSIFYING',10,?)", (db.now(),))
    return path


def row(test_name: str = "1-lookahead-bars"):
    with db.connect() as conn:
        return conn.execute(
            "SELECT status, passed, statistic, threshold, artifact_path, trials_at_run"
            " FROM falsification WHERE experiment_id = 'EXP-000009' AND test_name = ?",
            (test_name,)).fetchone()


# ══════════════════════════════════════════════════════════════════════════════
# the two domain vocabularies
# ══════════════════════════════════════════════════════════════════════════════
def test_battery_status_has_exactly_two_outcomes():
    """A test that cannot run is a FAIL. There is no third member to reach for."""
    assert {s.value for s in BatteryStatus} == {"PASS", "FAIL"}


def test_channel_status_has_exactly_four_outcomes():
    assert {s.value for s in ChannelStatus} == {"PASS", "SUSPICIOUS", "INERT",
                                                "UNDEFINED"}


def test_a_test_result_reports_its_status_as_the_domain_enum():
    passed = TestResult("3-cost-ladder", True, 1.0, 0.0, "survives")
    failed = TestResult("5-walk-forward", False, 1.0, 0.0, "cannot run, therefore FAIL")
    assert passed.status is BatteryStatus.PASS
    assert failed.status is BatteryStatus.FAIL


def test_a_battery_status_still_compares_as_its_string():
    """StrEnum, so existing renderers and `== "PASS"` keep working."""
    assert TestResult("x", True, None, None, "").status == "PASS"
    assert str(BatteryStatus.FAIL) == "FAIL"


def test_a_channel_verdict_carries_the_domain_enum():
    verdict = ChannelVerdict("bars", 0.0, 0.0, 0.0, ChannelStatus.INERT, "uninformative")
    assert verdict.status is ChannelStatus.INERT
    assert verdict.passed is False


def test_only_a_pass_channel_verdict_passes():
    for status in ChannelStatus:
        verdict = ChannelVerdict("bars", 0.0, 0.0, 0.0, status, "")
        assert verdict.passed is (status is ChannelStatus.PASS)


# ══════════════════════════════════════════════════════════════════════════════
# the persisted set, and the dependency direction
# ══════════════════════════════════════════════════════════════════════════════
def test_the_persisted_set_is_the_union_of_both_domains():
    """Drift guard. The registry declares its own accepted strings so it need not import
    the falsify package; this asserts the two never diverge."""
    union = {s.value for s in BatteryStatus} | {s.value for s in ChannelStatus}
    assert set(experiments.FALSIFICATION_STATUSES) == union


def test_the_registry_does_not_import_the_falsify_package():
    """registry -> falsify -> registry would be a cycle. The registry owns storage; the
    falsify package owns meaning."""
    for module in ("experiments.py", "db.py"):
        source = (Path("src/supercortex/registry") / module).read_text(encoding="utf-8")
        assert "supercortex.falsify" not in source, module
        assert "from supercortex import falsify" not in source, module


# ══════════════════════════════════════════════════════════════════════════════
# migration
# ══════════════════════════════════════════════════════════════════════════════
def test_a_fresh_schema_has_the_status_column(registry):
    with db.connect() as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(falsification)")}
    assert "status" in columns


def test_an_existing_database_gains_the_column_without_backfill(tmp_path):
    legacy = tmp_path / "legacy.sqlite"
    db.init(legacy)
    with db.connect(legacy) as conn:
        conn.execute("ALTER TABLE falsification RENAME TO falsification_old")
        conn.execute(
            "CREATE TABLE falsification (experiment_id TEXT NOT NULL, test_name TEXT"
            " NOT NULL, statistic REAL, threshold REAL, passed INTEGER NOT NULL,"
            " ran_at TEXT NOT NULL, artifact_path TEXT, trials_at_run INTEGER NOT NULL,"
            " PRIMARY KEY (experiment_id, test_name))")
        # one historical PASS and one historical non-pass, plus the ambiguous case
        conn.execute("INSERT INTO falsification VALUES ('E','t-pass',1.0,0.5,1,'t',NULL,3)")
        conn.execute("INSERT INTO falsification VALUES ('E','t-fail',0.1,0.5,0,'t',NULL,3)")
        conn.execute("INSERT INTO falsification VALUES ('E','t-null',NULL,0.5,0,'t',NULL,3)")

    db.init(legacy)

    with db.connect(legacy) as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(falsification)")}
        rows = {r["test_name"]: r for r in
                conn.execute("SELECT test_name, status, passed, statistic"
                             " FROM falsification")}
    assert "status" in columns
    assert len(rows) == 3
    for name, record in rows.items():
        assert record["status"] is None, f"{name} was backfilled"


def test_a_legacy_pass_is_not_promoted_to_a_status(tmp_path):
    """`passed = 1` is not proof the verdict was PASS in the vocabulary that now exists."""
    legacy = tmp_path / "legacy.sqlite"
    db.init(legacy)
    with db.connect(legacy) as conn:
        conn.execute("INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
                     " VALUES ('E','e','e','FALSIFYING',1,'t')")
        conn.execute("INSERT INTO falsification(experiment_id,test_name,statistic,"
                     "threshold,passed,ran_at,trials_at_run)"
                     " VALUES ('E','t',1.0,0.5,1,'t',3)")
    db.init(legacy)
    with db.connect(legacy) as conn:
        assert conn.execute("SELECT status FROM falsification").fetchone()["status"] is None


def test_a_null_statistic_never_becomes_undefined(tmp_path):
    """`statistic IS NULL` is ambiguous - a real test can legitimately have no statistic."""
    legacy = tmp_path / "legacy.sqlite"
    db.init(legacy)
    with db.connect(legacy) as conn:
        conn.execute("INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
                     " VALUES ('E','e','e','FALSIFYING',1,'t')")
        conn.execute("INSERT INTO falsification(experiment_id,test_name,statistic,"
                     "threshold,passed,ran_at,trials_at_run)"
                     " VALUES ('E','t',NULL,0.5,0,'t',3)")
    db.init(legacy)
    with db.connect(legacy) as conn:
        assert conn.execute("SELECT status FROM falsification").fetchone()["status"] is None


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "repeat.sqlite"
    for _ in range(3):
        db.init(path)
    with db.connect(path) as conn:
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(falsification)")]
    assert columns.count("status") == 1


def test_a_fresh_and_a_migrated_schema_agree(tmp_path):
    """Observably identical, column for column and in order.

    A set comparison hid a real difference: SQLite's ALTER TABLE can only APPEND, so a
    migrated database has `status` last while a fresh one had it mid-table. Same columns,
    different layout - and anything reading by position, or a schema diff, would disagree
    about two databases that are supposed to be the same.
    """
    fresh = tmp_path / "fresh.sqlite"
    db.init(fresh)
    migrated = tmp_path / "migrated.sqlite"
    db.init(migrated)
    with db.connect(migrated) as conn:
        conn.execute("ALTER TABLE falsification DROP COLUMN status")
    db.init(migrated)

    def shape(path: Path) -> list[tuple[int, str, str, int, object, int]]:
        with db.connect(path) as conn:
            return [(r["cid"], r["name"], r["type"], r["notnull"], r["dflt_value"],
                     r["pk"])
                    for r in conn.execute("PRAGMA table_info(falsification)")]

    assert shape(fresh) == shape(migrated)


def test_the_status_column_is_last_in_a_fresh_schema(tmp_path):
    """The layout a migration can produce. Stated so the fresh schema is written to match
    it rather than the two drifting apart again."""
    fresh = tmp_path / "fresh.sqlite"
    db.init(fresh)
    with db.connect(fresh) as conn:
        names = [r["name"] for r in conn.execute("PRAGMA table_info(falsification)")]
    assert names[-1] == "status", names


def test_legacy_unclassified_is_named_not_guessed():
    assert experiments.LEGACY_UNCLASSIFIED is None


# ══════════════════════════════════════════════════════════════════════════════
# the sanctioned writer
# ══════════════════════════════════════════════════════════════════════════════
def _write(status, **over):
    payload = dict(experiment_id="EXP-000009", test_name="1-lookahead-bars",
                   status=status, statistic=0.25, threshold=0.10,
                   artifact_path=None, trials_at_run=4)
    payload.update(over)
    experiments.record_falsification(**payload)


@pytest.mark.parametrize("status", list(ChannelStatus))
def test_every_channel_status_round_trips_exactly(registry, status):
    _write(status)
    assert row()["status"] == status.value


@pytest.mark.parametrize("status", list(BatteryStatus))
def test_every_battery_status_round_trips_exactly(registry, status):
    _write(status)
    assert row()["status"] == status.value


def test_pass_derives_passed_one(registry):
    _write(ChannelStatus.PASS)
    assert row()["passed"] == 1


@pytest.mark.parametrize("status", [s for s in ChannelStatus if s is not ChannelStatus.PASS])
def test_every_other_status_derives_passed_zero(registry, status):
    _write(status)
    assert row()["passed"] == 0


def test_passed_is_derived_not_accepted(registry):
    """The caller does not get to supply truth alongside the status."""
    import inspect

    signature = inspect.signature(experiments.record_falsification)
    assert "passed" not in signature.parameters


@pytest.mark.parametrize("status", ["pass", "Passed", "OK", "SKIPPED", "", "42"])
def test_an_unknown_status_is_rejected(registry, status):
    with pytest.raises(ValueError, match="status"):
        _write(status)
    assert row() is None


def test_a_null_status_is_rejected_for_a_new_write(registry):
    with pytest.raises(ValueError, match="status"):
        _write(None)
    assert row() is None


def test_a_string_status_from_the_persisted_set_is_accepted(registry):
    _write("SUSPICIOUS")
    assert row()["status"] == "SUSPICIOUS"


def test_a_rerun_replaces_every_mutable_field(registry):
    _write(ChannelStatus.PASS, statistic=0.9, threshold=0.1,
           artifact_path="02-experiments/old.json", trials_at_run=4)
    _write(ChannelStatus.SUSPICIOUS, statistic=0.01, threshold=0.2,
           artifact_path="02-experiments/new.json", trials_at_run=9)

    record = row()
    assert record["status"] == "SUSPICIOUS"
    assert record["passed"] == 0
    assert record["statistic"] == pytest.approx(0.01)
    assert record["threshold"] == pytest.approx(0.2), "a stale threshold survived a rerun"
    assert record["artifact_path"] == "02-experiments/new.json"
    assert record["trials_at_run"] == 9


def test_a_rerun_does_not_duplicate_the_row(registry):
    _write(ChannelStatus.PASS)
    _write(ChannelStatus.INERT)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM falsification").fetchone()["n"] == 1


def test_a_write_for_an_unknown_experiment_is_refused(registry):
    with pytest.raises(sqlite3.IntegrityError):
        _write(ChannelStatus.PASS, experiment_id="EXP-999999")


def test_every_stored_row_is_internally_consistent(registry):
    for index, status in enumerate(ChannelStatus):
        _write(status, test_name=f"1-lookahead-{index}")
    with db.connect() as conn:
        rows = conn.execute("SELECT status, passed FROM falsification").fetchall()
    assert len(rows) == len(ChannelStatus)
    for record in rows:
        assert record["passed"] == int(record["status"] == "PASS")
