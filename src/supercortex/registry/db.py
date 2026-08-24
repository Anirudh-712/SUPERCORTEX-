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


def now() -> str:
    """Timestamps are UTC and ISO-8601. Local time is a reproducibility bug."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path or DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init(path: Path | None = None) -> Path:
    target = path or DB_PATH
    with connect(target) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
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
def git_commit(strict: bool = True) -> str:
    """Return HEAD. Refuse on a dirty tree: a trial you cannot reproduce is not evidence."""
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        commit = run("rev-parse", "HEAD")
    except subprocess.CalledProcessError as exc:  # no commits yet
        raise RuntimeError("no git commits yet — commit before running trials") from exc

    if strict and run("status", "--porcelain"):
        raise RuntimeError(
            "git tree is dirty. A trial recorded against uncommitted code cannot be "
            "reproduced, so it is not evidence. Commit (or stash) first."
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
