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
POST_HOOK = "no_fabricated_numbers.py"
CHECK_PRE = "preventive hook present"
CHECK_PRE_REG = "preventive hook registered"
CHECK_POST = "verifying hook present"
CHECK_POST_REG = "verifying hook registered"
ALL_CHECKS = (CHECK_PRE, CHECK_PRE_REG, CHECK_POST, CHECK_POST_REG)


@pytest.fixture
def wired(tmp_path: Path) -> Path:
    """A project whose hook layer is correctly wired, copied from the real one."""
    shutil.copytree(REPO / ".claude", tmp_path / ".claude")
    return tmp_path


def named(checks: list[health.Check], name: str) -> health.Check:
    for check in checks:
        if check.name == name:
            return check
    raise AssertionError(f"doctor has no check named {name!r}")


def rewrite_command(project: Path, command: str, filename: str = PRE_HOOK) -> None:
    path = project / ".claude" / "settings.json"
    config = json.loads(path.read_text())
    for entries in config["hooks"].values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                if filename in str(hook.get("command", "")):
                    hook["command"] = command
    path.write_text(json.dumps(config, indent=2))


# ── the wired baseline ──────────────────────────────────────────────────────────
def test_all_hook_checks_pass_when_correctly_wired(wired: Path):
    checks = health.hook_checks(wired)
    for name in ALL_CHECKS:
        assert named(checks, name).ok, f"{name}: {named(checks, name).detail}"


# ── each failure mode, individually ─────────────────────────────────────────────
def test_missing_hook_fails(wired: Path):
    (wired / ".claude" / "hooks" / PRE_HOOK).unlink()
    check = named(health.hook_checks(wired), CHECK_PRE)
    assert not check.ok
    assert "missing" in check.detail.lower()


def test_non_executable_hook_fails(wired: Path):
    (wired / ".claude" / "hooks" / PRE_HOOK).chmod(0o644)
    check = named(health.hook_checks(wired), CHECK_PRE)
    assert not check.ok
    assert "executable" in check.detail.lower()


def test_missing_verifying_hook_fails(wired: Path):
    (wired / ".claude" / "hooks" / POST_HOOK).unlink()
    assert not named(health.hook_checks(wired), CHECK_POST).ok


def test_removed_registration_fails(wired: Path):
    path = wired / ".claude" / "settings.json"
    config = json.loads(path.read_text())
    for entry in config["hooks"]["PreToolUse"]:
        entry["hooks"] = [h for h in entry["hooks"] if PRE_HOOK not in h["command"]]
    path.write_text(json.dumps(config, indent=2))
    check = named(health.hook_checks(wired), CHECK_PRE_REG)
    assert not check.ok
    assert "not registered" in check.detail.lower()


def test_removed_verifying_registration_fails(wired: Path):
    path = wired / ".claude" / "settings.json"
    config = json.loads(path.read_text())
    config["hooks"]["PostToolUse"] = []
    path.write_text(json.dumps(config, indent=2))
    check = named(health.hook_checks(wired), CHECK_POST_REG)
    assert not check.ok
    assert "nothing verifies" in check.detail.lower()


def test_registration_under_the_wrong_matcher_fails(wired: Path):
    """Registered but only for Bash: configured, and never fires on a document write."""
    path = wired / ".claude" / "settings.json"
    config = json.loads(path.read_text())
    for entry in config["hooks"]["PreToolUse"]:
        if entry.get("matcher") == "Write|Edit":
            entry["matcher"] = "Bash"
    path.write_text(json.dumps(config, indent=2))
    assert not named(health.hook_checks(wired), CHECK_PRE_REG).ok


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
    check = named(health.hook_checks(wired), CHECK_PRE_REG)
    assert not check.ok
    assert "PostToolUse" in check.detail


def test_unparseable_settings_fails_rather_than_passing(wired: Path):
    (wired / ".claude" / "settings.json").write_text("{ not json")
    check = named(health.hook_checks(wired), CHECK_PRE_REG)
    assert not check.ok
    assert "unreadable" in check.detail.lower() or "parse" in check.detail.lower()


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


def test_doctor_reports_every_hook_check_to_the_operator(wired: Path, monkeypatch):
    db.init(wired / "registry.sqlite")
    _, output = invoke_doctor(wired, monkeypatch)
    for fragment in ("preventive hook present", "preventive hook registered",
                     "verifying hook registered"):
        assert fragment in output, output


def test_doctor_fails_when_the_preventive_hook_is_missing(wired: Path, monkeypatch):
    db.init(wired / "registry.sqlite")
    (wired / ".claude" / "hooks" / PRE_HOOK).unlink()
    code, output = invoke_doctor(wired, monkeypatch)
    assert code == 1
    assert "🔴" in output
    assert "all guarantees hold" not in output


def test_doctor_fails_when_registration_is_removed(wired: Path, monkeypatch):
    db.init(wired / "registry.sqlite")
    path = wired / ".claude" / "settings.json"
    config = json.loads(path.read_text())
    for entry in config["hooks"]["PreToolUse"]:
        entry["hooks"] = [h for h in entry["hooks"] if PRE_HOOK not in h["command"]]
    path.write_text(json.dumps(config, indent=2))
    code, output = invoke_doctor(wired, monkeypatch)
    assert code == 1
    assert "all guarantees hold" not in output


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


# ══════════════════════════════════════════════════════════════════════════════
# Structurally malformed configuration must not crash doctor
# ══════════════════════════════════════════════════════════════════════════════
MALFORMED_CONFIGS = [
    ("top-level list", "[]"),
    ("top-level string", '"hooks"'),
    ("top-level number", "42"),
    ("hooks as a list", '{"hooks": []}'),
    ("hooks as a string", '{"hooks": "PreToolUse"}'),
    ("PreToolUse as a string", '{"hooks": {"PreToolUse": "Write|Edit"}}'),
    ("PreToolUse as an object", '{"hooks": {"PreToolUse": {"matcher": "Write|Edit"}}}'),
    ("entry is a string", '{"hooks": {"PreToolUse": ["Write|Edit"]}}'),
    ("entry is a number", '{"hooks": {"PreToolUse": [7]}}'),
    ("handlers is a string", '{"hooks": {"PreToolUse": [{"matcher": "Write|Edit",'
                            ' "hooks": "cmd"}]}}'),
    ("handler is a string", '{"hooks": {"PreToolUse": [{"matcher": "Write|Edit",'
                            ' "hooks": ["cmd"]}]}}'),
    ("command is a number", '{"hooks": {"PreToolUse": [{"matcher": "Write|Edit",'
                            ' "hooks": [{"type": "command", "command": 9}]}]}}'),
    ("not json at all", "{ not json"),
]


MALFORMED_IDS = [case[0] for case in MALFORMED_CONFIGS]


@pytest.mark.parametrize(("label", "body"), MALFORMED_CONFIGS, ids=MALFORMED_IDS)
def test_helper_survives_malformed_settings(wired: Path, label: str, body: str):
    (wired / ".claude" / "settings.json").write_text(body)
    check = named(health.hook_checks(wired), CHECK_PRE_REG)
    assert not check.ok, label
    assert check.detail.strip(), "a failure must carry a diagnostic"


@pytest.mark.parametrize(("label", "body"), MALFORMED_CONFIGS, ids=MALFORMED_IDS)
def test_doctor_reports_malformed_config_to_the_operator(wired: Path, monkeypatch,
                                                         label: str, body: str):
    """Operator level: a red row, a specific diagnostic, a non-zero exit, no traceback."""
    db.init(wired / "registry.sqlite")
    (wired / ".claude" / "settings.json").write_text(body)
    code, output = invoke_doctor(wired, monkeypatch)
    assert code == 1, label
    assert "🔴" in output, output
    assert "all guarantees hold" not in output
    assert "Traceback" not in output


def test_unreadable_config_file_fails_rather_than_crashing(wired: Path):
    """A directory where a file is expected, and a file with no read permission."""
    path = wired / ".claude" / "settings.json"
    path.unlink()
    path.mkdir()
    check = named(health.hook_checks(wired), CHECK_PRE_REG)
    assert not check.ok
    assert check.detail.strip()


def test_permission_denied_config_fails_rather_than_crashing(wired: Path):
    path = wired / ".claude" / "settings.json"
    path.chmod(0o000)
    try:
        check = named(health.hook_checks(wired), CHECK_PRE_REG)
        assert not check.ok
        assert check.detail.strip()
    finally:
        path.chmod(0o644)


# ══════════════════════════════════════════════════════════════════════════════
# Partial structural damage is not a partial pass
# ══════════════════════════════════════════════════════════════════════════════
def corrupt_alongside_the_valid_entry(project: Path, corruption: object) -> None:
    """Append a malformed entry to PreToolUse, leaving the real registration intact."""
    path = project / ".claude" / "settings.json"
    config = json.loads(path.read_text())
    config["hooks"]["PreToolUse"].append(corruption)
    path.write_text(json.dumps(config, indent=2))


DAMAGE_ALONGSIDE_VALID = [
    ("entry is a string", "Write|Edit"),
    ("entry is a number", 7),
    ("handlers is a string", {"matcher": "Write|Edit", "hooks": "cmd"}),
    ("handler is a string", {"matcher": "Write|Edit", "hooks": ["cmd"]}),
    ("command is a number",
     {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": 9}]}),
    ("command is null",
     {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": None}]}),
]


@pytest.mark.parametrize(("label", "corruption"), DAMAGE_ALONGSIDE_VALID,
                         ids=[case[0] for case in DAMAGE_ALONGSIDE_VALID])
def test_damage_beside_a_valid_registration_still_fails(wired: Path, label: str,
                                                        corruption: object):
    """A valid entry must not launder a malformed one.

    The parser skips what it cannot read, so a configuration that is partly the wrong shape
    yields a green row while an unknown fraction of it is being ignored — and the skipped
    entry may be the one the client actually honours.
    """
    corrupt_alongside_the_valid_entry(wired, corruption)
    check = named(health.hook_checks(wired), CHECK_PRE_REG)
    assert not check.ok, label
    assert "malformed" in check.detail.lower(), check.detail


def test_damage_names_what_could_not_be_read(wired: Path):
    corrupt_alongside_the_valid_entry(wired, 7)
    detail = named(health.hook_checks(wired), CHECK_PRE_REG).detail
    assert "PreToolUse[" in detail, detail
    assert "int" in detail, detail


@pytest.mark.parametrize(("label", "corruption"), DAMAGE_ALONGSIDE_VALID,
                         ids=[case[0] for case in DAMAGE_ALONGSIDE_VALID])
def test_doctor_reports_partial_damage_to_the_operator(wired: Path, monkeypatch,
                                                       label: str, corruption: object):
    db.init(wired / "registry.sqlite")
    corrupt_alongside_the_valid_entry(wired, corruption)
    code, output = invoke_doctor(wired, monkeypatch)
    assert code == 1, label
    assert "🔴" in output
    assert "Traceback" not in output


def test_damage_under_an_unrelated_event_does_not_fail_the_preventive_row(wired: Path):
    """Scope. Corruption under SessionStart says nothing about what runs on a write."""
    path = wired / ".claude" / "settings.json"
    config = json.loads(path.read_text())
    config["hooks"]["SessionStart"] = ["not an entry"]
    path.write_text(json.dumps(config, indent=2))
    assert named(health.hook_checks(wired), CHECK_PRE_REG).ok


# ══════════════════════════════════════════════════════════════════════════════
# The command must INVOKE this checkout's executable
# ══════════════════════════════════════════════════════════════════════════════
def test_moved_checkout_with_absolute_paths_is_detected(tmp_path: Path):
    """The regression that matters. A configuration committed with absolute command paths
    survives a clone or a move looking perfectly healthy: the file is there and the filename
    appears in the JSON. Only the command still points at the old location, so nothing fires
    and nothing says so."""
    original = tmp_path / "original"
    shutil.copytree(REPO / ".claude", original / ".claude")

    # the legacy shape: absolute paths baked in at the original location
    config_path = original / ".claude" / "settings.json"
    config = json.loads(config_path.read_text())
    for entries in config["hooks"].values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                name = Path(hook["command"].strip('"')).name
                hook["command"] = str(original / ".claude" / "hooks" / name)
    config_path.write_text(json.dumps(config, indent=2))
    assert named(health.hook_checks(original), CHECK_PRE_REG).ok, "healthy in place"

    moved = tmp_path / "moved"
    shutil.copytree(original, moved)

    check = named(health.hook_checks(moved), CHECK_PRE_REG)
    assert not check.ok, "an absolute path from another checkout must not count"
    assert "does not resolve to this checkout" in check.detail
    assert "CLAUDE_PROJECT_DIR" in check.detail, "must say how to fix it"


def test_checkout_relative_config_survives_relocation(tmp_path: Path):
    """The counterpart: the form actually committed keeps working after a move."""
    moved = tmp_path / "elsewhere"
    shutil.copytree(REPO / ".claude", moved / ".claude")
    assert named(health.hook_checks(moved), CHECK_PRE_REG).ok


def test_command_pointing_at_another_checkout_is_rejected(wired: Path, tmp_path: Path):
    other = tmp_path / "other-checkout" / ".claude" / "hooks"
    other.mkdir(parents=True)
    target = other / PRE_HOOK
    target.write_text("#!/usr/bin/env python3\n")
    target.chmod(0o755)
    rewrite_command(wired, str(target))
    assert not named(health.hook_checks(wired), CHECK_PRE_REG).ok


def test_correct_filename_in_a_nonexistent_directory_is_rejected(wired: Path):
    rewrite_command(wired, f"/no/such/dir/{PRE_HOOK}")
    check = named(health.hook_checks(wired), CHECK_PRE_REG)
    assert not check.ok
    assert "does not" in check.detail.lower() or "resolve" in check.detail.lower()


def test_non_executable_target_is_rejected(wired: Path):
    (wired / ".claude" / "hooks" / PRE_HOOK).chmod(0o644)
    check = named(health.hook_checks(wired), CHECK_PRE_REG)
    assert not check.ok
    assert "executable" in check.detail.lower()


def test_unrelated_substring_is_rejected(wired: Path):
    """`...pre.py.bak` contains the filename but is a different file."""
    backup = wired / ".claude" / "hooks" / f"{PRE_HOOK}.bak"
    backup.write_text("#!/usr/bin/env python3\n")
    backup.chmod(0o755)
    rewrite_command(wired, str(backup))
    assert not named(health.hook_checks(wired), CHECK_PRE_REG).ok


def test_unparseable_command_quoting_is_rejected(wired: Path):
    rewrite_command(wired, f'python3 "{PRE_HOOK}')      # unbalanced quote
    check = named(health.hook_checks(wired), CHECK_PRE_REG)
    assert not check.ok
    assert check.detail.strip()


# ── position, not presence ──────────────────────────────────────────────────────
NOT_AN_INVOCATION = [
    ("flag value", "/usr/bin/python3 /some/other/script.py --hook {hook}"),
    ("long flag value", "/usr/bin/python3 /other.py --hook={hook}"),
    ("trailing argument", "/usr/bin/python3 /other.py {hook}"),
    ("read, not run", "/bin/cat {hook}"),
    ("interpreter running a module", "/usr/bin/python3 -m runner {hook}"),
    ("interpreter running inline code", "/usr/bin/python3 -c 'pass' {hook}"),
    ("mentioned in an echo", "/bin/echo running {hook}"),
    # The hook is the VALUE of an option that consumes one, so python reads from stdin
    # and the hook is never executed.
    ("consumed as a warning filter", "/usr/bin/python3 -W {hook}"),
    ("consumed as an -X value", "/usr/bin/python3 -X {hook}"),
]


@pytest.mark.parametrize(("label", "template"), NOT_AN_INVOCATION,
                         ids=[case[0] for case in NOT_AN_INVOCATION])
def test_filename_not_in_a_running_position_is_rejected(wired: Path, label: str,
                                                        template: str):
    """The filename appears, but nothing would execute it."""
    hook = wired / ".claude" / "hooks" / PRE_HOOK
    rewrite_command(wired, template.format(hook=hook))
    check = named(health.hook_checks(wired), CHECK_PRE_REG)
    assert not check.ok, label
    assert "not in a position that runs it" in check.detail, check.detail


INVOCATIONS = [
    ("the hook is the executable", "{hook}"),
    ("quoted executable", "'{hook}'"),
    ("python3 runs it", "/usr/bin/python3 {hook}"),
    ("bare python3 runs it", "python3 {hook}"),
    ("versioned interpreter runs it", "/usr/bin/python3.12 {hook}"),
    ("interpreter with a switch", "/usr/bin/python3 -u {hook}"),
    ("warning filter before the script", "/usr/bin/python3 -W ignore {hook}"),
    ("implementation option before the script", "/usr/bin/python3 -X dev {hook}"),
    ("hash-based pyc option before the script",
     "/usr/bin/python3 --check-hash-based-pycs always {hook}"),
    ("several value-consuming options", "/usr/bin/python3 -W ignore -X dev -u {hook}"),
    ("env chooses the interpreter", "/usr/bin/env python3 {hook}"),
    ("env with an assignment", "/usr/bin/env PYTHONPATH=src python3 {hook}"),
]


@pytest.mark.parametrize(("label", "template"), INVOCATIONS,
                         ids=[case[0] for case in INVOCATIONS])
def test_real_invocation_shapes_are_accepted(wired: Path, label: str, template: str):
    hook = wired / ".claude" / "hooks" / PRE_HOOK
    rewrite_command(wired, template.format(hook=hook))
    check = named(health.hook_checks(wired), CHECK_PRE_REG)
    assert check.ok, f"{label}: {check.detail}"


def test_claude_project_dir_placeholder_is_accepted(wired: Path):
    rewrite_command(wired, "${CLAUDE_PROJECT_DIR}/.claude/hooks/" + PRE_HOOK)
    assert named(health.hook_checks(wired), CHECK_PRE_REG).ok


def test_git_toplevel_substitution_is_accepted(wired: Path):
    """The other checkout-relative form a shell would expand."""
    rewrite_command(
        wired,
        f'/usr/bin/python3 "$(git rev-parse --show-toplevel)/.claude/hooks/{PRE_HOOK}"',
    )
    assert named(health.hook_checks(wired), CHECK_PRE_REG).ok


def test_plain_absolute_path_into_this_checkout_is_accepted(wired: Path):
    rewrite_command(wired, str(wired / ".claude" / "hooks" / PRE_HOOK))
    assert named(health.hook_checks(wired), CHECK_PRE_REG).ok


def test_value_consuming_option_without_a_value_finds_no_script():
    """`python3 -W` with nothing after it runs no script. Never guess one."""
    assert health._script_argument(["-W"]) is None
    assert health._script_argument(["-X"]) is None
    assert health._script_argument(["--check-hash-based-pycs"]) is None


def test_value_consuming_options_do_not_hide_the_script():
    """The option value is skipped; the script after it is still found."""
    assert health._script_argument(["-W", "ignore", "hook.py"]) == "hook.py"
    assert health._script_argument(["-X", "dev", "-u", "hook.py"]) == "hook.py"
