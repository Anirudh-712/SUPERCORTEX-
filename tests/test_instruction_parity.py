"""`CLAUDE.md` and `AGENTS.md` must stay byte-identical.

Different tools load different instruction files: Claude Code reads `CLAUDE.md`, other
agent harnesses read `AGENTS.md`. Both are kept as full copies on purpose — replacing
either with a pointer or a stub would leave whichever tool reads it with weaker rules, and
a symlink is not portable across checkouts and is resolved away by some tools.

Two copies can drift, and a drifted constitution is worse than a duplicated one: the tools
would be governed by different rules with nothing announcing it. `CLAUDE.md` is canonical,
`AGENTS.md` is its copy, and this test is what makes the duplication safe.

Scope is parity and portability only. Whether the rules themselves are adequate is a
code-review question, not something this test can answer.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CANONICAL = REPO / "CLAUDE.md"
COPY = REPO / "AGENTS.md"


def test_instruction_files_are_real_and_byte_identical() -> None:
    for path in (CANONICAL, COPY):
        assert not path.is_symlink(), f"{path.name} must be a real file, not a symlink"
        assert path.is_file(), f"{path.name} is missing or is not a regular file"

    canonical, copy = CANONICAL.read_bytes(), COPY.read_bytes()
    if canonical != copy:
        raise AssertionError(
            "instruction files have drifted:\n"
            f"  {CANONICAL.name}  {len(canonical):>6} bytes  "
            f"sha256 {hashlib.sha256(canonical).hexdigest()}\n"
            f"  {COPY.name}  {len(copy):>6} bytes  "
            f"sha256 {hashlib.sha256(copy).hexdigest()}\n"
            f"{CANONICAL.name} is canonical: copy it over {COPY.name}, or, if the two are "
            "meant to differ now, change this test deliberately and say why."
        )
