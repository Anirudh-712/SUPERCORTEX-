"""The on-demand cockpit: `supercortex dash render`.

Three properties carry this module, and each has a specific failure it exists to prevent.

**Determinism.** A dashboard that changes when nothing changed teaches you to ignore it.
So no clock is ever read: the fingerprint is a digest of the collected snapshot, and the
same snapshot must render byte-identically forever.

**Atomic visibility.** A half-written dashboard is worse than a stale one, because it looks
current. Publication is a symlink swap over a completed immutable generation. The guarantee
is namespace visibility under normal process interruption — *not* power-loss durability,
which would require fsync of files and directories and is not implemented here.

**No fabrication.** Every rendered value is asserted against a distinctive fixture value,
not against a regex hoping a number is traceable. Unknown state renders as prose naming
what is unknown, never as `0`, `N/A`, `TBD`, or a dash standing in for a value.

Everything here runs against a temporary repository, registry, dataset and dashboard root.
The real `00-dashboard/`, `registry.sqlite` and `data/` are never touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from supercortex import dashboard
from supercortex.registry import db

# Distinctive values: if one of these appears in output, it came from the fixture and
# nowhere else. Round numbers would be indistinguishable from an invented default.
EXP_ID = "EXP-000777"
EXP_TITLE = "Zebra funding carry probe"
EXP_BUDGET = 43
DATA_ID = "DATA-000912"
DATA_TITLE = "Quokka perp mark series"
DATA_ROWS = 918_273
DATA_SYMBOL = "QQQUSDT"
DATA_INTERVAL = "7m"


# ── fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A temp repo whose hooks pass, so a quiet registry really can render quiet.

    Without this every render reports failed health checks — correctly, since a bare temp
    directory has no hooks — and the empty-attention state would be unobservable.
    """
    root = tmp_path / "repo"
    (root / "00-dashboard").mkdir(parents=True)
    hooks = root / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    for name in ("no_fabricated_numbers_pre.py", "no_fabricated_numbers.py"):
        hook = hooks / name
        hook.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8")
        hook.chmod(0o755)
    (root / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {
            event: [{"matcher": "Write|Edit", "hooks": [{
                "type": "command",
                "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/" + script,
            }]}]
            for event, script in (("PreToolUse", "no_fabricated_numbers_pre.py"),
                                  ("PostToolUse", "no_fabricated_numbers.py"))
        }
    }), encoding="utf-8")
    return root


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    return db.init(tmp_path / "registry.sqlite")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def add_experiment(
    registry: Path, *, exp_id: str = EXP_ID, title: str = EXP_TITLE,
    status: str = "RESEARCH", budget: int = EXP_BUDGET,
) -> None:
    with connect(registry) as conn:
        conn.execute(
            "INSERT INTO experiment (id, title, slug, status, trial_budget, market, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (exp_id, title, "zebra", status, budget, "crypto", "2026-01-02T03:04:05Z"),
        )


def add_trial(registry: Path, *, exp_id: str = EXP_ID, seq: int = 1,
              outcome: str | None = "ok", metrics_path: str | None = None) -> None:
    """`trial_no_reseal` forbids UPDATE on a sealed trial, so every column is set here."""
    with connect(registry) as conn:
        conn.execute(
            "INSERT INTO trial (experiment_id, seq, config_hash, code_commit, "
            "data_snapshot, cost_model_version, seed, started_at, finished_at, outcome, "
            "metrics_path) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (exp_id, seq, "c" * 8, "d" * 7, "e" * 8, "v1", 7,
             "2026-01-02T03:04:05Z",
             None if outcome is None else "2026-01-02T04:04:05Z", outcome, metrics_path),
        )


def add_lifecycle(registry: Path, *, exp_id: str = EXP_ID, to_state: str = "BACKTESTING",
                  at: str = "2026-03-04T05:06:07Z") -> None:
    with connect(registry) as conn:
        conn.execute(
            "INSERT INTO lifecycle_event (entity_id, from_state, to_state, at) "
            "VALUES (?,?,?,?)", (exp_id, "RESEARCH", to_state, at))


def add_falsification(registry: Path, *, exp_id: str = EXP_ID,
                      test_name: str = "look-ahead", passed: int = 0,
                      status: str | None = "FAIL") -> None:
    with connect(registry) as conn:
        conn.execute(
            "INSERT INTO falsification (experiment_id, test_name, passed, ran_at, "
            "trials_at_run, status) VALUES (?,?,?,?,?,?)",
            (exp_id, test_name, passed, "2026-05-06T07:08:09Z", 3, status))


def add_dataset(registry: Path, parquet: Path | None, *, data_id: str = DATA_ID,
                sha: str | None = None, gaps: int = 0) -> None:
    with connect(registry) as conn:
        conn.execute(
            "INSERT INTO dataset (id, title, venue, symbol, interval, sha256, rows, "
            "start_ts, end_ts, gaps_json, verified_at, kind, parquet_path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (data_id, DATA_TITLE, "quokka-venue", DATA_SYMBOL, DATA_INTERVAL,
             sha or (hashlib.sha256(parquet.read_bytes()).hexdigest() if parquet else "0" * 64),
             DATA_ROWS, "2020-01-01T00:00:00+00:00", "2026-07-31T16:00:00+00:00",
             json.dumps([{"from": "a", "to": "b", "missing_bars": 5}] * gaps),
             "2026-06-07T08:09:10Z", "marks",
             str(parquet) if parquet else None),
        )


@pytest.fixture
def parquet(tmp_path: Path) -> Path:
    path = tmp_path / "lake" / "quokka.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"distinctive-parquet-bytes-for-the-cockpit-test")
    return path


def render_to(project: Path, registry: Path) -> Path:
    return dashboard.render_dashboard(registry=registry, project=project,
                                      dashboard_root=project / "00-dashboard")


def live(project: Path) -> Path:
    return project / "00-dashboard" / dashboard.LIVE_LINK


def page(project: Path, name: str) -> str:
    return (live(project) / name).read_text(encoding="utf-8")


# ── determinism ─────────────────────────────────────────────────────────────────

def test_same_snapshot_renders_byte_identically(project: Path, registry: Path) -> None:
    add_experiment(registry)
    add_trial(registry)
    snapshot = dashboard.collect(registry=registry, project=project)
    first, second = dashboard.render(snapshot), dashboard.render(snapshot)
    assert first == second
    assert snapshot.fingerprint == dashboard.collect(
        registry=registry, project=project).fingerprint


def test_two_renders_produce_identical_visible_bytes(project: Path, registry: Path) -> None:
    add_experiment(registry)
    render_to(project, registry)
    before = {p.name: p.read_bytes() for p in live(project).iterdir()}
    render_to(project, registry)
    assert {p.name: p.read_bytes() for p in live(project).iterdir()} == before


def test_changing_one_authoritative_value_changes_output_and_fingerprint(
    project: Path, registry: Path
) -> None:
    add_experiment(registry)
    before = dashboard.collect(registry=registry, project=project)
    with connect(registry) as conn:
        conn.execute("UPDATE experiment SET trial_budget = 44 WHERE id = ?", (EXP_ID,))
    after = dashboard.collect(registry=registry, project=project)

    assert before.fingerprint != after.fingerprint
    assert "43" in dashboard.render(before)["pipeline.md"]
    assert "44" in dashboard.render(after)["pipeline.md"]


def test_sqlite_row_order_cannot_affect_ordering(project: Path, registry: Path) -> None:
    """Insertion order is reversed; canonical ordering must make the output identical."""
    for exp in ("EXP-000003", "EXP-000001", "EXP-000002"):
        add_experiment(registry, exp_id=exp, title=f"title {exp}")
    forward = dashboard.render(dashboard.collect(registry=registry, project=project))

    other = db.init(registry.parent / "reordered.sqlite")
    for exp in ("EXP-000002", "EXP-000001", "EXP-000003"):
        add_experiment(other, exp_id=exp, title=f"title {exp}")
    reversed_order = dashboard.render(dashboard.collect(registry=other, project=project))

    assert forward == reversed_order
    body = forward["pipeline.md"]
    assert body.index("EXP-000001") < body.index("EXP-000002") < body.index("EXP-000003")


# ── the marker ──────────────────────────────────────────────────────────────────

def test_every_page_starts_with_the_marker_at_byte_zero(
    project: Path, registry: Path
) -> None:
    render_to(project, registry)
    for name in dashboard.PAGES:
        raw = (live(project) / name).read_bytes()
        assert raw.startswith(dashboard.MARKER.encode()), name


def test_marker_is_inside_the_guard_window(project: Path, registry: Path) -> None:
    """`guard_write.py` only inspects the first 200 bytes, so byte zero is the point."""
    render_to(project, registry)
    for name in dashboard.PAGES:
        assert "<!-- GENERATED" in (live(project) / name).read_text(encoding="utf-8")[:200]


# ── publication ─────────────────────────────────────────────────────────────────

def test_first_publication_creates_a_symlink_to_a_generation(
    project: Path, registry: Path
) -> None:
    add_experiment(registry)
    published = render_to(project, registry)
    assert live(project).is_symlink()
    assert live(project).resolve() == published.resolve()
    assert published.parent.name == dashboard.GENERATIONS_DIRNAME


def test_second_publication_over_an_existing_symlink_succeeds(
    project: Path, registry: Path
) -> None:
    """The failure the directory-rename design hid: `os.replace` onto a non-empty
    directory raises ENOTEMPTY, so a first-render-only test proves nothing."""
    add_experiment(registry)
    first = render_to(project, registry)
    add_trial(registry)
    second = render_to(project, registry)
    assert first != second
    assert live(project).is_symlink()
    assert live(project).resolve() == second.resolve()
    assert EXP_ID in page(project, "pipeline.md")


def test_symlink_is_relative(project: Path, registry: Path) -> None:
    render_to(project, registry)
    assert not Path(os.readlink(live(project))).is_absolute()


def test_a_real_directory_at_generated_fails_closed(project: Path, registry: Path) -> None:
    intruder = live(project)
    intruder.mkdir(parents=True)
    (intruder / "precious.md").write_text("not ours", encoding="utf-8")

    with pytest.raises(dashboard.DashboardRefused, match=r"(?i)not a symlink"):
        render_to(project, registry)
    assert (intruder / "precious.md").read_text(encoding="utf-8") == "not ours"
    assert intruder.is_dir() and not intruder.is_symlink()


def test_failure_after_staging_leaves_previous_generation_visible(
    project: Path, registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_experiment(registry)
    render_to(project, registry)
    before_target = os.readlink(live(project))
    before_bytes = {p.name: p.read_bytes() for p in live(project).iterdir()}

    add_trial(registry)
    monkeypatch.setattr(dashboard, "_swap_symlink",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        render_to(project, registry)

    assert os.readlink(live(project)) == before_target
    assert {p.name: p.read_bytes() for p in live(project).iterdir()} == before_bytes


def test_inconsistent_existing_generation_aborts_without_moving_the_link(
    project: Path, registry: Path
) -> None:
    """A content-addressed directory whose bytes disagree with a fresh render is a
    collision or a corruption. Reusing it silently would publish the wrong dashboard."""
    add_experiment(registry)
    first = render_to(project, registry)
    before_target = os.readlink(live(project))

    (first / "pipeline.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(dashboard.DashboardRefused, match=r"(?i)differ|inconsist"):
        render_to(project, registry)
    assert os.readlink(live(project)) == before_target


def test_no_staging_directory_survives_success(project: Path, registry: Path) -> None:
    render_to(project, registry)
    runtime = project / "00-dashboard" / dashboard.RUNTIME_DIRNAME
    assert not list(runtime.glob("staging-*"))


# ── read-only guarantees ────────────────────────────────────────────────────────

def test_registry_is_unchanged_by_rendering(project: Path, registry: Path) -> None:
    add_experiment(registry)
    add_trial(registry)
    before = hashlib.sha256(registry.read_bytes()).hexdigest()
    render_to(project, registry)
    assert hashlib.sha256(registry.read_bytes()).hexdigest() == before


def test_collect_opens_the_registry_read_only(project: Path, registry: Path) -> None:
    add_experiment(registry)
    with (dashboard._open_readonly(registry) as conn,
          pytest.raises(sqlite3.OperationalError, match=r"(?i)readonly")):
        conn.execute("UPDATE experiment SET title = 'nope'")


def test_artifact_paths_are_never_accessed(
    project: Path, registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No page needs trial metrics, so the artifact tree must not be traversed at all."""
    add_experiment(registry)
    add_trial(registry, metrics_path="artifacts/metrics-trial-0001.json")

    opened: list[str] = []
    real = Path.read_bytes

    def watched(self: Path) -> bytes:
        opened.append(str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", watched)
    render_to(project, registry)
    assert not [p for p in opened if "artifact" in p or "metrics" in p], opened


# ── snapshot correctness, asserted against distinctive values ───────────────────

def test_pipeline_reports_identity_budget_and_trial_states(
    project: Path, registry: Path
) -> None:
    add_experiment(registry)
    add_trial(registry, seq=1, outcome="ok")
    add_trial(registry, seq=2, outcome="abandoned")
    add_trial(registry, seq=3, outcome=None)
    snapshot = dashboard.collect(registry=registry, project=project)
    body = dashboard.render(snapshot)["pipeline.md"]

    assert EXP_ID in body and EXP_TITLE in body and "RESEARCH" in body
    assert str(EXP_BUDGET) in body
    row = snapshot.experiments[0]
    counts = (row.trials_consumed, row.trials_ok, row.trials_abandoned, row.trials_open)
    assert counts == (3, 1, 1, 1)


def test_pipeline_shows_latest_lifecycle_transition(project: Path, registry: Path) -> None:
    add_experiment(registry)
    add_lifecycle(registry, to_state="BACKTESTING", at="2026-03-04T05:06:07Z")
    add_lifecycle(registry, to_state="VALIDATING", at="2026-09-09T09:09:09Z")
    body = dashboard.render(dashboard.collect(registry=registry, project=project))["pipeline.md"]
    assert "VALIDATING" in body and "2026-09-09T09:09:09Z" in body
    assert "2026-03-04T05:06:07Z" not in body


def test_unknown_lifecycle_and_falsification_render_as_prose(
    project: Path, registry: Path
) -> None:
    add_experiment(registry)
    body = dashboard.render(dashboard.collect(registry=registry, project=project))["pipeline.md"]
    assert "no lifecycle transition recorded" in body
    assert "no falsification verdict recorded" in body
    for forbidden in (" N/A", " TBD", "| 0 |", " — |"):
        assert forbidden not in body, forbidden


# ── attention ───────────────────────────────────────────────────────────────────

def test_attention_lists_over_budget_unsealed_and_failed_falsification(
    project: Path, registry: Path
) -> None:
    add_experiment(registry, budget=1)
    add_trial(registry, seq=1, outcome="ok")
    add_trial(registry, seq=2, outcome=None)
    add_falsification(registry, test_name="zebra-lookahead", passed=0, status="FAIL")
    body = dashboard.render(dashboard.collect(registry=registry, project=project))["attention.md"]

    assert EXP_ID in body
    assert "over budget" in body.lower()
    assert "unsealed" in body.lower()
    assert "zebra-lookahead" in body


def test_quiet_registry_states_nothing_requires_a_human(
    project: Path, registry: Path
) -> None:
    add_experiment(registry)
    add_trial(registry, outcome="ok")
    body = dashboard.render(dashboard.collect(registry=registry, project=project))["attention.md"]
    assert "nothing requires a human" in body.lower()


# ── evidence ────────────────────────────────────────────────────────────────────

def test_evidence_reports_identity_coverage_rows_and_verification(
    project: Path, registry: Path, parquet: Path
) -> None:
    add_dataset(registry, parquet)
    body = dashboard.render(dashboard.collect(registry=registry, project=project))["evidence.md"]
    assert DATA_ID in body and DATA_TITLE in body
    assert DATA_SYMBOL in body and DATA_INTERVAL in body
    assert f"{DATA_ROWS:,}" in body
    assert "2020-01-01T00:00:00+00:00" in body
    assert "match" in body


def test_evidence_does_not_classify_staleness(
    project: Path, registry: Path, parquet: Path
) -> None:
    """No dataset-freshness threshold is configured; `data_staleness_seconds` is a
    live-tick kill-switch bound and applying it here would be meaningless."""
    add_dataset(registry, parquet)
    body = dashboard.render(dashboard.collect(registry=registry, project=project))["evidence.md"]
    assert "stale" not in body.lower()


@pytest.mark.parametrize("case", ["missing", "mismatch"])
def test_dataset_failures_appear_in_both_evidence_and_attention(
    project: Path, registry: Path, parquet: Path, case: str
) -> None:
    if case == "missing":
        add_dataset(registry, parquet)
        parquet.unlink()
        needle = "file missing"
    else:
        add_dataset(registry, parquet, sha="f" * 64)
        needle = "hash mismatch"

    pages = dashboard.render(dashboard.collect(registry=registry, project=project))
    assert needle in pages["evidence.md"].lower()
    assert DATA_ID in pages["attention.md"]
    assert needle in pages["attention.md"].lower()
    assert pages["evidence.md"].strip() != "0"


def test_gap_count_comes_from_gaps_json(project: Path, registry: Path,
                                        parquet: Path) -> None:
    add_dataset(registry, parquet, gaps=3)
    body = dashboard.render(dashboard.collect(registry=registry, project=project))["evidence.md"]
    assert "3" in body


# ── empty and broken registries ─────────────────────────────────────────────────

def test_empty_registry_renders_explicit_empty_states(
    project: Path, registry: Path
) -> None:
    pages = dashboard.render(dashboard.collect(registry=registry, project=project))
    assert "no experiments registered" in pages["pipeline.md"].lower()
    assert "no datasets registered" in pages["evidence.md"].lower()
    assert render_to(project, registry).exists()


def test_missing_registry_aborts_without_publishing(project: Path, tmp_path: Path) -> None:
    with pytest.raises(dashboard.DashboardRefused, match=r"(?i)registry"):
        dashboard.render_dashboard(registry=tmp_path / "absent.sqlite", project=project,
                                   dashboard_root=project / "00-dashboard")
    assert not live(project).exists()


def test_malformed_registry_aborts_without_publishing(
    project: Path, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.sqlite"
    broken.write_bytes(b"this is not a database")
    with pytest.raises(dashboard.DashboardRefused):
        dashboard.render_dashboard(registry=broken, project=project,
                                   dashboard_root=project / "00-dashboard")
    assert not live(project).exists()


def test_schema_drift_aborts_without_publishing(project: Path, registry: Path) -> None:
    with connect(registry) as conn:
        conn.execute("DROP VIEW experiment_trials")
    with pytest.raises(dashboard.DashboardRefused, match=r"(?i)missing|behind"):
        render_to(project, registry)
    assert not live(project).exists()


# ── git hygiene ─────────────────────────────────────────────────────────────────

# ── completeness is not shown, and cannot leak in ───────────────────────────────
# The registry stores neither `expected_rows` nor `completeness`, and its `gaps_json` is
# written as `report.gaps[:300]` — truncated, so it cannot reconstruct a missing-row total
# either. The DATA card *does* carry a completeness figure, but a card is a generated
# mirror, not an authoritative source. Reading one back would be the dashboard sourcing a
# fact from another mirror.

def write_card(project: Path, completeness: str) -> Path:
    card = project / "05-data" / f"{DATA_ID}-quokka.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text(
        "---\n"
        f"id: {DATA_ID}\n"
        "type: dataset\n"
        f"completeness: {completeness}\n"
        "---\n\n<!-- GENERATED by supercortex — do not edit -->\n",
        encoding="utf-8",
    )
    return card


def add_dataset_with_card(registry: Path, parquet: Path, card: Path) -> None:
    add_dataset(registry, parquet)
    with connect(registry) as conn:
        conn.execute("UPDATE dataset SET card_path = ? WHERE id = ?",
                     (str(card), DATA_ID))


def test_evidence_states_why_completeness_is_absent(
    project: Path, registry: Path, parquet: Path
) -> None:
    add_dataset(registry, parquet)
    body = dashboard.render(dashboard.collect(registry=registry, project=project))["evidence.md"]
    assert "completeness" in body.lower()
    assert "registry" in body.lower()
    for derived in ("1.000000", "0.99", "100%", "99."):
        assert derived not in body, derived


def test_a_conflicting_card_completeness_cannot_enter_the_dashboard(
    project: Path, registry: Path, parquet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card claiming a distinctive completeness must neither appear nor be opened."""
    card = write_card(project, "0.123456")
    add_dataset_with_card(registry, parquet, card)

    opened: list[str] = []
    real_text, real_bytes = Path.read_text, Path.read_bytes

    def watch_text(self: Path, *a: object, **k: object) -> str:
        opened.append(str(self))
        return real_text(self, *a, **k)

    def watch_bytes(self: Path) -> bytes:
        opened.append(str(self))
        return real_bytes(self)

    monkeypatch.setattr(Path, "read_text", watch_text)
    monkeypatch.setattr(Path, "read_bytes", watch_bytes)
    pages = dashboard.render(dashboard.collect(registry=registry, project=project))

    assert not [p for p in opened if p == str(card)], "the DATA card was read"
    for body in pages.values():
        assert "0.123456" not in body


def test_card_path_is_not_even_collected(project: Path, registry: Path,
                                         parquet: Path) -> None:
    """Nothing downstream can leak a card fact if the snapshot never carries the path."""
    card = write_card(project, "0.123456")
    add_dataset_with_card(registry, parquet, card)
    snapshot = dashboard.collect(registry=registry, project=project)
    assert "card" not in json.dumps(
        {k: str(v) for k, v in vars(snapshot.datasets[0]).items()}).lower()


# ── the generation address must cover what is stored, not merely the facts ──────

def test_a_renderer_change_publishes_a_new_generation_instead_of_bricking(
    project: Path, registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Addressing a directory of rendered bytes by a digest of the *facts* means any change
    to the renderer makes every stored generation look inconsistent, and the command can
    never succeed again without someone deleting `.runtime/` by hand. The address must
    therefore cover the bytes actually stored."""
    add_experiment(registry)
    first = render_to(project, registry)

    real = dashboard.render
    monkeypatch.setattr(
        dashboard, "render",
        lambda snapshot: {k: v + "\nrenderer changed\n" for k, v in real(snapshot).items()},
    )
    second = render_to(project, registry)

    assert second != first, "a changed renderer must not reuse the old generation"
    assert live(project).resolve() == second.resolve()
    assert "renderer changed" in page(project, "pipeline.md")


def test_identical_pages_reuse_the_same_generation(project: Path, registry: Path) -> None:
    add_experiment(registry)
    assert render_to(project, registry) == render_to(project, registry)


# ── every schema defect must fail closed ────────────────────────────────────────
# `db.SchemaStatus.current` is the authoritative contract: it covers unreadable
# registries, missing columns, indexes and triggers as well as missing tables. Checking a
# hand-picked subset means a registry can be structurally wrong and still produce pages
# that look authoritative.

@pytest.mark.parametrize(
    "damage, sql",
    [
        ("index", "DROP INDEX ix_trial_experiment"),
        ("trigger", "DROP TRIGGER trial_no_delete"),
        ("view", "DROP VIEW experiment_trials"),
    ],
)
def test_any_schema_defect_refuses_to_collect(
    project: Path, registry: Path, damage: str, sql: str
) -> None:
    add_experiment(registry)
    with connect(registry) as conn:
        conn.execute(sql)
    with pytest.raises(dashboard.DashboardRefused, match=r"(?i)missing|behind"):
        dashboard.collect(registry=registry, project=project)


def test_missing_column_refuses_to_collect(project: Path, registry: Path) -> None:
    """A registry rebuilt without a migrated column is not one this may read."""
    with connect(registry) as conn:
        conn.execute("ALTER TABLE dataset DROP COLUMN parquet_path")
    with pytest.raises(dashboard.DashboardRefused, match=r"(?i)parquet_path"):
        dashboard.collect(registry=registry, project=project)


def test_no_pages_are_published_for_any_schema_defect(
    project: Path, registry: Path
) -> None:
    with connect(registry) as conn:
        conn.execute("DROP INDEX ix_trial_experiment")
    with pytest.raises(dashboard.DashboardRefused):
        render_to(project, registry)
    assert not live(project).exists()


# ── an unknown gap inventory is not zero ────────────────────────────────────────

def test_null_gaps_render_as_not_recorded(project: Path, registry: Path,
                                          parquet: Path) -> None:
    add_dataset(registry, parquet)
    with connect(registry) as conn:
        conn.execute("UPDATE dataset SET gaps_json = NULL WHERE id = ?", (DATA_ID,))
    snapshot = dashboard.collect(registry=registry, project=project)
    assert snapshot.datasets[0].gap_count is None
    body = dashboard.render(snapshot)["evidence.md"]
    assert "gap inventory not recorded" in body.lower()


def test_genuine_empty_list_is_zero_not_unknown(project: Path, registry: Path,
                                                parquet: Path) -> None:
    add_dataset(registry, parquet, gaps=0)
    snapshot = dashboard.collect(registry=registry, project=project)
    assert snapshot.datasets[0].gap_count == 0


@pytest.mark.parametrize(
    "value, label",
    [("", "empty"), ("{not json", "malformed"), ('{"a": 1}', "object"), ("42", "scalar")],
)
def test_invalid_gaps_json_refuses_rather_than_rendering_zero(
    project: Path, registry: Path, parquet: Path, value: str, label: str
) -> None:
    """The defect this replaces: every one of these silently became `0`, which reads as a
    perfectly clean dataset."""
    add_dataset(registry, parquet)
    with connect(registry) as conn:
        conn.execute("UPDATE dataset SET gaps_json = ? WHERE id = ?", (value, DATA_ID))
    with pytest.raises(dashboard.DashboardRefused, match=DATA_ID):
        dashboard.collect(registry=registry, project=project)


def test_gap_line_does_not_claim_completeness_or_a_total(
    project: Path, registry: Path, parquet: Path
) -> None:
    add_dataset(registry, parquet, gaps=4)
    body = dashboard.render(dashboard.collect(registry=registry, project=project))["evidence.md"]
    assert "gap entries recorded" in body.lower()
    assert "%" not in body


# ── nullable identity fields ────────────────────────────────────────────────────

@pytest.mark.parametrize("column", ["symbol", "interval", "kind"])
def test_missing_identity_fields_render_as_explicit_phrases(
    project: Path, registry: Path, parquet: Path, column: str
) -> None:
    """`or ""` produced lines like `series:   ()`, which says nothing at all."""
    add_dataset(registry, parquet)
    with connect(registry) as conn:
        conn.execute(f"UPDATE dataset SET {column} = NULL WHERE id = ?", (DATA_ID,))
    snapshot = dashboard.collect(registry=registry, project=project)
    assert getattr(snapshot.datasets[0], column) is None
    body = dashboard.render(snapshot)["evidence.md"]
    assert f"{column} not recorded" in body.lower()
    assert "series:   (" not in body


# ── publication contract ────────────────────────────────────────────────────────

@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_publish_requires_exactly_the_three_pages(
    project: Path, registry: Path, mutation: str
) -> None:
    add_experiment(registry)
    render_to(project, registry)
    before = os.readlink(live(project))

    pages = dashboard.render(dashboard.collect(registry=registry, project=project))
    if mutation == "missing":
        pages.pop("evidence.md")
    else:
        pages["surprise.md"] = "smuggled"

    with pytest.raises(dashboard.DashboardRefused, match=r"(?i)page"):
        dashboard.publish(pages, project / "00-dashboard")
    assert os.readlink(live(project)) == before


# ── relative dataset paths resolve against the supplied project ─────────────────

def test_relative_dataset_path_resolves_against_the_given_project(
    project: Path, registry: Path
) -> None:
    """Resolving against the global `db.PROJECT_ROOT` makes a temp project consult the
    real repository, which is both wrong and untestable."""
    relative = Path("data") / "catalog" / "quokka.parquet"
    absolute = project / relative
    absolute.parent.mkdir(parents=True)
    absolute.write_bytes(b"relative-path-bytes")
    add_dataset(registry, None, sha=hashlib.sha256(b"relative-path-bytes").hexdigest())
    with connect(registry) as conn:
        conn.execute("UPDATE dataset SET parquet_path = ? WHERE id = ?",
                     (str(relative), DATA_ID))
    snapshot = dashboard.collect(registry=registry, project=project)
    assert snapshot.datasets[0].verification == "match"


# ── unreadable dataset files ────────────────────────────────────────────────────

def test_unreadable_dataset_file_is_a_stable_diagnostic(
    project: Path, registry: Path, parquet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_dataset(registry, parquet)

    def boom(_path: Path, chunk: int = 1 << 20) -> str:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(dashboard.db, "sha256_file", boom)
    pages = dashboard.render(dashboard.collect(registry=registry, project=project))
    assert "file unreadable" in pages["evidence.md"].lower()
    assert DATA_ID in pages["attention.md"]
    # Platform wording must not reach a generated page.
    assert "Permission denied" not in pages["evidence.md"]
    assert "Errno" not in pages["evidence.md"]


# ── health wording ──────────────────────────────────────────────────────────────

def test_health_items_are_described_as_hook_enforcement_checks(
    project: Path, registry: Path
) -> None:
    """These are `health.hook_checks`, not the whole doctor fact model, and the page must
    not imply otherwise."""
    (project / ".claude" / "hooks" / "no_fabricated_numbers.py").unlink()
    body = dashboard.render(dashboard.collect(registry=registry, project=project))["attention.md"]
    assert "hook-enforcement health check" in body.lower()


# ── ignore rules, asserted through git itself ───────────────────────────────────

def test_git_actually_ignores_runtime_and_generated_but_not_the_readme() -> None:
    """Reading `.gitignore` text proves nothing about what git does with it."""
    import subprocess

    repo = Path(__file__).resolve().parents[1]

    def ignored(rel: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", rel], cwd=repo,
                              capture_output=True).returncode == 0

    assert ignored("00-dashboard/.runtime/generations/abc/pipeline.md")
    assert ignored("00-dashboard/generated")
    assert not ignored("00-dashboard/README.md")


# ── one read snapshot for the whole collection ──────────────────────────────────

def test_reader_holds_a_single_snapshot_across_queries(
    project: Path, registry: Path
) -> None:
    """Opening `mode=ro` prevents writing; it does not make successive SELECTs agree.

    Without an explicit read transaction the four collection queries can each observe a
    different committed version while the result claims to be one immutable Snapshot.
    """
    add_experiment(registry, exp_id="EXP-000001", title="First")
    with dashboard._open_readonly(registry) as reader:
        first = reader.execute("SELECT COUNT(*) FROM experiment").fetchone()[0]
        with connect(registry) as writer:
            writer.execute(
                "INSERT INTO experiment (id, title, slug, status, trial_budget, market, "
                "created_at) VALUES ('EXP-000002','Second','s','RESEARCH',10,'crypto',"
                "'2026-01-01T00:00:00Z')")
        second = reader.execute("SELECT COUNT(*) FROM experiment").fetchone()[0]

    assert first == second, "the reader observed a mid-collection commit"


def test_reader_still_refuses_writes(project: Path, registry: Path) -> None:
    with (dashboard._open_readonly(registry) as conn,
          pytest.raises(sqlite3.OperationalError, match=r"(?i)readonly")):
        conn.execute("UPDATE experiment SET title = 'nope'")


def test_a_later_render_sees_committed_changes(project: Path, registry: Path) -> None:
    """The snapshot must be per-collection, not a stale cache held across calls."""
    add_experiment(registry, exp_id="EXP-000001", title="First")
    before = dashboard.collect(registry=registry, project=project)
    add_experiment(registry, exp_id="EXP-000002", title="Second")
    after = dashboard.collect(registry=registry, project=project)
    assert len(before.experiments) == 1
    assert len(after.experiments) == 2


# ── a reused generation must be exactly the three expected files ────────────────

def test_extra_file_in_a_reused_generation_is_refused(
    project: Path, registry: Path
) -> None:
    """Reproduced: an extra file inside an existing generation was published, and the live
    dashboard exposed four files."""
    add_experiment(registry)
    generation = render_to(project, registry)
    before = os.readlink(live(project))
    (generation / "surprise.md").write_text("smuggled\n", encoding="utf-8")

    with pytest.raises(dashboard.DashboardRefused, match=r"(?i)unexpected|extra"):
        render_to(project, registry)
    assert os.readlink(live(project)) == before
    assert sorted(p.name for p in live(project).iterdir()) == sorted(
        [*dashboard.PAGES, "surprise.md"])


def test_missing_file_in_a_reused_generation_is_refused(
    project: Path, registry: Path
) -> None:
    add_experiment(registry)
    generation = render_to(project, registry)
    before = os.readlink(live(project))
    (generation / "evidence.md").unlink()

    with pytest.raises(dashboard.DashboardRefused, match=r"(?i)missing|evidence"):
        render_to(project, registry)
    assert os.readlink(live(project)) == before


def test_a_symlinked_page_in_a_reused_generation_is_refused(
    project: Path, registry: Path, tmp_path: Path
) -> None:
    """A page replaced by a link points the dashboard at bytes outside the generation."""
    add_experiment(registry)
    generation = render_to(project, registry)
    before = os.readlink(live(project))

    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text((generation / "pipeline.md").read_text(encoding="utf-8"),
                         encoding="utf-8")
    (generation / "pipeline.md").unlink()
    (generation / "pipeline.md").symlink_to(elsewhere)

    with pytest.raises(dashboard.DashboardRefused, match=r"(?i)symlink|regular file"):
        render_to(project, registry)
    assert os.readlink(live(project)) == before


def test_a_subdirectory_in_a_reused_generation_is_refused(
    project: Path, registry: Path
) -> None:
    add_experiment(registry)
    generation = render_to(project, registry)
    before = os.readlink(live(project))
    (generation / "nested").mkdir()

    with pytest.raises(dashboard.DashboardRefused, match=r"(?i)unexpected|extra"):
        render_to(project, registry)
    assert os.readlink(live(project)) == before
