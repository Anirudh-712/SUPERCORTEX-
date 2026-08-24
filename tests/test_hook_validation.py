"""The preventive fabricated-number validator (ADR-013).

Written before the implementation. The guarantee under test is not "bad content is
reported" but "bad content never reaches disk", so the end-to-end cases assert file
*absence* after a denial, not just the decision object.

Fail-closed is the other half: anything watched that cannot be identified, reconstructed
or checked must deny with a diagnostic naming what could not be resolved. A blanket
failure would make a hook bug indistinguishable from a genuine catch.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from supercortex.validation import numbers as val

REPO = Path(__file__).resolve().parents[1]
PRE_HOOK = REPO / ".claude" / "hooks" / "no_fabricated_numbers_pre.py"
POST_HOOK = REPO / ".claude" / "hooks" / "no_fabricated_numbers.py"

METRICS = {
    "trades": {"profit_factor_gross": 1.259726, "n_trades": 386, "win_rate": 0.463731},
    "four_numbers": {"gross": 48458.98, "net of costs": 13557.26},
    "detail": {"tds_withheld": 205646.88},
}


# ── fixture project ─────────────────────────────────────────────────────────────
@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A miniature repo: registry, one sealed trial, one artifact, one document."""
    exp = tmp_path / "02-experiments" / "EXP-000001-probe"
    (exp / "artifacts").mkdir(parents=True)
    (exp / "artifacts" / "metrics.json").write_text(json.dumps(METRICS), encoding="utf-8")
    (tmp_path / "10-reports").mkdir()
    (tmp_path / "src" / "supercortex").mkdir(parents=True)

    conn = sqlite3.connect(tmp_path / "registry.sqlite")
    conn.executescript(
        """
        CREATE TABLE trial (
            experiment_id TEXT, seq INTEGER, outcome TEXT, metrics_path TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO trial VALUES ('EXP-000001', 1, 'ok', ?)",
        ("02-experiments/EXP-000001-probe/artifacts/metrics-trial-0001.json",),
    )
    conn.commit()
    conn.close()
    shutil.copy(
        exp / "artifacts" / "metrics.json",
        exp / "artifacts" / "metrics-trial-0001.json",
    )
    return tmp_path


def doc(project: Path) -> Path:
    return project / "02-experiments" / "EXP-000001-probe" / "results.md"


def decide(project: Path, tool: str, **tool_input) -> val.Decision:
    return val.decide(project, tool, tool_input)


# ── path classification ─────────────────────────────────────────────────────────
def test_source_file_is_outside_and_allowed_immediately(project: Path):
    target = project / "src" / "supercortex" / "engine.py"
    d = decide(project, "Write", file_path=str(target), content="sharpe = 9.99")
    assert d.allow
    assert d.classification is val.PathClass.OUTSIDE


def test_markdown_outside_watched_directories_is_allowed(project: Path):
    target = project / "21-learnings" / "LRN-000001.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    assert decide(project, "Write", file_path=str(target), content="Sharpe 9.99").allow


def test_non_markdown_inside_a_watched_directory_is_allowed(project: Path):
    target = project / "02-experiments" / "EXP-000001-probe" / "config.yaml"
    assert decide(project, "Write", file_path=str(target), content="seed: 4242").allow


def test_path_outside_the_project_is_allowed(project: Path, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere") / "notes.md"
    assert decide(project, "Write", file_path=str(other), content="9.99").allow


# ── fail closed: path cannot be determined ──────────────────────────────────────
@pytest.mark.parametrize("tool_input", [
    {},
    {"content": "Sharpe 9.99"},
    {"file_path": "", "content": "x"},
    {"file_path": None, "content": "x"},
    {"file_path": 42, "content": "x"},
])
def test_undeterminable_path_is_denied_with_a_diagnostic(project: Path, tool_input):
    d = val.decide(project, "Write", tool_input)
    assert not d.allow
    assert d.classification is val.PathClass.UNDETERMINED
    assert "file_path" in d.reason


def test_malformed_payload_denial_names_the_missing_field(project: Path):
    d = val.decide(project, "Write", {})
    assert "file_path" in d.reason
    assert d.reason != "validation failed"          # must be specific, not blanket


# ── Write ───────────────────────────────────────────────────────────────────────
def test_write_with_supported_numbers_is_allowed(project: Path):
    content = "Profit factor 1.259726 over 386 trades. Gross 48,458.98."
    assert decide(project, "Write", file_path=str(doc(project)), content=content).allow


def test_write_with_a_fabricated_decimal_is_denied(project: Path):
    d = decide(project, "Write", file_path=str(doc(project)),
               content="Sharpe was 2.71.")
    assert not d.allow
    assert "2.71" in d.unsupported


def test_write_with_a_fabricated_percentage_is_denied(project: Path):
    d = decide(project, "Write", file_path=str(doc(project)),
               content="Return was 37.4% for the period.")
    assert not d.allow
    assert "37.4" in d.unsupported


def test_write_with_a_fabricated_ratio_is_denied(project: Path):
    d = decide(project, "Write", file_path=str(doc(project)),
               content="A payoff ratio of 3.25 to 1.")
    assert not d.allow
    assert "3.25" in d.unsupported


def test_write_with_a_fabricated_large_integer_is_denied(project: Path):
    d = decide(project, "Write", file_path=str(doc(project)),
               content="It executed 91827 orders.")
    assert not d.allow
    assert "91827" in d.unsupported


def test_rounded_value_of_a_real_artifact_number_is_allowed(project: Path):
    """1.259726 rounds to 1.26 — supported, because it derives from the artifact."""
    assert decide(project, "Write", file_path=str(doc(project)),
                  content="Profit factor 1.26.").allow


def test_fraction_written_as_a_percentage_is_allowed(project: Path):
    """win_rate 0.463731 presented as 46.3731% is the same evidence."""
    assert decide(project, "Write", file_path=str(doc(project)),
                  content="Win rate 46.3731%.").allow


def test_a_number_close_but_not_derived_is_denied(project: Path):
    """1.27 is near 1.259726 but is not a rounding of it. Near-misses are fabrications."""
    d = decide(project, "Write", file_path=str(doc(project)), content="Profit factor 1.27.")
    assert not d.allow


# ── exemptions preserved ────────────────────────────────────────────────────────
def test_frontmatter_is_exempt(project: Path):
    content = "---\nid: EXP-000001\nsharpe: 9.9999\n---\n\nProfit factor 1.259726.\n"
    assert decide(project, "Write", file_path=str(doc(project)), content=content).allow


def test_fenced_code_is_exempt(project: Path):
    content = "Config:\n\n```yaml\nmin_sharpe: 8.8888\n```\n"
    assert decide(project, "Write", file_path=str(doc(project)), content=content).allow


def test_inline_code_is_exempt(project: Path):
    assert decide(project, "Write", file_path=str(doc(project)),
                  content="Threshold is `0.35789` in the config.").allow


def test_iso_dates_are_exempt(project: Path):
    assert decide(project, "Write", file_path=str(doc(project)),
                  content="Sealed 2026-08-24 17:44:02 after review.").allow


def test_immutable_ids_are_exempt(project: Path):
    assert decide(project, "Write", file_path=str(doc(project)),
                  content="See EXP-000042 and DEC-000019 for context.").allow


def test_version_strings_are_exempt(project: Path):
    assert decide(project, "Write", file_path=str(doc(project)),
                  content="Built with nautilus v1.231.0 and duckdb 1.5.5.").allow


def test_urls_are_exempt(project: Path):
    assert decide(project, "Write", file_path=str(doc(project)),
                  content="Source https://example.com/a/9.8765/b").allow


def test_html_comments_are_exempt(project: Path):
    assert decide(project, "Write", file_path=str(doc(project)),
                  content="<!-- GENERATED 7.7777 -->\n\nProfit factor 1.259726.").allow


def test_years_are_not_result_shaped(project: Path):
    assert decide(project, "Write", file_path=str(doc(project)),
                  content="Covering 2020 through 2026 inclusive.").allow


# ── Edit ────────────────────────────────────────────────────────────────────────
def test_edit_with_supported_replacement_is_allowed(project: Path):
    target = doc(project)
    target.write_text("Profit factor PLACEHOLDER over 386 trades.\n", encoding="utf-8")
    assert decide(project, "Edit", file_path=str(target),
                  old_string="PLACEHOLDER", new_string="1.259726").allow


def test_edit_introducing_a_fabricated_number_is_denied(project: Path):
    target = doc(project)
    target.write_text("Profit factor PLACEHOLDER.\n", encoding="utf-8")
    d = decide(project, "Edit", file_path=str(target),
               old_string="PLACEHOLDER", new_string="4.44")
    assert not d.allow
    assert "4.44" in d.unsupported


def test_edit_validates_the_whole_document_not_just_the_fragment(project: Path):
    """The fabrication sits OUTSIDE the replaced region. Validating the fragment alone
    would pass it."""
    target = doc(project)
    target.write_text("Sharpe was 8.6421.\n\nProfit factor PLACEHOLDER.\n", encoding="utf-8")
    d = decide(project, "Edit", file_path=str(target),
               old_string="PLACEHOLDER", new_string="1.259726")
    assert not d.allow
    assert "8.6421" in d.unsupported


def test_edit_removing_the_only_fabrication_is_allowed(project: Path):
    target = doc(project)
    target.write_text("Sharpe was 8.6421.\n", encoding="utf-8")
    assert decide(project, "Edit", file_path=str(target),
                  old_string="8.6421", new_string="1.259726").allow


def test_edit_with_an_absent_old_string_is_denied_as_impossible(project: Path):
    target = doc(project)
    target.write_text("Profit factor 1.259726.\n", encoding="utf-8")
    d = decide(project, "Edit", file_path=str(target),
               old_string="NOT PRESENT", new_string="x")
    assert not d.allow
    assert "does not occur" in d.reason


def test_edit_with_an_ambiguous_old_string_is_denied(project: Path):
    """Two occurrences and replace_all unset: the prospective document is not knowable."""
    target = doc(project)
    target.write_text("X and X again.\n", encoding="utf-8")
    d = decide(project, "Edit", file_path=str(target), old_string="X", new_string="y")
    assert not d.allow
    assert "ambiguous" in d.reason.lower()
    assert "2" in d.reason                          # names the occurrence count


def test_replace_all_reconstructs_every_occurrence(project: Path):
    target = doc(project)
    target.write_text("N trades and N trades.\n", encoding="utf-8")
    merged = val.reconstruct_edit("N trades and N trades.\n", "N", "386", True)
    assert merged == "386 trades and 386 trades.\n"
    assert decide(project, "Edit", file_path=str(target),
                  old_string="N", new_string="386", replace_all=True).allow


def test_replace_all_denies_when_any_occurrence_yields_a_fabrication(project: Path):
    target = doc(project)
    target.write_text("A and A.\n", encoding="utf-8")
    d = decide(project, "Edit", file_path=str(target),
               old_string="A", new_string="5.55", replace_all=True)
    assert not d.allow
    assert "5.55" in d.unsupported


def test_edit_on_a_missing_file_is_denied(project: Path):
    d = decide(project, "Edit", file_path=str(doc(project)),
               old_string="a", new_string="b")
    assert not d.allow
    assert "cannot be read" in d.reason or "does not exist" in d.reason


def test_edit_with_empty_old_string_is_denied(project: Path):
    target = doc(project)
    target.write_text("text\n", encoding="utf-8")
    d = decide(project, "Edit", file_path=str(target), old_string="", new_string="9.99")
    assert not d.allow


def test_edit_outside_watched_paths_skips_reconstruction_entirely(project: Path):
    """Row one of the ADR table: allowed immediately, no file read required."""
    target = project / "src" / "supercortex" / "nonexistent.py"
    assert decide(project, "Edit", file_path=str(target),
                  old_string="absent", new_string="9.99").allow


# ── fail closed: registry unavailable ───────────────────────────────────────────
def test_missing_registry_denies_a_watched_write(project: Path):
    (project / "registry.sqlite").unlink()
    d = decide(project, "Write", file_path=str(doc(project)), content="Sharpe 1.259726.")
    assert not d.allow
    assert "registry" in d.reason.lower()


def test_corrupt_registry_denies_a_watched_write(project: Path):
    (project / "registry.sqlite").write_bytes(b"not a database")
    d = decide(project, "Write", file_path=str(doc(project)), content="Sharpe 1.259726.")
    assert not d.allow
    assert "registry" in d.reason.lower()


def test_missing_registry_does_not_deny_a_document_with_no_numbers(project: Path):
    """Nothing to fabricate means nothing to verify. Denying here would be noise."""
    (project / "registry.sqlite").unlink()
    assert decide(project, "Write", file_path=str(doc(project)),
                  content="A paragraph of prose with no evidence in it.").allow


def test_missing_registry_does_not_deny_an_unwatched_write(project: Path):
    (project / "registry.sqlite").unlink()
    target = project / "src" / "supercortex" / "engine.py"
    assert decide(project, "Write", file_path=str(target), content="x = 9.99").allow


def test_artifact_not_referenced_by_a_sealed_trial_is_not_trusted(project: Path):
    """Provenance, not proximity: a stray JSON cannot launder a number."""
    stray = project / "02-experiments" / "EXP-000001-probe" / "artifacts" / "stray.json"
    stray.write_text(json.dumps({"sharpe": 2.71}), encoding="utf-8")
    d = decide(project, "Write", file_path=str(doc(project)), content="Sharpe 2.71.")
    assert not d.allow


def test_errored_trial_artifacts_are_not_trusted(project: Path):
    conn = sqlite3.connect(project / "registry.sqlite")
    conn.execute(
        "INSERT INTO trial VALUES ('EXP-000001', 2, 'error', ?)",
        ("02-experiments/EXP-000001-probe/artifacts/bad.json",),
    )
    conn.commit()
    conn.close()
    (project / "02-experiments" / "EXP-000001-probe" / "artifacts" / "bad.json").write_text(
        json.dumps({"sharpe": 6.66}), encoding="utf-8"
    )
    d = decide(project, "Write", file_path=str(doc(project)), content="Sharpe 6.66.")
    assert not d.allow


# ── end to end: the hook as Claude Code invokes it ──────────────────────────────
def run_hook(hook: Path, project: Path, tool: str, tool_input: dict) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": tool, "tool_input": tool_input,
                          "hook_event_name": "PreToolUse"})
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project),
           "PYTHONPATH": str(REPO / "src")}
    return subprocess.run([sys.executable, str(hook)], input=payload,
                          capture_output=True, text=True, env=env)


def hook_decision(result: subprocess.CompletedProcess) -> str:
    if not result.stdout.strip():
        return "allow"
    return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]


def test_denied_write_never_creates_the_file(project: Path):
    """The guarantee. Not 'reported after the fact' — the file must not exist."""
    target = doc(project)
    assert not target.exists()
    result = run_hook(PRE_HOOK, project, "Write",
                      {"file_path": str(target), "content": "Sharpe 2.71."})
    assert hook_decision(result) == "deny"
    assert not target.exists(), "PreToolUse denial must precede file creation"


def test_denied_edit_never_changes_the_file(project: Path):
    target = doc(project)
    original = "Profit factor 1.259726.\n"
    target.write_text(original, encoding="utf-8")
    result = run_hook(PRE_HOOK, project, "Edit",
                      {"file_path": str(target), "old_string": "1.259726",
                       "new_string": "9.99"})
    assert hook_decision(result) == "deny"
    assert target.read_text(encoding="utf-8") == original


def test_hook_allows_a_legitimate_write(project: Path):
    result = run_hook(PRE_HOOK, project, "Write",
                      {"file_path": str(doc(project)), "content": "Profit factor 1.259726."})
    assert hook_decision(result) == "allow"
    assert result.returncode == 0


def test_hook_allows_an_unrelated_source_write(project: Path):
    target = project / "src" / "supercortex" / "engine.py"
    result = run_hook(PRE_HOOK, project, "Write",
                      {"file_path": str(target), "content": "THRESHOLD = 3.14159"})
    assert hook_decision(result) == "allow"


def test_hook_denies_unparseable_stdin(project: Path):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project),
           "PYTHONPATH": str(REPO / "src")}
    result = subprocess.run([sys.executable, str(PRE_HOOK)], input="{not json",
                            capture_output=True, text=True, env=env)
    assert hook_decision(result) == "deny"
    assert "payload" in result.stdout.lower()


def test_hook_reason_is_specific_not_blanket(project: Path):
    result = run_hook(PRE_HOOK, project, "Write",
                      {"file_path": str(doc(project)), "content": "Sharpe 2.71."})
    reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "2.71" in reason
    assert len(reason) > 40


# ── PostToolUse still works, on the same shared code ────────────────────────────
def test_post_hook_still_blocks_a_written_fabrication(project: Path):
    target = doc(project)
    target.write_text("Sharpe was 2.71.\n", encoding="utf-8")
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target)},
                          "hook_event_name": "PostToolUse"})
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project),
           "PYTHONPATH": str(REPO / "src")}
    result = subprocess.run([sys.executable, str(POST_HOOK)], input=payload,
                            capture_output=True, text=True, env=env)
    assert result.returncode == 2
    assert "2.71" in result.stderr


def test_post_hook_allows_a_clean_document(project: Path):
    target = doc(project)
    target.write_text("Profit factor 1.259726.\n", encoding="utf-8")
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target)},
                          "hook_event_name": "PostToolUse"})
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project),
           "PYTHONPATH": str(REPO / "src")}
    result = subprocess.run([sys.executable, str(POST_HOOK)], input=payload,
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0


def test_both_hooks_share_one_implementation():
    """No duplicated logic: neither hook may define its own extraction or validation."""
    for hook in (PRE_HOOK, POST_HOOK):
        source = hook.read_text(encoding="utf-8")
        assert "supercortex.validation" in source
        assert "def is_result_shaped" not in source
        assert "def supported" not in source
        assert "def trusted_artifacts" not in source


# ══════════════════════════════════════════════════════════════════════════════
# REGRESSION: headline laundering
# ══════════════════════════════════════════════════════════════════════════════
def test_modified_headline_cannot_authorize_a_fabricated_number(project: Path):
    """artifacts/metrics.json is supposed to be a COPY of a registered trial artifact.

    Trusting it because it exists makes it an independent source of authority, and any
    write to it launders arbitrary values. It must carry no evidence of its own.
    """
    artifacts = project / "02-experiments" / "EXP-000001-probe" / "artifacts"
    # the registered, sealed trial says 1.259726 — unchanged
    registered = json.loads((artifacts / "metrics-trial-0001.json").read_text())
    assert registered["trades"]["profit_factor_gross"] == 1.259726
    # someone edits ONLY the headline copy
    (artifacts / "metrics.json").write_text(
        json.dumps({"trades": {"profit_factor_gross": 8.888888}}), encoding="utf-8"
    )
    d = decide(project, "Write", file_path=str(doc(project)),
               content="Profit factor 8.888888.")
    assert not d.allow, "a modified headline must not authorize a number"
    assert "8.888888" in d.unsupported


def test_headline_removal_does_not_break_legitimate_quoting(project: Path):
    """No legitimate loss: the headline is a copy, so its values live in the registered
    artifact anyway and still validate."""
    (project / "02-experiments" / "EXP-000001-probe" / "artifacts" / "metrics.json").unlink()
    assert decide(project, "Write", file_path=str(doc(project)),
                  content="Profit factor 1.259726.").allow


def test_only_registered_trial_paths_are_trusted(project: Path):
    artifacts = project / "02-experiments" / "EXP-000001-probe" / "artifacts"
    trusted = val.trusted_artifacts(project, "02-experiments/EXP-000001-probe/results.md")
    assert trusted == {artifacts / "metrics-trial-0001.json"}


# ══════════════════════════════════════════════════════════════════════════════
# REGRESSION: malformed top-level payloads must not crash
# ══════════════════════════════════════════════════════════════════════════════
MALFORMED = ["[]", '["02-experiments/x.md"]', "null", '"a string"', "42"]


def run_raw(hook: Path, project: Path, raw: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project),
           "PYTHONPATH": str(REPO / "src")}
    return subprocess.run([sys.executable, str(hook)], input=raw,
                          capture_output=True, text=True, env=env)


@pytest.mark.parametrize("raw", MALFORMED)
def test_pre_hook_denies_malformed_toplevel_without_traceback(project: Path, raw: str):
    result = run_raw(PRE_HOOK, project, raw)
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 0
    assert hook_decision(result) == "deny"
    reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert len(reason) > 30
    assert "fail-closed" in reason.lower()


@pytest.mark.parametrize("raw", MALFORMED)
def test_post_hook_reports_malformed_toplevel_without_traceback(project: Path, raw: str):
    """PostToolUse cannot deny, but it must not silently allow either."""
    result = run_raw(POST_HOOK, project, raw)
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 2
    assert len(result.stderr.strip()) > 30


def test_pre_hook_denial_names_the_shape_it_could_not_read(project: Path):
    result = run_raw(PRE_HOOK, project, "[]")
    reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "list" in reason.lower() or "object" in reason.lower()


def test_both_hooks_still_allow_a_readable_outside_path(project: Path):
    """Scoped blast radius: malformed handling must not make outside writes noisy."""
    target = project / "src" / "supercortex" / "engine.py"
    payload = json.dumps({"tool_name": "Write",
                          "tool_input": {"file_path": str(target), "content": "K = 9.99"}})
    pre = run_raw(PRE_HOOK, project, payload)
    assert hook_decision(pre) == "allow"
    post = run_raw(POST_HOOK, project, payload)
    assert post.returncode == 0
    assert post.stderr.strip() == ""


def test_post_hook_reports_when_path_cannot_be_determined(project: Path):
    payload = json.dumps({"tool_name": "Write", "tool_input": {"content": "9.99"}})
    result = run_raw(POST_HOOK, project, payload)
    assert result.returncode == 2
    assert "file_path" in result.stderr


# ══════════════════════════════════════════════════════════════════════════════
# The Codex hook layer must not drift from the Claude one
# ══════════════════════════════════════════════════════════════════════════════
CODEX_HOOKS = REPO / ".codex" / "hooks"


@pytest.mark.parametrize("name", [
    "guard_bash.py", "guard_write.py",
    "no_fabricated_numbers.py", "no_fabricated_numbers_pre.py",
])
def test_codex_mirror_is_byte_identical(name: str):
    """A stale copy of a security hook is worse than no copy: whichever tool runs the
    old one silently gets the old, weaker guarantees."""
    claude = (REPO / ".claude" / "hooks" / name)
    codex = CODEX_HOOKS / name
    assert codex.exists(), f"{name} missing from the Codex mirror"
    assert codex.read_bytes() == claude.read_bytes(), f"{name} has drifted"


def test_codex_registers_the_preventive_hook():
    config = json.loads((REPO / ".codex" / "hooks.json").read_text())
    commands = [
        h["command"]
        for entry in config["hooks"].get("PreToolUse", [])
        for h in entry["hooks"]
    ]
    assert any("no_fabricated_numbers_pre.py" in c for c in commands)
