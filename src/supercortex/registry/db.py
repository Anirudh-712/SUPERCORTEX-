"""Registry access. The only sanctioned path to registry.sqlite."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
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
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path or DB_PATH, isolation_level=None)
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
)


def init(path: Path | None = None) -> Path:
    target = path or DB_PATH
    with connect(target) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for table, column, decl in MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return target


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
