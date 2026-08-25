"""C3-pre-A — the registry schema is either current or nothing runs against it.

Three defects motivate this file, and all three were live when it was written.

**The fresh schema and a migrated one disagreed.** `ALTER TABLE` in SQLite can only
append, so a column declared mid-table in `schema.sql` lands at the END of a database that
reached it by migration. `trial.artifact_sha256` and `dataset.kind`/`parquet_path` were
declared mid-table, so two databases with the same columns had them in different positions.
`falsification.status` already carried the fix; these did not.

**The live registry was behind the code.** `registry.sqlite` had neither
`trial.artifact_sha256` nor `falsification.status`, because migrations run only inside
`db.init()` and nothing had called it since those columns were added. Sealing a trial,
authenticating an artifact and recording a falsification verdict would all have raised
`no such column` — against a green test suite, because every test builds its own registry
with `db.init` and is therefore always current.

**Nothing checked.** A stale registry announced itself as a traceback from inside SQLite,
at whatever moment a command first touched the missing column.

So: the fresh schema is the specification, `schema_status` reads a database without
touching it, and `db.connect` refuses a stale one before any statement runs.

Nothing here mutates `registry.sqlite`. The migration tests operate on copies, and each one
re-checks the original's digest afterwards.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from supercortex import cli as root_cli
from supercortex.registry import db, experiments

runner = CliRunner()

#: Every column PRAGMA reports, in the order it reports them. Comparing names alone — or
#: comparing sets — is what hid the ordering defect for three slices.
PRAGMA_COLUMNS = ("cid", "name", "type", "notnull", "dflt_value", "pk")

MIGRATED_TABLES = ("trial", "dataset", "falsification")


def shape(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(row[c] for c in PRAGMA_COLUMNS)
            for row in conn.execute(f"PRAGMA table_info({table})")]


def objects(conn: sqlite3.Connection, kind: str, table: str) -> list[tuple[str, str]]:
    """Indexes or triggers attached to one table, with their SQL, ordered by name."""
    return sorted(
        (r[0], " ".join((r[1] or "").split()))
        for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = ? AND tbl_name = ?"
            " AND name NOT LIKE 'sqlite_%'", (kind, table))
    )


def contents(path: Path) -> dict[str, int]:
    """Row counts per table, read-only.

    Comparing the database FILE's bytes proves nothing: `schema.sql` sets
    `journal_mode = WAL`, so a committed write lands in the `-wal` sidecar and the main
    file is untouched. An earlier version of these tests asserted on those bytes and
    passed while a row was being inserted.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        conn.close()


def full_schema(path: Path) -> dict:
    """Everything the runtime contract covers, read from a database. Ordered throughout."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out: dict = {"objects": {}, "tables": {}}
        for name, kind, sql in conn.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                " ORDER BY name"):
            out["objects"][name] = (kind, " ".join((sql or "").split()))
            if kind in ("table", "view"):
                out["tables"][name] = (
                    [tuple(r) for r in conn.execute(f"PRAGMA table_info({name})")],
                    sorted(tuple(r) for r in conn.execute(f"PRAGMA index_list({name})")),
                )
        return out
    finally:
        conn.close()


def build(path: Path, transform=lambda sql: sql, after: str | None = None) -> Path:
    """A database built from a possibly-altered `schema.sql`. The damage is in the DDL,
    because SQLite cannot retype or reorder a column after the fact."""
    conn = sqlite3.connect(path)
    conn.executescript(transform(db.SCHEMA_PATH.read_text(encoding="utf-8")))
    if after:
        conn.executescript(after)
    conn.commit()
    conn.close()
    return path


def rowdict(conn: sqlite3.Connection, table: str, order: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]


def _drop_dangling_comma(body: str) -> str:
    """Remove the comma left behind when the last column of a block is stripped.

    Scans past trailing blank and comment lines rather than anchoring on the end of the
    string: `dataset` now ends with its migrated columns under an explanatory comment, so
    the comma to remove is not on the last line.
    """
    lines = body.split("\n")
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("--"):
            continue
        lines[index] = lines[index].rstrip().removesuffix(",")
        break
    return "\n".join(lines)


def legacy_schema() -> str:
    """`schema.sql` with every migrated column stripped from its CREATE TABLE block.

    A faithful stand-in for a database created before those columns existed. Scoped to one
    table's block at a time — a blunt line-wise strip also removed `experiment.status`,
    which shares a name with the falsification column and is not migrated.
    """
    sql = db.SCHEMA_PATH.read_text(encoding="utf-8")
    for table, column, _decl in db.MIGRATIONS:
        block = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql, re.S)
        assert block is not None, f"cannot locate the {table} CREATE block"
        body = re.sub(rf"\n\s*{column}\s+\w+[^\n]*", "", block.group(1))
        sql = sql[:block.start(1)] + _drop_dangling_comma(body) + sql[block.end(1):]
    return sql


@pytest.fixture
def fresh(tmp_path) -> Path:
    return db.init(tmp_path / "fresh.sqlite")


@pytest.fixture(autouse=True)
def _isolated_experiments(tmp_path, monkeypatch):
    """No test here may write into the real `02-experiments`.

    `experiments.register` scaffolds a directory as its second act, so a regression in the
    guard would leave an experiment folder in the working tree. It did, once, under a
    deliberately ablated guard — which is exactly the moment isolation has to hold.
    """
    monkeypatch.setattr(experiments, "EXPERIMENTS_DIR", tmp_path / "02-experiments")


@pytest.fixture
def stale(tmp_path) -> Path:
    """A database on the pre-migration schema. Never migrated."""
    path = tmp_path / "stale.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(legacy_schema())
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def migrated(tmp_path, stale) -> Path:
    path = tmp_path / "migrated.sqlite"
    shutil.copy2(stale, path)
    return db.init(path)


# ── 1. the fresh schema declares migrated columns last, in migration order ──────
def test_migrated_columns_are_declared_last_in_migration_order(fresh):
    """SQLite can only append, so a migrated database has these columns at the end. The
    fresh declaration must match that or the two shapes differ for no reason a reader of
    either file could see."""
    expected: dict[str, list[str]] = {}
    for table, column, _decl in db.MIGRATIONS:
        expected.setdefault(table, []).append(column)

    with db.connect(fresh) as conn:
        for table, columns in expected.items():
            names = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
            assert names[-len(columns):] == columns, (
                f"{table}: migrated columns must be last and in MIGRATIONS order; "
                f"declared order is {names}")


# ── 2. fresh and migrated agree exactly ────────────────────────────────────────
@pytest.mark.parametrize("table", MIGRATED_TABLES)
def test_a_fresh_and_a_migrated_schema_agree_column_for_column(fresh, migrated, table):
    with db.connect(fresh) as a, db.connect(migrated) as b:
        assert shape(a, table) == shape(b, table)


@pytest.mark.parametrize("table", MIGRATED_TABLES)
def test_a_fresh_and_a_migrated_schema_agree_on_indexes_and_triggers(fresh, migrated,
                                                                     table):
    """Columns are not the whole schema. An append-only guarantee that survives migration
    only by luck is not a guarantee."""
    with db.connect(fresh) as a, db.connect(migrated) as b:
        assert objects(a, "index", table) == objects(b, "index", table)
        assert objects(a, "trigger", table) == objects(b, "trigger", table)


def test_every_table_and_view_agrees_between_fresh_and_migrated(fresh, migrated):
    """Not only the three tables that carry migrations — the whole database."""
    with db.connect(fresh) as a, db.connect(migrated) as b:
        names = [r["name"] for r in a.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        assert names, "the fresh schema defines no tables"
        for name in names:
            assert shape(a, name) == shape(b, name), name


# ── 3. schema_status: read-only, total, and never creates anything ─────────────
def test_a_current_database_reports_no_missing_items(fresh):
    status = db.schema_status(fresh)
    assert status.current
    assert status.exists
    assert status.missing == ()


def test_a_pre_migration_database_names_every_missing_column(stale):
    status = db.schema_status(stale)
    assert not status.current
    assert set(status.missing_columns) == {
        (table, column) for table, column, _ in db.MIGRATIONS}
    # every one of them is named in the human-readable summary, not merely counted
    for table, column, _ in db.MIGRATIONS:
        assert f"{table}.{column}" in status.summary()


def test_checking_a_missing_database_does_not_create_it(tmp_path):
    """`sqlite3.connect` creates the file it is pointed at. A status check that did that
    would answer its own question and leave an empty registry behind."""
    absent = tmp_path / "nothing-here.sqlite"
    status = db.schema_status(absent)
    assert not status.exists
    assert not status.current
    assert not absent.exists()
    assert "supercortex init" in status.summary()


def test_a_missing_table_is_reported(tmp_path):
    path = tmp_path / "partial.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE experiment (id TEXT PRIMARY KEY);")
    conn.commit()
    conn.close()
    status = db.schema_status(path)
    assert "trial" in status.missing_tables
    assert "falsification" in status.missing_tables
    assert not status.current


def test_the_status_check_does_not_modify_the_database(stale):
    """The database itself is untouched: same bytes, same rows, no schema change.

    Not asserted: the absence of a `-shm` file. `schema.sql` sets `journal_mode = WAL`,
    and SQLite builds its shared-memory index on ANY read of a WAL database, including a
    `mode=ro` one. That is a working file, not a content change. The alternative —
    opening with `immutable=1`, which creates nothing — makes SQLite ignore an
    uncheckpointed WAL, so a check run while another process held the database open could
    report a schema that is no longer there. A function whose only job is to give the
    right answer does not trade correctness for a tidy directory.
    """
    before = (stale.read_bytes(), contents(stale))
    db.schema_status(stale)
    db.schema_status(stale)
    assert (stale.read_bytes(), contents(stale)) == before


def test_the_required_shape_is_the_fresh_schema_not_a_second_list(fresh):
    """The requirement is derived from `schema.sql`, so it cannot drift from it. A
    hand-maintained list of expected columns is a second source of truth that goes stale
    silently — which is the exact failure this slice exists to fix."""
    required = db.required_shape()
    with db.connect(fresh) as conn:
        for table, columns in required.items():
            assert [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")] \
                == list(columns)


# ── 4. fail closed ─────────────────────────────────────────────────────────────
def test_connect_refuses_a_stale_database(stale):
    with pytest.raises(db.SchemaOutOfDate) as caught, db.connect(stale):
        pass
    message = str(caught.value)
    for table, column, _ in db.MIGRATIONS:
        assert f"{table}.{column}" in message
    assert db.SCHEMA_RECOVERY_COMMAND in message


def test_a_refused_connection_runs_no_statement(stale):
    """The refusal happens before the connection is usable, so nothing is read and
    nothing is written."""
    before = contents(stale)
    with pytest.raises(db.SchemaOutOfDate), db.connect(stale) as conn:
        conn.execute("INSERT INTO experiment(id,title,slug,status,trial_budget,"
                     "created_at) VALUES ('EXP-000001','x','x','RESEARCH',1,'t')")
    assert contents(stale) == before


def test_init_may_connect_to_a_stale_database(stale):
    """`init` is the sanctioned repair path; it cannot be blocked by the condition it
    exists to fix."""
    db.init(stale)
    assert db.schema_status(stale).current


def test_ordinary_reads_never_migrate(stale):
    """A read that quietly repaired the schema would make the failure invisible again."""
    with pytest.raises(db.SchemaOutOfDate), db.connect(stale):
        pass
    assert not db.schema_status(stale).current


def test_a_registry_command_exits_cleanly_against_a_stale_registry(stale, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", stale)
    result = runner.invoke(root_cli.app, ["exp", "list"])
    assert result.exit_code == 1
    assert not isinstance(result.exception, sqlite3.OperationalError)
    assert "Traceback" not in result.output
    for table, column, _ in db.MIGRATIONS:
        assert f"{table}.{column}" in result.output
    assert db.SCHEMA_RECOVERY_COMMAND in result.output


def test_a_refused_command_writes_nothing(stale, monkeypatch, tmp_path):
    """`exp register` allocates an id, inserts a row and scaffolds a directory. A refusal
    must leave all three undone — checked by counting rows and looking for the folder, not
    by comparing file bytes."""
    monkeypatch.setattr(db, "DB_PATH", stale)
    before = contents(stale)
    result = runner.invoke(root_cli.app, ["exp", "register", "probe", "--budget", "5"])
    assert result.exit_code == 1
    assert contents(stale) == before
    assert not (tmp_path / "02-experiments").exists()


def test_help_and_version_work_against_a_stale_registry(stale, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", stale)
    for argv in (["--help"], ["--version"], ["exp", "--help"], ["falsify", "static",
                                                                "--help"]):
        result = runner.invoke(root_cli.app, argv)
        assert result.exit_code == 0, f"{argv} -> {result.output}"


def test_doctor_reports_a_red_schema_row_against_a_stale_registry(stale, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", stale)
    result = runner.invoke(root_cli.app, ["doctor"])
    assert result.exit_code != 0
    said = " ".join(result.output.split())
    assert "schema" in said
    for table, column, _ in db.MIGRATIONS:
        assert f"{table}.{column}" in said


def test_doctor_passes_the_schema_row_against_a_current_registry(fresh, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", fresh)
    result = runner.invoke(root_cli.app, ["doctor"])
    said = " ".join(result.output.split())
    assert "schema current" in said
    assert "🔴" not in said.split("schema current")[1][:40]


# ── 5. migration behaviour, on copies only ────────────────────────────────────
def test_the_live_registry_shape_is_detected_as_stale():
    """The database this desk actually runs on. Read-only: only its status is asked for."""
    if not db.DB_PATH.exists():
        pytest.skip("no live registry in this checkout")
    before = db.DB_PATH.read_bytes()
    status = db.schema_status(db.DB_PATH)
    assert db.DB_PATH.read_bytes() == before, "checking the live registry modified it"
    assert isinstance(status.current, bool)


def _live_copy(tmp_path: Path) -> tuple[Path, bytes]:
    original = db.DB_PATH.read_bytes()
    target = tmp_path / "copy.sqlite"
    target.write_bytes(original)
    return target, original


def test_init_migrates_a_copy_of_the_live_registry_and_preserves_its_rows(tmp_path):
    if not db.DB_PATH.exists():
        pytest.skip("no live registry in this checkout")
    copy, original = _live_copy(tmp_path)

    with sqlite3.connect(copy) as conn:
        conn.row_factory = sqlite3.Row
        before_exp = rowdict(conn, "experiment", "id")
        before_trial = rowdict(conn, "trial", "experiment_id, seq")
    assert before_trial, "the live registry has no trials to preserve"

    db.init(copy)
    assert db.schema_status(copy).current

    with sqlite3.connect(copy) as conn:
        conn.row_factory = sqlite3.Row
        after_exp = rowdict(conn, "experiment", "id")
        after_trial = rowdict(conn, "trial", "experiment_id, seq")

    assert after_exp == before_exp
    assert len(after_trial) == len(before_trial)
    for old, new in zip(before_trial, after_trial, strict=True):
        # every pre-existing field byte-for-byte; the added ones NULL, never invented
        assert {k: v for k, v in new.items() if k in old} == old
        for _table, column, _ in db.MIGRATIONS:
            if column in new and column not in old:
                assert new[column] is None

    assert db.DB_PATH.read_bytes() == original, "the live registry was modified"


def test_init_is_idempotent(stale):
    """Repeated `init` changes neither the schema nor the data.

    Asserted on ordered schema metadata and row contents, not on the database file's
    bytes: under WAL a commit lands in the sidecar, so byte equality is satisfied by a
    write that did happen and violated by a checkpoint that changed nothing.
    """
    db.init(stale)
    first_schema, first_rows = full_schema(stale), contents(stale)
    assert db.schema_status(stale).current

    db.init(stale)
    db.init(stale)

    assert full_schema(stale) == first_schema
    assert contents(stale) == first_rows
    assert db.schema_status(stale).current


def test_append_only_triggers_still_reject_after_migration(migrated):
    with db.connect(migrated) as conn:
        conn.execute("INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
                     " VALUES ('EXP-000001','probe','probe','RESEARCH',1,?)", (db.now(),))
        conn.execute("INSERT INTO trial(experiment_id,seq,config_hash,code_commit,"
                     "data_snapshot,cost_model_version,seed,started_at,finished_at,outcome)"
                     " VALUES ('EXP-000001',1,'c','g','d','v',1,?,?,'ok')",
                     (db.now(), db.now()))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM trial WHERE seq = 1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE trial SET outcome = 'error' WHERE seq = 1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE trial SET config_hash = 'x' WHERE seq = 1")


def test_a_migrated_database_accepts_the_writes_that_were_failing(migrated):
    """The concrete symptom: sealing a trial writes `artifact_sha256`, and recording a
    verdict writes `status`. Both raised `no such column` on the live registry."""
    with db.connect(migrated) as conn:
        conn.execute("INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
                     " VALUES ('EXP-000001','probe','probe','RESEARCH',1,?)", (db.now(),))
        conn.execute("INSERT INTO trial(experiment_id,seq,config_hash,code_commit,"
                     "data_snapshot,cost_model_version,seed,started_at)"
                     " VALUES ('EXP-000001',1,'c','g','d','v',1,?)", (db.now(),))
        conn.execute("UPDATE trial SET finished_at=?, outcome='ok', artifact_sha256=?"
                     " WHERE experiment_id='EXP-000001' AND seq=1", (db.now(), "a" * 64))
        conn.execute("INSERT INTO falsification(experiment_id,test_name,statistic,"
                     "threshold,status,passed,ran_at,trials_at_run)"
                     " VALUES ('EXP-000001','3-cost-ladder',1.0,0.0,'PASS',1,?,1)",
                     (db.now(),))
        assert conn.execute("SELECT status FROM falsification").fetchone()[0] == "PASS"


# ══════════════════════════════════════════════════════════════════════════════
# 6. the observed schema is never cached
# ══════════════════════════════════════════════════════════════════════════════
def legacy_transform(sql: str) -> str:
    """`schema.sql` as it stood before the migrated columns existed."""
    for table, column, _decl in db.MIGRATIONS:
        block = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql, re.S)
        assert block is not None
        body = re.sub(rf"\n\s*{column}\s+\w+[^\n]*", "", block.group(1))
        sql = sql[:block.start(1)] + _drop_dangling_comma(body) + sql[block.end(1):]
    return sql


def test_a_database_replaced_after_a_successful_check_is_rechecked(tmp_path):
    """The gap a pathname cache leaves open.

    A long-running autonomous process opens the registry once, succeeds, and thereafter
    trusts the NAME. The file behind that name can be replaced, restored from a backup or
    rewritten by another process, and every later `connect` would sail past validation
    into a database this code has never inspected.
    """
    path = tmp_path / "registry.sqlite"
    db.init(path)
    with db.connect(path) as conn:
        conn.execute("SELECT 1")

    path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    build(path, legacy_transform)

    with pytest.raises(db.SchemaOutOfDate), db.connect(path) as conn:
        conn.execute("SELECT 1")


def test_a_damaged_database_at_a_previously_good_path_is_rechecked(tmp_path):
    """The same gap reached by damage rather than replacement, so a stat- or mtime-based
    cache would be no better: the inode is unchanged and WAL makes mtime unreliable."""
    path = tmp_path / "registry.sqlite"
    db.init(path)
    with db.connect(path):
        pass
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER trial_no_delete")
    with pytest.raises(db.SchemaOutOfDate), db.connect(path):
        pass


def test_the_expected_schema_is_cached_but_the_observed_is_not(tmp_path):
    """`schema.sql` cannot change inside a process, so deriving it once is safe and keeps
    the check cheap. The database can change, so it is read every single time."""
    assert db.expected_schema() is db.expected_schema()
    path = tmp_path / "registry.sqlite"
    db.init(path)
    assert db.schema_status(path).current
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE learning")
    assert not db.schema_status(path).current


# ══════════════════════════════════════════════════════════════════════════════
# 7. "matches schema.sql" must mean matches, not "every expected name is present"
# ══════════════════════════════════════════════════════════════════════════════
#: Each case leaves every expected NAME in place, so a name-only check passes it. None is
#: repairable by `init`, which can only create missing objects and add migration columns.
INCOMPATIBLE = [
    ("a column with the wrong type",
     lambda s: s.replace("seed               INTEGER NOT NULL,",
                         "seed               TEXT NOT NULL,"), "seed"),
    ("a column in the wrong position",
     lambda s: s.replace("finished_at        TEXT,\n    outcome            TEXT,",
                         "outcome            TEXT,\n    finished_at        TEXT,"),
     "outcome"),
    ("a changed default",
     lambda s: s.replace("status         TEXT NOT NULL DEFAULT 'RESEARCH',",
                         "status         TEXT NOT NULL DEFAULT 'IDEA',"), "status"),
    ("a relaxed NOT NULL",
     lambda s: s.replace("trial_budget   INTEGER NOT NULL,",
                         "trial_budget   INTEGER,"), "trial_budget"),
    ("a moved primary key",
     lambda s: s.replace("id TEXT PRIMARY KEY, title TEXT NOT NULL, note_path TEXT NOT NULL,",
                         "id TEXT, title TEXT NOT NULL PRIMARY KEY, note_path TEXT NOT NULL,"),
     "decision"),
    ("an unexpected column",
     lambda s: s.replace("    note               TEXT,",
                         "    note               TEXT,\n    surprise           TEXT,"),
     "surprise"),
    ("a column missing that no migration declares",
     lambda s: s.replace("    note               TEXT,\n", ""), "note"),
    ("a dropped UNIQUE constraint",
     lambda s: s.replace("    UNIQUE (experiment_id, seq),\n", ""), "trial"),
    ("a table implemented as a view", lambda s: s, "learning"),
    ("an altered trigger under the same name", lambda s: s, "trial_no_delete"),
    ("an unexpected table", lambda s: s, "surprise_table"),
]

#: Damage that cannot be expressed in the DDL, applied after the schema is created.
_AFTER = {
    "a table implemented as a view":
        "DROP TABLE learning;\nCREATE VIEW learning AS SELECT id FROM decision;",
    "an altered trigger under the same name":
        "DROP TRIGGER trial_no_delete;\n"
        "CREATE TRIGGER trial_no_delete BEFORE DELETE ON trial BEGIN SELECT 1; END;",
    "an unexpected table": "CREATE TABLE surprise_table (id TEXT PRIMARY KEY);",
}

CASE_IDS = [c[0] for c in INCOMPATIBLE]


@pytest.mark.parametrize("label,transform,mentioned", INCOMPATIBLE, ids=CASE_IDS)
def test_incompatible_drift_is_detected_and_named(tmp_path, label, transform, mentioned):
    path = build(tmp_path / "drifted.sqlite", transform, _AFTER.get(label))
    status = db.schema_status(path)
    assert not status.current, f"{label} passed the check"
    assert status.incompatible, f"{label} was not classified as incompatible"
    assert mentioned in " ".join(status.incompatible), (
        f"{label}: nothing named {mentioned!r} in {status.incompatible}")


@pytest.mark.parametrize("label,transform,mentioned", INCOMPATIBLE, ids=CASE_IDS)
def test_incompatible_drift_never_promises_init_will_fix_it(tmp_path, label, transform,
                                                            mentioned):
    """`init` creates missing objects and adds declared migration columns. It cannot
    retype a column, reorder one, replace a trigger or remove a surprise. Telling the
    operator to run it would send them round a loop that changes nothing."""
    path = build(tmp_path / "drifted.sqlite", transform, _AFTER.get(label))
    status = db.schema_status(path)
    assert not status.repairable
    assert db.SCHEMA_RECOVERY_COMMAND not in status.summary(), (
        f"{label}: the summary falsely promises `init` will repair it")


@pytest.mark.parametrize("label,transform,mentioned", INCOMPATIBLE, ids=CASE_IDS)
def test_incompatible_drift_is_refused_by_connect(tmp_path, label, transform, mentioned):
    path = build(tmp_path / "drifted.sqlite", transform, _AFTER.get(label))
    with pytest.raises(db.SchemaOutOfDate), db.connect(path):
        pass


def test_a_database_behind_the_code_is_repairable_and_says_so(stale):
    status = db.schema_status(stale)
    assert not status.current
    assert status.repairable
    assert not status.incompatible
    assert db.SCHEMA_RECOVERY_COMMAND in status.summary()


def test_a_missing_object_is_behind_not_incompatible(tmp_path):
    """`CREATE ... IF NOT EXISTS` genuinely restores these, so `init` is honest advice."""
    path = build(tmp_path / "partial.sqlite")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER trial_no_reseal")
        conn.execute("DROP INDEX ix_trial_experiment")
        conn.execute("DROP VIEW experiment_trials")
    status = db.schema_status(path)
    assert status.repairable and not status.incompatible
    assert db.SCHEMA_RECOVERY_COMMAND in status.summary()
    db.init(path)
    assert db.schema_status(path).current


def test_both_kinds_are_reported_together_and_init_is_not_promised(tmp_path):
    path = build(tmp_path / "both.sqlite", legacy_transform)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER trial_no_delete")
        conn.execute("CREATE TRIGGER trial_no_delete BEFORE DELETE ON trial"
                     " BEGIN SELECT 1; END")
    status = db.schema_status(path)
    assert status.missing_columns and status.incompatible
    assert not status.repairable
    assert db.SCHEMA_RECOVERY_COMMAND not in status.summary()


# ══════════════════════════════════════════════════════════════════════════════
# 8. CHECK constraints and foreign keys are contract, not metadata
# ══════════════════════════════════════════════════════════════════════════════
# Each of these leaves every table, column, type, position, default, index, trigger and
# view intact. Only a constraint changes — and each constraint enforces an invariant the
# desk actually rests on:
#
#   trial_budget > 0                 a budget of zero is not a declared budget
#   experiment.status IN (...)       the lifecycle cannot reach a state nobody defined
#   trial.outcome IN (...)           the DSR counts ok/error/abandoned and nothing else
#   REFERENCES experiment(id)        a trial or a verdict cannot orphan itself
#
# `init()` cannot restore any of them: `CREATE TABLE IF NOT EXISTS` leaves an existing
# table alone, and SQLite cannot add a constraint to one. So every case here must be
# incompatible, and none may be offered the recovery command.
BUDGET_CHECK = "    CHECK (trial_budget > 0),\n"
STATUS_CHECK = ("    CHECK (status IN ('IDEA','CANDIDATE','RESEARCH','BACKTESTING',"
                "'VALIDATING',\n                      'FALSIFYING','PAPER','APPROVED',"
                "'LIVE','DEGRADED',\n                      'SUSPENDED','RETIRED',"
                "'KILLED'))")
OUTCOME_CHECK = ("    UNIQUE (experiment_id, seq),\n    CHECK (outcome IS NULL OR outcome"
                 " IN ('ok','error','abandoned'))\n")
TRIAL_FK = "    experiment_id      TEXT NOT NULL REFERENCES experiment(id),\n"
FALSIFY_FK = "    experiment_id TEXT NOT NULL REFERENCES experiment(id),\n"


def _drop_status_check(sql: str) -> str:
    """Remove the last CHECK on `experiment`, and the comma that preceded it."""
    assert STATUS_CHECK in sql, "the experiment status CHECK is not where this test expects"
    return sql.replace(BUDGET_CHECK + STATUS_CHECK, "    CHECK (trial_budget > 0)")


CONSTRAINT_CASES = [
    ("the positive-budget CHECK removed",
     lambda s: s.replace(BUDGET_CHECK, ""), "experiment"),
    ("the positive-budget CHECK weakened",
     lambda s: s.replace("CHECK (trial_budget > 0)", "CHECK (trial_budget >= 0)"),
     "experiment"),
    ("the lifecycle-state CHECK removed", _drop_status_check, "experiment"),
    ("a lifecycle state silently added",
     lambda s: s.replace("'SUSPENDED','RETIRED','KILLED'))",
                         "'SUSPENDED','RETIRED','KILLED','PROMOTED'))"), "experiment"),
    ("the trial-outcome CHECK removed",
     lambda s: s.replace(OUTCOME_CHECK, "    UNIQUE (experiment_id, seq)\n"), "trial"),
    ("a trial outcome silently added",
     lambda s: s.replace("IN ('ok','error','abandoned')",
                         "IN ('ok','error','abandoned','discarded')"), "trial"),
    ("trial.experiment_id loses its foreign key",
     lambda s: s.replace(TRIAL_FK, "    experiment_id      TEXT NOT NULL,\n"), "trial"),
    ("trial.experiment_id points at a different parent",
     lambda s: s.replace(TRIAL_FK,
                         "    experiment_id      TEXT NOT NULL REFERENCES decision(id),\n"),
     "trial"),
    ("trial.experiment_id points at a different column",
     lambda s: s.replace(TRIAL_FK,
                         "    experiment_id      TEXT NOT NULL REFERENCES experiment(slug),\n"),
     "trial"),
    ("falsification.experiment_id loses its foreign key",
     lambda s: s.replace(FALSIFY_FK, "    experiment_id TEXT NOT NULL,\n"), "falsification"),
    ("falsification.experiment_id points at a different parent",
     lambda s: s.replace(FALSIFY_FK,
                         "    experiment_id TEXT NOT NULL REFERENCES decision(id),\n"),
     "falsification"),
]
CONSTRAINT_IDS = [c[0] for c in CONSTRAINT_CASES]


@pytest.mark.parametrize("label,transform,table", CONSTRAINT_CASES, ids=CONSTRAINT_IDS)
def test_a_constraint_change_is_detected(tmp_path, label, transform, table):
    """Every other part of the schema is untouched, so only a constraint comparison can
    see this at all."""
    original = db.SCHEMA_PATH.read_text(encoding="utf-8")
    assert transform(original) != original, f"{label}: the transform matched nothing"
    path = build(tmp_path / "constrained.sqlite", transform)
    status = db.schema_status(path)
    assert not status.current, f"{label} passed the schema check"
    assert status.incompatible, f"{label} was not classified as incompatible"


@pytest.mark.parametrize("label,transform,table", CONSTRAINT_CASES, ids=CONSTRAINT_IDS)
def test_a_constraint_change_names_the_table_and_the_constraint_class(tmp_path, label,
                                                                     transform, table):
    path = build(tmp_path / "constrained.sqlite", transform)
    said = " ".join(db.schema_status(path).incompatible)
    assert table in said, f"{label}: {table!r} is not named in {said!r}"
    expected_class = "foreign key" if "foreign key" in label or "points at" in label \
        else "CHECK"
    assert expected_class in said, f"{label}: {expected_class!r} is not named in {said!r}"


@pytest.mark.parametrize("label,transform,table", CONSTRAINT_CASES, ids=CONSTRAINT_IDS)
def test_a_constraint_change_is_refused_by_connect(tmp_path, label, transform, table):
    path = build(tmp_path / "constrained.sqlite", transform)
    with pytest.raises(db.SchemaOutOfDate), db.connect(path):
        pass


@pytest.mark.parametrize("label,transform,table", CONSTRAINT_CASES, ids=CONSTRAINT_IDS)
def test_a_constraint_change_is_never_offered_the_recovery_command(tmp_path, label,
                                                                   transform, table):
    """SQLite cannot add a constraint to an existing table, and `CREATE TABLE IF NOT
    EXISTS` leaves one alone. `init` would change nothing here."""
    path = build(tmp_path / "constrained.sqlite", transform)
    status = db.schema_status(path)
    assert not status.repairable
    assert db.SCHEMA_RECOVERY_COMMAND not in status.summary()


def test_a_migrated_table_keeps_its_constraints_and_still_matches(stale):
    """The reason CHECKs are compared as extracted expressions rather than as raw DDL.

    `ALTER TABLE ADD COLUMN` rewrites the stored CREATE text, appending the new column
    after the table constraints. The string differs; the constraints do not.
    """
    db.init(stale)
    assert db.schema_status(stale).current
    with sqlite3.connect(f"file:{stale}?mode=ro", uri=True) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'trial'").fetchone()[0]
    assert "artifact_sha256" in sql
    assert "CHECK" in sql
    assert db.check_expressions(sql) == db.expected_schema().tables["trial"].checks


def test_check_extraction_handles_nesting_and_quoted_text():
    """A regex stopping at the first `)` would truncate every one of these."""
    sql = (
        "CREATE TABLE t (\n"
        "  a TEXT CHECK (a IN ('x)y', 'z')),\n"
        "  b INTEGER,\n"
        "  CHECK (b > 0 AND (b < 10 OR b IN (20, 30))),\n"
        "  CHECK (a <> ')' )\n"
        ")"
    )
    assert db.check_expressions(sql) == (
        "a IN ('x)y', 'z')",
        "b > 0 AND (b < 10 OR b IN (20, 30))",
        "a <> ')'",
    )


def test_check_extraction_normalises_layout_but_not_meaning():
    spaced = "CREATE TABLE t (\n  a INTEGER,\n  CHECK (a\n        >\n        0)\n)"
    tight = "CREATE TABLE t (a INTEGER, CHECK (a > 0))"
    assert db.check_expressions(spaced) == db.check_expressions(tight)
    assert db.check_expressions("CREATE TABLE t (a INTEGER, CHECK (a > 0))") != \
        db.check_expressions("CREATE TABLE t (a INTEGER, CHECK (a >= 0))")
    # whitespace inside a literal is content, not layout
    assert db.check_expressions("CREATE TABLE t (a TEXT, CHECK (a = 'x  y'))") != \
        db.check_expressions("CREATE TABLE t (a TEXT, CHECK (a = 'x y'))")


def test_the_expected_contract_carries_constraints():
    """Derived from the in-memory fresh schema, never a hand-maintained list."""
    tables = db.expected_schema().tables
    assert tables["experiment"].checks, "experiment declares two CHECKs"
    assert tables["trial"].checks, "trial declares an outcome CHECK"
    assert tables["trial"].foreign_keys, "trial references experiment"
    assert tables["falsification"].foreign_keys, "falsification references experiment"
    assert tables["decision"].checks == ()
    assert tables["decision"].foreign_keys == ()
