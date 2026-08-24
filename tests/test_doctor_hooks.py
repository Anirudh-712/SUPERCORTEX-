"""doctor must notice when preventive validation stops being enforced.

A control that is configured but unregistered is worse than an absent one: the repository
looks protected, the tests still pass, and nothing fires. These checks exist so that state
is visible to the operator rather than inferred.

The CLI-level cases assert on what `supercortex doctor` actually prints and the exit code
it returns, not on the helper. A green helper behind a broken command helps nobody.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from supercortex import health
from supercortex.cli import app
from supercortex.registry import db

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()

PRE_HOOK = "no_fabricated_numbers_pre.py"
CHECK_CLAUDE_HOOK = "preventive hook present (.claude)"
CHECK_CLAUDE_REG = "preventive hook registered (.claude)"
CHECK_CODEX_HOOK = "preventive hook present (.codex)"
CHECK_CODEX_REG = "preventive hook registered (.codex)"
CHECK_SYNC = "number-hook adapters synchronised"


@pytest.fixture
def wired(tmp_path: Path) -> Path:
    """A project whose hook layer is correctly wired, copied from the real one."""
    shutil.copytree(REPO / ".claude", tmp_path / ".claude")
    shutil.copytree(REPO / ".codex", tmp_path / ".codex")
    return tmp_path


def named(checks: list[health.Check], name: str) -> health.Check:
    for check in checks:
        if check.name == name:
            return check
    raise AssertionError(f"doctor has no check named {name!r}")


# ── the wired baseline ──────────────────────────────────────────────────────────
def test_all_five_hook_checks_pass_when_correctly_wired(wired: Path):
    checks = health.hook_checks(wired)
    for name in (CHECK_CLAUDE_HOOK, CHECK_CLAUDE_REG,
                 CHECK_CODEX_HOOK, CHECK_CODEX_REG, CHECK_SYNC):
        assert named(checks, name).ok, f"{name}: {named(checks, name).detail}"


# ── each failure mode, individually ─────────────────────────────────────────────
def test_missing_claude_hook_fails(wired: Path):
    (wired / ".claude" / "hooks" / PRE_HOOK).unlink()
    check = named(health.hook_checks(wired), CHECK_CLAUDE_HOOK)
    assert not check.ok
    assert "missing" in check.detail.lower()


def test_non_executable_claude_hook_fails(wired: Path):
    (wired / ".claude" / "hooks" / PRE_HOOK).chmod(0o644)
    check = named(health.hook_checks(wired), CHECK_CLAUDE_HOOK)
    assert not check.ok
    assert "executable" in check.detail.lower()


def test_missing_codex_hook_fails(wired: Path):
    (wired / ".codex" / "hooks" / PRE_HOOK).unlink()
    assert not named(health.hook_checks(wired), CHECK_CODEX_HOOK).ok


def test_removed_claude_registration_fails(wired: Path):
    path = wired / ".claude" / "settings.json"
    config = json.loads(path.read_text())
    for entry in config["hooks"]["PreToolUse"]:
        entry["hooks"] = [h for h in entry["hooks"] if PRE_HOOK not in h["command"]]
    path.write_text(json.dumps(config, indent=2))
    check = named(health.hook_checks(wired), CHECK_CLAUDE_REG)
    assert not check.ok
    assert "not registered" in check.detail.lower()


def test_registration_under_the_wrong_matcher_fails(wired: Path):
    """Registered but only for Bash: configured, and never fires on a document write."""
    path = wired / ".claude" / "settings.json"
    config = json.loads(path.read_text())
    for entry in config["hooks"]["PreToolUse"]:
        if entry.get("matcher") == "Write|Edit":
            entry["matcher"] = "Bash"
    path.write_text(json.dumps(config, indent=2))
    assert not named(health.hook_checks(wired), CHECK_CLAUDE_REG).ok


def test_registration_under_the_wrong_event_fails(wired: Path):
    """Moved to PostToolUse: it would verify, never prevent."""
    path = wired / ".claude" / "settings.json"
    config = json.loads(path.read_text())
    moved = []
    for entry in config["hooks"]["PreToolUse"]:
        keep = [h for h in entry["hooks"] if PRE_HOOK not in h["command"]]
        moved += [h for h in entry["hooks"] if PRE_HOOK in h["command"]]
        entry["hooks"] = keep
    config["hooks"]["PostToolUse"][0]["hooks"] += moved
    path.write_text(json.dumps(config, indent=2))
    assert not named(health.hook_checks(wired), CHECK_CLAUDE_REG).ok


def test_removed_codex_registration_fails(wired: Path):
    path = wired / ".codex" / "hooks.json"
    config = json.loads(path.read_text())
    for entry in config["hooks"]["PreToolUse"]:
        entry["hooks"] = [h for h in entry["hooks"] if PRE_HOOK not in h["command"]]
    path.write_text(json.dumps(config, indent=2))
    assert not named(health.hook_checks(wired), CHECK_CODEX_REG).ok


def test_unparseable_settings_fails_rather_than_passing(wired: Path):
    (wired / ".claude" / "settings.json").write_text("{ not json")
    check = named(health.hook_checks(wired), CHECK_CLAUDE_REG)
    assert not check.ok
    assert "unreadable" in check.detail.lower() or "parse" in check.detail.lower()


def test_desynchronised_adapters_fail(wired: Path):
    """A stale mirror runs the old, weaker logic under whichever tool loads it."""
    target = wired / ".codex" / "hooks" / "no_fabricated_numbers.py"
    target.write_text(target.read_text() + "\n# drifted\n")
    check = named(health.hook_checks(wired), CHECK_SYNC)
    assert not check.ok
    assert "no_fabricated_numbers.py" in check.detail


def test_desynchronised_preventive_adapter_fails(wired: Path):
    target = wired / ".codex" / "hooks" / PRE_HOOK
    target.write_text(target.read_text() + "\n# drifted\n")
    assert not named(health.hook_checks(wired), CHECK_SYNC).ok


# ── what the operator actually sees ─────────────────────────────────────────────
def invoke_doctor(project: Path, monkeypatch) -> tuple[int, str]:
    """Run the real command and capture exactly what it renders to the operator.

    `cli.console` is a rich Console bound at import, so CliRunner's stdout swap never
    reaches it. Substituting a Console that writes to a buffer captures the table itself
    rather than an empty string that would make every assertion vacuously pass.
    """
    import io

    from rich.console import Console

    import supercortex.cli as cli_module

    buffer = io.StringIO()
    monkeypatch.setattr(db, "PROJECT_ROOT", project)
    monkeypatch.setattr(db, "DB_PATH", project / "registry.sqlite")
    monkeypatch.setattr(cli_module, "console", Console(file=buffer, width=200))
    result = runner.invoke(app, ["doctor"])
    return result.exit_code, buffer.getvalue()


def test_doctor_reports_all_five_checks_to_the_operator(wired: Path, monkeypatch):
    db.init(wired / "registry.sqlite")
    _, output = invoke_doctor(wired, monkeypatch)
    for fragment in ("preventive hook present", "preventive hook registered",
                     "adapters synchronised"):
        assert fragment in output, output


def test_doctor_fails_when_the_preventive_hook_is_missing(wired: Path, monkeypatch):
    db.init(wired / "registry.sqlite")
    (wired / ".claude" / "hooks" / PRE_HOOK).unlink()
    code, output = invoke_doctor(wired, monkeypatch)
    assert code == 1
    assert "🔴" in output
    assert "all guarantees hold" not in output


def test_doctor_fails_when_claude_registration_is_removed(wired: Path, monkeypatch):
    db.init(wired / "registry.sqlite")
    path = wired / ".claude" / "settings.json"
    config = json.loads(path.read_text())
    for entry in config["hooks"]["PreToolUse"]:
        entry["hooks"] = [h for h in entry["hooks"] if PRE_HOOK not in h["command"]]
    path.write_text(json.dumps(config, indent=2))
    code, output = invoke_doctor(wired, monkeypatch)
    assert code == 1
    assert "all guarantees hold" not in output


def test_doctor_fails_when_codex_registration_is_removed(wired: Path, monkeypatch):
    db.init(wired / "registry.sqlite")
    path = wired / ".codex" / "hooks.json"
    config = json.loads(path.read_text())
    for entry in config["hooks"]["PreToolUse"]:
        entry["hooks"] = [h for h in entry["hooks"] if PRE_HOOK not in h["command"]]
    path.write_text(json.dumps(config, indent=2))
    code, _ = invoke_doctor(wired, monkeypatch)
    assert code == 1


def test_doctor_passes_on_the_real_repository():
    """The live repo must actually be wired, not merely wireable."""
    checks = health.hook_checks(REPO)
    failed = [c for c in checks if not c.ok]
    assert not failed, [f"{c.name}: {c.detail}" for c in failed]


# ── the instruction files must agree ────────────────────────────────────────────
def test_claude_and_agents_instructions_stay_identical():
    assert (REPO / "AGENTS.md").read_text() == (REPO / "CLAUDE.md").read_text()


def test_law_one_describes_both_layers():
    text = (REPO / "CLAUDE.md").read_text()
    law = text.split("1. **NEVER write a number")[1].split("2. **NEVER run")[0]
    assert "PreToolUse" in law, "Law 1 must say the write is PREVENTED, not just reported"
    assert "PostToolUse" in law, "Law 1 must still describe the verification layer"
