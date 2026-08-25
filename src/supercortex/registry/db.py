"""Registry access. The only sanctioned path to registry.sqlite.

Being the only path is what lets this module refuse a stale one. `schema.sql` plus
`MIGRATIONS` is the specification; a database that does not match it is not opened, so a
missing column is reported once, by name, with the command that fixes it — instead of
surfacing as `no such column` from inside SQLite at whatever moment a command first
touched it.

That failure was live, not hypothetical: `registry.sqlite` carried neither
`trial.artifact_sha256` nor `falsification.status`, because migrations run only inside
`init()` and nothing had called it since those columns were added. Sealing a trial and
recording a falsification verdict would both have raised — against a green test suite,
because every test builds its own registry with `init` and is therefore always current.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = PROJECT_ROOT / "registry.sqlite"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

ID_WIDTH = 6
PREFIXES = ("IDEA", "EXP", "STR", "DEC", "ADR", "LRN", "TASK", "DATA", "AGT", "DEP", "INC")


#: Canonical timestamp format: UTC, space-separated, no offset.
#: This is deliberately SQLite's own `datetime('now')` format rather than strict
#: ISO-8601. With a 'T' separator and a '+00:00' offset, string comparison against
#: `datetime('now', '-6 hours')` silently never matches, which made trial reaping
#: dead code. `datetime.fromisoformat` still parses this, so nothing downstream loses.
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def now() -> str:
    """Timestamps are UTC. Local time is a reproducibility bug."""
    return datetime.now(UTC).strftime(TS_FORMAT)


@contextmanager
def connect(path: Path | None = None, *, allow_stale: bool = False) -> Iterator[sqlite3.Connection]:
    """Open the registry, or refuse a schema that does not match `schema.sql`.

    The check is here rather than in each command because this is the chokepoint every
    registry-dependent path already passes through, and a command cannot forget it. It
    also means `--help`, `--version` and an unknown command keep working on a stale
    registry without being special-cased: none of them opens the database.

    `allow_stale` exists for `init`, which is the sanctioned repair path and cannot be
    blocked by the condition it exists to fix.
    """
    target = Path(path or DB_PATH)
    if not allow_stale:
        require_current_schema(target)
    conn = sqlite3.connect(target, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


#: Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` will not add
#: them to an existing database, so they are applied explicitly and idempotently.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("dataset", "kind", "TEXT"),
    ("dataset", "parquet_path", "TEXT"),
    ("trial", "artifact_sha256", "TEXT"),
    ("falsification", "status", "TEXT"),
)


def init(path: Path | None = None) -> Path:
    """Create or migrate the registry. The ONLY sanctioned schema-mutation path.

    Ordinary reads never migrate. A read that quietly repaired the schema would make the
    staleness invisible again, and the repair would happen at an arbitrary moment inside
    whatever command happened to run first.
    """
    target = Path(path or DB_PATH)
    with connect(target, allow_stale=True) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for table, column, decl in MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return target


# ── schema currency ─────────────────────────────────────────────────────────────
#: What to run when a registry is behind the code. Stated once, quoted everywhere — and
#: deliberately absent from the diagnostic for drift `init` cannot repair.
SCHEMA_RECOVERY_COMMAND = "uv run supercortex init"


def _normalise(sql: object) -> str:
    """DDL text with whitespace collapsed, so indentation is not a difference."""
    return " ".join(str(sql or "").split())


# ── reading constraints out of DDL ──────────────────────────────────────────────
# `PRAGMA table_info` does not show CHECK constraints and there is no pragma that does, so
# they are read out of the CREATE TABLE text. The raw text cannot be compared directly:
# `ALTER TABLE ADD COLUMN` rewrites it, appending the new column AFTER the table
# constraints, so a fresh and a migrated table differ as strings while declaring the same
# constraints. What is compared is the extracted expressions.
#
# SQLite preserves comments in `sqlite_master.sql`, and this schema's comments contain both
# the word CHECK's neighbours and backticks, so comments are removed before anything else
# is looked at. A regex would be wrong twice over: it would stop at the first `)` inside a
# nested expression, and it would match text inside a string literal or a comment.
_QUOTES = {"'": "'", '"': '"', "`": "`", "[": "]"}


def _closing(text: str, start: int) -> int:
    """Index just past the quote opened at `start`. Doubled quotes are escapes."""
    opener = text[start]
    closer = _QUOTES[opener]
    index = start + 1
    while index < len(text):
        if text[index] == closer:
            if closer != "]" and index + 1 < len(text) and text[index + 1] == closer:
                index += 2
                continue
            return index + 1
        index += 1
    return len(text)


def _strip_comments(sql: str) -> str:
    """SQL with `--` and `/* */` comments removed, leaving string literals untouched."""
    out: list[str] = []
    index, length = 0, len(sql)
    while index < length:
        char = sql[index]
        if char in _QUOTES:
            end = _closing(sql, index)
            out.append(sql[index:end])
            index = end
        elif sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline
        elif sql.startswith("/*", index):
            close = sql.find("*/", index + 2)
            index = length if close == -1 else close + 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _balanced(text: str, start: int) -> tuple[str, int]:
    """The contents of the parenthesis group opening at `start`, and the index past it."""
    depth, index = 0, start
    while index < len(text):
        char = text[index]
        if char in _QUOTES:
            index = _closing(text, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
        index += 1
    raise ValueError("unbalanced parentheses in DDL")


def _normalise_expression(expression: str) -> str:
    """Collapse layout, preserve meaning.

    Whitespace OUTSIDE a quoted literal is formatting and is collapsed to one space.
    Whitespace inside one is content: `a = 'x  y'` and `a = 'x y'` are different
    constraints and must not be normalised together.
    """
    out: list[str] = []
    index, length, pending = 0, len(expression), False
    while index < length:
        char = expression[index]
        if char in _QUOTES:
            end = _closing(expression, index)
            if pending and out:
                out.append(" ")
            pending = False
            out.append(expression[index:end])
            index = end
            continue
        if char.isspace():
            pending = True
            index += 1
            continue
        if pending and out:
            out.append(" ")
        pending = False
        out.append(char)
        index += 1
    return "".join(out).strip()


def check_expressions(sql: object) -> tuple[str, ...]:
    """Every CHECK expression in a CREATE TABLE statement, in declaration order.

    Column-level and table-level alike: both are CHECK constraints on the table, and
    which one an author chose is not a semantic difference.
    """
    text = _strip_comments(str(sql or ""))
    found: list[str] = []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char in _QUOTES:
            index = _closing(text, index)
            continue
        if (char in "cC" and text[index:index + 5].upper() == "CHECK"
                and (index == 0 or not (text[index - 1].isalnum() or text[index - 1] == "_"))):
            cursor = index + 5
            while cursor < length and text[cursor].isspace():
                cursor += 1
            if cursor < length and text[cursor] == "(":
                expression, end = _balanced(text, cursor)
                found.append(_normalise_expression(expression))
                index = end
                continue
        index += 1
    return tuple(found)


@dataclass(frozen=True)
class ExpectedTable:
    """One table or view exactly as `schema.sql` declares it."""

    kind: str                                  # 'table' | 'view'
    #: Ordered PRAGMA table_info records: (cid, name, type, notnull, dflt_value, pk).
    columns: tuple[tuple[object, ...], ...]
    #: Constraint-backed indexes only, by name: (name, unique, origin, partial). These
    #: carry UNIQUE and PRIMARY KEY, which table_info does not show and which `trial`
    #: depends on. Indexes from an explicit CREATE INDEX are excluded because they are
    #: compared as objects, where a missing one is repairable rather than drift.
    indexes: tuple[tuple[object, ...], ...]
    #: CHECK expressions in declaration order. These enforce the registry's own
    #: invariants — a positive trial budget, a defined lifecycle state, an outcome the
    #: DSR knows how to count — and no PRAGMA reports them.
    checks: tuple[str, ...]
    #: Ordered PRAGMA foreign_key_list records. A trial or a verdict that can orphan
    #: itself is a registry that cannot be trusted to say what it counted.
    foreign_keys: tuple[tuple[object, ...], ...]
    sql: str


@dataclass(frozen=True)
class ExpectedObject:
    kind: str                                  # 'index' | 'trigger'
    sql: str


@dataclass(frozen=True)
class ExpectedSchema:
    tables: dict[str, ExpectedTable]
    objects: dict[str, ExpectedObject]


def _read_schema(conn: sqlite3.Connection) -> tuple[dict[str, ExpectedTable],
                                                    dict[str, ExpectedObject]]:
    """The contract as a live database actually implements it."""
    tables: dict[str, ExpectedTable] = {}
    objects: dict[str, ExpectedObject] = {}
    for name, kind, sql in conn.execute(
        "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        " ORDER BY name"
    ):
        if kind in ("table", "view"):
            tables[name] = ExpectedTable(
                kind=kind,
                columns=tuple(tuple(r) for r in conn.execute(f"PRAGMA table_info({name})")),
                indexes=tuple(sorted(
                    (r[1], r[2], r[3], r[4])
                    for r in conn.execute(f"PRAGMA index_list({name})")
                    if r[3] != "c")),
                checks=check_expressions(sql),
                foreign_keys=tuple(sorted(
                    tuple(r) for r in conn.execute(f"PRAGMA foreign_key_list({name})"))),
                sql=_normalise(sql),
            )
        else:
            objects[name] = ExpectedObject(kind=kind, sql=_normalise(sql))
    return tables, objects


@lru_cache(maxsize=1)
def expected_schema() -> ExpectedSchema:
    """The contract, derived by building `schema.sql` in memory and applying MIGRATIONS.

    Cached because `schema.sql` cannot change inside a process. The OBSERVED registry is
    never cached: that file can be replaced, restored from a backup or edited by another
    process between two calls, and a cache keyed on its pathname would let every later
    open sail past validation into a database this code has not inspected.

    Derived rather than hand-written for the same reason the equivalence tests exist: a
    second list of expected columns is a second source of truth, and it goes stale
    silently.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for table, column, decl in MIGRATIONS:
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        tables, objects = _read_schema(conn)
        return ExpectedSchema(tables=tables, objects=objects)
    finally:
        conn.close()


def required_shape() -> dict[str, tuple[str, ...]]:
    """Every table and view the code requires, with its columns in declaration order."""
    return {name: tuple(str(c[1]) for c in table.columns)
            for name, table in expected_schema().tables.items()}


#: Column metadata compared position by position, and how to name each field to an
#: operator. `cid` is covered by comparing the records in order.
_COLUMN_FIELDS = ((2, "type"), (3, "NOT NULL"), (4, "default"), (5, "primary-key position"))

#: Columns `init` can add, because `ALTER TABLE ADD COLUMN` is what MIGRATIONS runs. A
#: column missing from any other table is drift, not a pending migration.
def _is_migration(table: str, column: str) -> bool:
    return any(t == table and c == column for t, c, _ in MIGRATIONS)


@dataclass(frozen=True)
class SchemaStatus:
    """What a registry is missing, and whether `init` can actually fix it.

    Two buckets, kept apart on purpose. `behind` is repairable: `schema.sql` creates
    missing objects with `IF NOT EXISTS` and MIGRATIONS adds declared columns.
    `incompatible` is not: `init` cannot retype a column, reorder one, replace a trigger
    or remove a surprise, so telling an operator to run it would send them round a loop
    that changes nothing.
    """

    path: Path
    exists: bool
    missing_tables: tuple[str, ...] = ()
    missing_columns: tuple[tuple[str, str], ...] = ()
    missing_indexes: tuple[str, ...] = ()
    missing_triggers: tuple[str, ...] = ()
    #: Differences `init` cannot repair, each a sentence naming what disagrees.
    incompatible: tuple[str, ...] = ()
    unreadable: str | None = None

    @property
    def current(self) -> bool:
        return (self.exists and self.unreadable is None
                and not self.missing and not self.incompatible)

    @property
    def repairable(self) -> bool:
        """`init` would genuinely fix this database."""
        return (self.exists and self.unreadable is None
                and bool(self.missing) and not self.incompatible)

    @property
    def missing(self) -> tuple[str, ...]:
        """Every repairable gap, named the way an operator would grep for it."""
        return (
            *(f"table {name}" for name in self.missing_tables),
            *(f"{table}.{column}" for table, column in self.missing_columns),
            *(f"index {name}" for name in self.missing_indexes),
            *(f"trigger {name}" for name in self.missing_triggers),
        )

    def summary(self) -> str:
        if self.current:
            return f"{self.path.name} matches schema.sql"
        if not self.exists:
            return (f"{self.path} does not exist. Run `{SCHEMA_RECOVERY_COMMAND}` to "
                    f"create it.")
        if self.unreadable is not None:
            return (f"{self.path.name} cannot be read as a registry: {self.unreadable}.")
        if self.incompatible:
            behind = (f" It is also missing {', '.join(self.missing)}."
                      if self.missing else "")
            return (
                f"{self.path.name} does not match schema.sql and cannot be repaired "
                f"automatically: {'; '.join(self.incompatible)}.{behind} Creating missing "
                f"objects and adding declared columns is the whole of what migration "
                f"does, and none of these is that. Nothing will run against this "
                f"database until the difference is explained — restore it, or re-create "
                f"it from a registry this code wrote."
            )
        return (
            f"{self.path.name} is behind the code. Missing: {', '.join(self.missing)}. "
            f"Run `{SCHEMA_RECOVERY_COMMAND}` to migrate it; existing rows are preserved "
            f"and the added columns are NULL, never backfilled."
        )


class SchemaOutOfDate(RuntimeError):
    """The registry does not match `schema.sql`, so nothing may run against it.

    A plain exception: the registry has no business importing the CLI framework. The
    command layer turns it into a clean exit, in one place — see `SupercortexGroup` in
    `supercortex.cli`.
    """

    def __init__(self, status: SchemaStatus) -> None:
        super().__init__(status.summary())
        self.status = status


def _compare_table(name: str, want: ExpectedTable, got: ExpectedTable,
                   missing_columns: list[tuple[str, str]],
                   incompatible: list[str]) -> None:
    """One table or view, field by field."""
    if got.kind != want.kind:
        incompatible.append(f"{name} is a {got.kind}, expected a {want.kind}")
        return

    want_names = [str(c[1]) for c in want.columns]
    got_names = [str(c[1]) for c in got.columns]
    for column in want_names:
        if column in got_names:
            continue
        if _is_migration(name, column):
            missing_columns.append((name, column))
        else:
            incompatible.append(
                f"{name}.{column} is missing and no migration declares it, so it cannot "
                f"be added")
    for column in got_names:
        if column not in want_names:
            incompatible.append(f"{name}.{column} is present but schema.sql does not "
                                f"declare it")

    if sorted(want_names) == sorted(got_names) and want_names != got_names:
        # Every column is present and none is a surprise, but they are in a different
        # order. Nothing in the per-column metadata has to differ for this to be a real
        # incompatibility, so comparing sets of names would miss it entirely.
        for position, (wanted, found) in enumerate(zip(want_names, got_names,
                                                       strict=True)):
            if wanted != found:
                incompatible.append(
                    f"{name} column order differs: position {position} is {found}, "
                    f"expected {wanted}")

    if want_names == got_names:
        for want_row, got_row in zip(want.columns, got.columns, strict=True):
            for index, label in _COLUMN_FIELDS:
                if want_row[index] != got_row[index]:
                    incompatible.append(
                        f"{name}.{want_row[1]} {label} is {got_row[index]!r}, expected "
                        f"{want_row[index]!r}")
            if want_row[0] != got_row[0]:
                incompatible.append(
                    f"{name}.{want_row[1]} is at position {got_row[0]}, expected "
                    f"{want_row[0]}")

    if want.checks != got.checks:
        incompatible.append(
            f"{name} CHECK constraints differ from schema.sql: {list(got.checks)} != "
            f"{list(want.checks)}")
    if want.foreign_keys != got.foreign_keys:
        incompatible.append(
            f"{name} foreign keys differ from schema.sql: "
            f"{[(fk[2], fk[3], fk[4]) for fk in got.foreign_keys]} != "
            f"{[(fk[2], fk[3], fk[4]) for fk in want.foreign_keys]}")
    if want.indexes != got.indexes:
        incompatible.append(
            f"{name} UNIQUE/PRIMARY KEY constraints differ from schema.sql: "
            f"{[i[0] for i in got.indexes]} != {[i[0] for i in want.indexes]}")
    if want.kind == "view" and want.sql != got.sql:
        incompatible.append(f"view {name} is defined differently from schema.sql")


def schema_status(path: Path | None = None) -> SchemaStatus:
    """What this database is missing, and what about it cannot be repaired.

    Reads only, and never creates anything: `sqlite3.connect` creates the file it is
    pointed at, so the existence test comes first and the open is `mode=ro`.

    The whole declared contract is compared: column metadata and order, UNIQUE and PRIMARY
    KEY through `PRAGMA index_list`, foreign keys through `PRAGMA foreign_key_list`, CHECK
    expressions extracted from the CREATE text, and the definitions of views, indexes and
    triggers.
    """
    target = Path(path or DB_PATH)
    if not target.exists():
        return SchemaStatus(path=target, exists=False)

    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return SchemaStatus(path=target, exists=True, unreadable=str(exc))
    try:
        got_tables, got_objects = _read_schema(conn)
    except sqlite3.DatabaseError as exc:
        return SchemaStatus(path=target, exists=True, unreadable=str(exc))
    finally:
        conn.close()

    expected = expected_schema()
    missing_tables: list[str] = []
    missing_columns: list[tuple[str, str]] = []
    missing_indexes: list[str] = []
    missing_triggers: list[str] = []
    incompatible: list[str] = []

    for name, want in expected.tables.items():
        got = got_tables.get(name)
        if got is None:
            # The name may exist as an index or trigger instead, which is drift, not a
            # missing table that `CREATE TABLE IF NOT EXISTS` would restore.
            if name in got_objects:
                incompatible.append(
                    f"{name} is a {got_objects[name].kind}, expected a {want.kind}")
            else:
                missing_tables.append(name)
            continue
        _compare_table(name, want, got, missing_columns, incompatible)

    for name, want_object in expected.objects.items():
        got_object = got_objects.get(name)
        if got_object is None:
            if name in got_tables:
                incompatible.append(
                    f"{name} is a {got_tables[name].kind}, expected a {want_object.kind}")
            elif want_object.kind == "index":
                missing_indexes.append(name)
            else:
                missing_triggers.append(name)
            continue
        if got_object.kind != want_object.kind:
            incompatible.append(
                f"{name} is a {got_object.kind}, expected a {want_object.kind}")
        elif got_object.sql != want_object.sql:
            incompatible.append(
                f"{want_object.kind} {name} exists under the right name but its "
                f"definition differs from schema.sql")

    for name in (*got_tables, *got_objects):
        if name not in expected.tables and name not in expected.objects:
            kind = got_tables[name].kind if name in got_tables else got_objects[name].kind
            incompatible.append(
                f"{kind} {name} is present but schema.sql does not declare it, which "
                f"means this database was written by different code")

    return SchemaStatus(
        path=target, exists=True,
        missing_tables=tuple(missing_tables), missing_columns=tuple(missing_columns),
        missing_indexes=tuple(missing_indexes), missing_triggers=tuple(missing_triggers),
        incompatible=tuple(incompatible),
    )


def require_current_schema(path: Path | None = None) -> None:
    """Raise `SchemaOutOfDate` unless this registry matches the specification.

    The database is re-read on every call. There is no observed-state cache — not by
    pathname, and deliberately not by stat or mtime either: the file can be replaced at
    the same path, and under WAL a committed change need not move the main file's mtime
    at all. Neither is an authority on what the schema currently is.
    """
    status = schema_status(path)
    if not status.current:
        raise SchemaOutOfDate(status)


def allocate_id(conn: sqlite3.Connection, prefix: str) -> str:
    """Allocate the next ID for a prefix. Monotonic, never reused, transactional."""
    if prefix not in PREFIXES:
        raise ValueError(f"unknown prefix {prefix!r}; expected one of {PREFIXES}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT next_value FROM id_counter WHERE prefix = ?", (prefix,)
        ).fetchone()
        value = row["next_value"] if row else 1
        conn.execute(
            "INSERT INTO id_counter(prefix, next_value) VALUES (?, ?) "
            "ON CONFLICT(prefix) DO UPDATE SET next_value = ?",
            (prefix, value + 1, value + 1),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return f"{prefix}-{value:0{ID_WIDTH}d}"


# ── provenance helpers ──────────────────────────────────────────────────────────
# Only these determine a trial's result, so only these must be committed for the run
# to be reproducible. The registry, artifacts and notes are *outputs* — the ledger
# recording the run is not an input to it, and requiring it to be clean would
# deadlock (registering an experiment dirties the registry, which blocks the trial).
REPRODUCIBILITY_INPUTS = ("src", "pyproject.toml", "uv.lock")


def git_commit(strict: bool = True, extra_inputs: tuple[str, ...] = ()) -> str:
    """Return HEAD. Refuse if any reproducibility input is uncommitted.

    A trial recorded against uncommitted strategy code cannot be re-run, so it is not
    evidence. `extra_inputs` lets a caller add the experiment's own config and strategy.
    """
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        commit = run("rev-parse", "HEAD")
    except subprocess.CalledProcessError as exc:  # no commits yet
        raise RuntimeError("no git commits yet — commit before running trials") from exc

    if strict:
        watched = [*REPRODUCIBILITY_INPUTS, *extra_inputs]
        dirty = run("status", "--porcelain", "--", *watched)
        if dirty:
            # Porcelain lines are "XY path", but `run` strips the whole output, which
            # removes the leading status space from the FIRST line only. A fixed-width
            # line[3:] therefore ate a character off exactly one filename — an error
            # message that named a file which does not exist. Split on whitespace instead.
            files = ", ".join(
                line.strip().split(maxsplit=1)[-1]
                for line in dirty.splitlines()[:6]
                if line.strip()
            )
            raise RuntimeError(
                f"uncommitted changes to reproducibility inputs: {files}. "
                "A trial recorded against uncommitted code cannot be re-run, so it is "
                "not evidence. Commit (or stash) first."
            )
    return commit


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def free_gb(path: Path | None = None) -> float:
    stat = os.statvfs(path or PROJECT_ROOT)
    return stat.f_bavail * stat.f_frsize / 1024**3
