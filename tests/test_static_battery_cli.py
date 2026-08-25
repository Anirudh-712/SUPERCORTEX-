"""Slice C2 — `supercortex falsify static`, the verified static-battery entrypoint.

The command exists to close one gap: until now nothing ran the static battery against
*verified* evidence and recorded what it concluded. Three properties are load-bearing and
each has tests here that fail without it.

**Nothing is inferred about which trial.** Both the experiment id and the trial sequence
are required. A command that defaults to "the latest" or to `metrics.json` silently judges
whichever artifact happens to be lying around, and the operator reading the verdict has no
way to know which run it describes.

**A refusal writes nothing.** If `loader.verify` refuses for any reason, no falsification
row is written at all. A half-recorded battery is worse than none: a lifecycle gate reading
it sees passes and no failures.

**The battery is persisted atomically.** Every row is validated before any is written, all
of them go in one transaction, and every row carries the same trial count and the same
verdict timestamp. Nine rows written at nine different moments against nine different trial
counts are not one verdict.

The end-to-end tests drive a real sealed trial produced by `supercortex.backtest.golden`
(law 6 — never a plausible hypothesis). The plumbing tests stub `loader.verify`, because a
test that spins a Nautilus backtest to prove an exit code is a slow test that proves the
wrong thing.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest
from test_loader import SAWTOOTH, _config, _wire
from typer.testing import CliRunner

from supercortex.costs.model import CostModel, Trade
from supercortex.falsify import cli as falsify_cli
from supercortex.falsify import loader
from supercortex.falsify import orchestrator as orch
from supercortex.registry import db, experiments

runner = CliRunner()

EXP = "EXP-000042"
BASE = 1_700_000_000_000
HOUR = 3_600_000


def flat(text: str) -> str:
    """Rich wraps at the terminal width, so assertions compare on collapsed whitespace."""
    return " ".join(text.split())


def refused(result) -> str:
    """A refusal is a decision the command reached, not an exception it fell out of.

    `CliRunner` reports an uncaught error as exit code 1 too, so asserting only that the
    exit code is non-zero would let a crash stand in for a diagnosis — and every "writes
    no rows" assertion would still hold, because a crash writes no rows either.
    """
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    return flat(result.output)


# ── a hand-built desk, for everything that is not about the engine ──────────────
def _trades(n: int = 24) -> tuple[Trade, ...]:
    """Enough round trips that no static test refuses for want of data, spread over three
    calendar years so the regime gate has something to attribute."""
    out = []
    for i in range(n):
        entry = BASE + i * 90 * 24 * HOUR
        out.append(Trade(
            entry_ts_ms=entry, exit_ts_ms=entry + 12 * HOUR, direction=1, qty=1.0,
            entry_price=100.0, exit_price=100.0 + (3.0 if i % 3 else -1.0),
        ))
    return tuple(out)


def _evidence(*, experiment_id: str = EXP, trial_seq: int = 1,
              trades: tuple[Trade, ...] | None = None) -> loader.TrialEvidence:
    rows = trades if trades is not None else _trades()
    prices = np.array([100.0 + (i % 7) for i in range(500)], dtype=float)
    stamps = np.array([BASE + i * HOUR for i in range(500)], dtype=float)
    return loader.TrialEvidence(
        experiment_id=experiment_id, trial_seq=trial_seq,
        code_commit="0" * 40, config_path="02-experiments/x/config.yaml",
        config_hash="c" * 64, dataset_ids=("DATA-000001",), data_snapshot="d" * 64,
        series_digest="s" * 64, series_source="registered_dataset", n_points=500,
        trades=rows, returns=tuple(0.0 for _ in rows), sharpe_per_trade=0.1,
        model=CostModel(), starting_equity=1_000_000.0,
        prices=prices, timestamps=stamps, headline={},
    )


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """A registry with one experiment and one sealed trial, and `verify` stubbed to accept
    it. The artifact file is real so the path the command resolves is a real path."""
    registry = tmp_path / "registry.sqlite"
    db.init(registry)
    monkeypatch.setattr(db, "DB_PATH", registry)
    monkeypatch.setattr(db, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(experiments, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(experiments, "EXPERIMENTS_DIR", tmp_path / "02-experiments")
    monkeypatch.setattr(falsify_cli, "PROJECT_ROOT", tmp_path)

    artifacts = tmp_path / "02-experiments" / f"{EXP}-probe" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "metrics.json").write_text("{}", encoding="utf-8")
    for seq in (1, 2):
        (artifacts / f"metrics-trial-{seq:04d}.json").write_text("{}", encoding="utf-8")

    with db.connect(registry) as conn:
        conn.execute(
            "INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
            " VALUES (?,?,?,?,?,?)", (EXP, "probe", "probe", "BACKTESTING", 10, db.now()))
        for seq, outcome in ((1, "ok"), (2, "ok"), (3, "error")):
            rel = f"02-experiments/{EXP}-probe/artifacts/metrics-trial-{seq:04d}.json"
            conn.execute(
                "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
                "cost_model_version,seed,started_at,finished_at,outcome,metrics_path,"
                "artifact_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (EXP, seq, "c" * 64, "0" * 40, "d" * 64, "india-vda-1.0.0", 42,
                 db.now(), db.now(), outcome, rel if seq != 3 else None, "a" * 64))

    seen: list[Path] = []

    def fake_verify(path: Path) -> loader.TrialEvidence:
        """Stands in for verification, and returns the trial it was asked to read.

        Returning a fixed sequence would trip the identity binding on every request but
        trial 1, which would make most of these tests pass for the wrong reason.
        """
        seen.append(Path(path))
        return _evidence(trial_seq=int(Path(path).stem.rsplit("-", 1)[-1]))

    monkeypatch.setattr(loader, "verify", fake_verify)
    return type("Desk", (), {"root": tmp_path, "registry": registry, "verified": seen})


def rows(registry: Path) -> list[sqlite3.Row]:
    with db.connect(registry) as conn:
        return conn.execute(
            "SELECT * FROM falsification ORDER BY test_name").fetchall()


def run(*args: str):
    return runner.invoke(falsify_cli.app, ["static", *args])


# ── the registry's batch writer ─────────────────────────────────────────────────
def _result(name: str, status: str = "PASS") -> experiments.FalsificationResult:
    return experiments.FalsificationResult(
        test_name=name, status=status, statistic=1.0, threshold=0.5)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.sqlite"
    db.init(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
            " VALUES (?,?,?,?,?,?)", (EXP, "probe", "probe", "BACKTESTING", 10, db.now()))
    return path


def test_a_batch_writes_every_row_in_one_transaction(registry):
    experiments.record_falsification_batch(
        EXP, [_result("a"), _result("b", "FAIL"), _result("c")], trials_at_run=7)
    stored = rows(registry)
    assert [r["test_name"] for r in stored] == ["a", "b", "c"]
    assert [r["status"] for r in stored] == ["PASS", "FAIL", "PASS"]
    assert [r["passed"] for r in stored] == [1, 0, 1]


def test_a_batch_with_one_invalid_status_writes_nothing(registry):
    """Validation runs over the whole battery before the first row is written. Validating
    row by row would leave the rows before the bad one on disk."""
    with pytest.raises(ValueError, match="SKIPPED"):
        experiments.record_falsification_batch(
            EXP, [_result("a"), _result("b"), _result("c", "SKIPPED")], trials_at_run=7)
    assert rows(registry) == []


class _FailsOnNthInsert:
    """A connection that stops working part-way through the battery.

    `sqlite3.Connection.execute` cannot be patched in place, so the connection is wrapped
    rather than modified. Everything else — including the ROLLBACK — passes straight
    through, which is the point: the rollback has to be real for the assertion to mean
    anything.
    """

    def __init__(self, conn, nth: int) -> None:
        self._conn, self._nth, self._seen = conn, nth, 0

    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith("INSERT INTO FALSIFICATION"):
            self._seen += 1
            if self._seen == self._nth:
                raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_a_database_failure_midway_leaves_no_partial_battery(registry, monkeypatch):
    """Validation cannot catch everything. A write that fails after two rows are inserted
    must still leave nothing behind, which is what the transaction is for."""
    real = db.connect

    @contextmanager
    def failing(path=None):
        with real(path) as conn:
            yield _FailsOnNthInsert(conn, 3)

    monkeypatch.setattr(experiments, "connect", failing)
    with pytest.raises(sqlite3.OperationalError):
        experiments.record_falsification_batch(
            EXP, [_result(n) for n in "abcde"], trials_at_run=7)
    monkeypatch.undo()
    assert rows(registry) == []


def test_a_batch_refuses_duplicate_test_names(registry):
    """Two verdicts for one test is a battery bug. The upsert would silently keep the
    last, and the row would then report a result nobody could trace to a run."""
    with pytest.raises(ValueError, match="more than once"):
        experiments.record_falsification_batch(
            EXP, [_result("a"), _result("b"), _result("a", "FAIL")], trials_at_run=7)
    assert rows(registry) == []


def test_a_batch_refuses_an_empty_result_set(registry):
    """A battery that concluded nothing is not a verdict, and returning quietly would let
    a caller report a successful run that recorded no evidence."""
    with pytest.raises(ValueError, match="no results"):
        experiments.record_falsification_batch(EXP, [], trials_at_run=7)


def test_every_row_in_a_batch_shares_one_ran_at_and_one_trials_at_run(registry,
                                                                      monkeypatch):
    """`now()` is second-resolution, so nine calls in a tight loop return the same string
    and comparing the stored values would prove nothing. The clock is made to move on
    every call instead, and the writer is required to read it once."""
    ticks = {"n": 0}

    def moving_clock():
        ticks["n"] += 1
        return f"2026-08-25 00:00:{ticks['n']:02d}"

    monkeypatch.setattr(experiments, "now", moving_clock)
    experiments.record_falsification_batch(
        EXP, [_result(n) for n in "abcdefghi"], trials_at_run=11)
    assert ticks["n"] == 1
    stored = rows(registry)
    assert {r["ran_at"] for r in stored} == {"2026-08-25 00:00:01"}
    assert {r["trials_at_run"] for r in stored} == {11}


def test_a_rerun_replaces_every_mutable_field(registry):
    experiments.record_falsification_batch(
        EXP, [experiments.FalsificationResult("a", "PASS", 9.0, 1.0)],
        trials_at_run=1, artifact_path="old.json", ran_at="2020-01-01 00:00:00")
    experiments.record_falsification_batch(
        EXP, [experiments.FalsificationResult("a", "FAIL", 2.0, 3.0)],
        trials_at_run=5, artifact_path="new.json", ran_at="2026-01-01 00:00:00")
    (row,) = rows(registry)
    assert (row["status"], row["passed"]) == ("FAIL", 0)
    assert (row["statistic"], row["threshold"]) == (2.0, 3.0)
    assert (row["trials_at_run"], row["artifact_path"]) == (5, "new.json")
    assert row["ran_at"] == "2026-01-01 00:00:00"


def test_the_singular_writer_delegates_to_the_batch_writer(registry, monkeypatch):
    """One storage contract. If the singular writer keeps its own INSERT, the two drift
    and only one of them enforces the validation."""
    seen = {}

    def spy(experiment_id, results, **kw):
        seen.update({"experiment_id": experiment_id, "results": list(results), **kw})

    monkeypatch.setattr(experiments, "record_falsification_batch", spy)
    experiments.record_falsification(
        EXP, "1-lookahead-bars", status="INERT", statistic=0.01, threshold=0.1,
        trials_at_run=4, artifact_path="a.json")
    assert seen["experiment_id"] == EXP
    assert seen["trials_at_run"] == 4
    assert seen["artifact_path"] == "a.json"
    assert seen["results"] == [
        experiments.FalsificationResult("1-lookahead-bars", "INERT", 0.01, 0.1)]


def test_the_singular_writer_still_validates_status(registry):
    with pytest.raises(ValueError, match="MAYBE"):
        experiments.record_falsification(
            EXP, "x", status="MAYBE", statistic=None, threshold=None, trials_at_run=1)
    assert rows(registry) == []


# ── what the static battery does and does not cover ─────────────────────────────
def test_the_static_battery_covers_six_numbered_tests_and_three_gates():
    names = [fn.__name__ for fn in orch.STATIC_TESTS]
    assert len(names) == 9
    ctx = orch.Context(EXP, list(_trades()), np.zeros(0), np.zeros(0), 1.0,
                       CostModel(), 1)
    results = [orch.TestResult(n, True, None, None, "") for n in
               ("3-a", "5-b", "6-c", "7-d", "9-e", "10-f", "G1-g", "G2-h", "G3-i")]
    assert orch.covered_numbers(results) == ("3", "5", "6", "7", "9", "10")
    assert orch.structural_gates(results) == ("G1-g", "G2-h", "G3-i")
    assert ctx.trials_consumed == 1


def test_the_uncovered_promotion_gate_tests_are_named():
    """The five tests the static battery does not run are named, not merely omitted. A
    report that lists what it did is not a report of what promotion still requires."""
    results = [orch.TestResult(n, True, None, None, "") for n in
               ("3-a", "5-b", "6-c", "7-d", "9-e", "10-f", "G1-g")]
    assert orch.uncovered_numbers(results) == ("1", "2", "4", "8", "11")
    assert len(orch.PROMOTION_GATE_TESTS) == 11


# ── the command ─────────────────────────────────────────────────────────────────
def test_the_trial_sequence_is_required(desk):
    """No default to the latest trial and no default to the headline artifact."""
    result = runner.invoke(falsify_cli.app, ["static", EXP])
    assert result.exit_code != 0
    assert "--trial" in flat(result.output)
    assert rows(desk.registry) == []


def test_a_verified_trial_is_the_one_named_not_the_headline(desk):
    run(EXP, "--trial", "2")
    assert [p.name for p in desk.verified] == ["metrics-trial-0002.json"]


def test_an_unknown_trial_is_refused_by_id_and_sequence(desk):
    result = run(EXP, "--trial", "99")
    assert "TRIAL_NOT_FOUND" in refused(result)
    assert desk.verified == []
    assert rows(desk.registry) == []


def test_a_trial_with_no_recorded_artifact_is_refused(desk):
    """Trial 3 is sealed `error` and has no metrics path. Falling back to the headline
    here would judge a run whose result was never accepted."""
    result = run(EXP, "--trial", "3")
    assert "ARTIFACT_PATH_MISMATCH" in refused(result)
    assert desk.verified == []
    assert rows(desk.registry) == []


def test_a_verification_refusal_prints_the_diagnostic_and_exits_non_zero(desk, monkeypatch):
    def refuse(path):
        raise loader.Unverifiable(loader.Diagnostic(
            code=loader.Code.ARTIFACT_HASH_MISMATCH,
            message="its bytes changed after sealing",
            experiment_id=EXP, trial_seq=1, expected="aaa", observed="bbb"))

    monkeypatch.setattr(loader, "verify", refuse)
    result = run(EXP, "--trial", "1")
    refused(result)
    payload = json.loads(result.output[result.output.index("{"):
                                       result.output.rindex("}") + 1])
    assert payload["code"] == "ARTIFACT_HASH_MISMATCH"
    assert payload["retryable"] is False
    assert "bytes changed after sealing" in flat(result.output)


def test_a_verification_refusal_writes_no_falsification_rows(desk, monkeypatch):
    def refuse(path):
        raise loader.Unverifiable(loader.Diagnostic(
            code=loader.Code.ENVIRONMENT_DRIFT, message="polars differs"))

    monkeypatch.setattr(loader, "verify", refuse)
    run(EXP, "--trial", "1")
    assert rows(desk.registry) == []


def test_unreadable_thresholds_refuse_and_write_nothing(desk, monkeypatch):
    def boom(path=None):
        raise orch.ThresholdsError("thresholds.yaml is missing")

    monkeypatch.setattr(orch, "load_thresholds", boom)
    result = run(EXP, "--trial", "1")
    assert "thresholds.yaml is missing" in refused(result)
    assert rows(desk.registry) == []


def test_the_report_names_the_static_battery_and_not_the_full_battery(desk):
    result = run(EXP, "--trial", "1")
    said = flat(result.output)
    assert "static battery" in said
    assert "full falsification battery" not in said


def test_the_report_states_the_shape_of_what_it_ran(desk):
    said = flat(run(EXP, "--trial", "1").output)
    assert "six numbered tests" in said
    assert "three structural gates" in said


def test_the_report_states_it_does_not_satisfy_the_promotion_gate(desk):
    said = flat(run(EXP, "--trial", "1").output)
    # The whole phrase. A bare "does not satisfy" also appears inside G3-benchmark's own
    # prose, so the short form matched the wrong sentence and passed with this line deleted.
    assert "does not satisfy the eleven-test promotion gate" in said
    assert "five of the eleven were not run here:" in said


def test_the_report_names_which_tests_failed(desk):
    """On the summary line specifically. Every test name appears somewhere in the report —
    each result prints its own detail — so asserting the failures are "in the output"
    passed even with the summary reduced to a count."""
    said = flat(run(EXP, "--trial", "1").output)
    stored = {r["test_name"]: r["status"] for r in rows(desk.registry)}
    failed = sorted(n for n, s in stored.items() if s == "FAIL")
    passed = sorted(n for n, s in stored.items() if s == "PASS")
    assert failed and passed, f"this desk must both fail and pass something: {stored}"

    line = said[said.index("failed:"):]
    line = line[:line.index("This does not satisfy")]
    for name in failed:
        assert name in line, f"{name} failed but is not named on the summary line"
    for name in passed:
        assert name not in line, f"{name} passed but is named as a failure"


def test_a_cannot_run_test_is_recorded_as_a_fail(desk):
    """PBO has no parameter sweep here, so it cannot run. That is a recorded FAIL, never a
    skip and never an absent row."""
    run(EXP, "--trial", "1")
    pbo = [r for r in rows(desk.registry) if r["test_name"] == "6-pbo"]
    assert len(pbo) == 1
    assert pbo[0]["status"] == "FAIL"
    assert pbo[0]["passed"] == 0


def test_every_static_result_is_persisted(desk):
    run(EXP, "--trial", "1")
    stored = rows(desk.registry)
    assert len(stored) == len(orch.STATIC_TESTS)
    assert {r["status"] for r in stored} <= {"PASS", "FAIL"}


def test_the_whole_battery_is_persisted_in_one_call(desk, monkeypatch):
    """One call, one transaction, one verdict. Recording test by test would stamp each row
    with its own clock reading and its own trial count, and the rows would then describe
    several judgements of one run rather than one judgement."""
    calls = []
    real = experiments.record_falsification_batch

    def spy(experiment_id, results, **kw):
        calls.append((experiment_id, list(results), kw))
        return real(experiment_id, results, **kw)

    monkeypatch.setattr(experiments, "record_falsification_batch", spy)
    run(EXP, "--trial", "1")
    assert len(calls) == 1
    assert len(calls[0][1]) == len(orch.STATIC_TESTS)


def test_the_persisted_status_is_the_one_the_test_concluded(desk, monkeypatch):
    """Persisted from `TestResult.status`, which is a `BatteryStatus`. Re-deriving a status
    from `passed` further down the pipe would put the vocabulary in two places, and only
    one of them would be the battery's."""
    results = [orch.TestResult("3-cost-ladder", True, 1.0, 0.0, "ok"),
               orch.TestResult("5-walk-forward", False, 0.0, 1.0, "no")]
    monkeypatch.setattr(
        orch, "run_static_battery",
        lambda ctx, thresholds=None: orch.Verdict(EXP, results, ctx.trials_consumed, 1))
    run(EXP, "--trial", "1")
    stored = {r["test_name"]: r["status"] for r in rows(desk.registry)}
    assert stored == {r.name: str(r.status) for r in results}
    assert [r.status for r in results] == [orch.BatteryStatus.PASS,
                                           orch.BatteryStatus.FAIL]


def test_the_verdict_records_the_artifact_it_was_reached_on(desk):
    run(EXP, "--trial", "2")
    paths = {r["artifact_path"] for r in rows(desk.registry)}
    assert paths == {f"02-experiments/{EXP}-probe/artifacts/metrics-trial-0002.json"}


def test_the_trial_count_is_measured_once_from_the_registry(desk, monkeypatch):
    """Measured once at verdict time, not once per test: a count that moves between rows
    would deflate two Sharpes against two different N."""
    calls = {"n": 0}
    real = experiments.trial_count

    def counted(experiment_id):
        calls["n"] += 1
        return real(experiment_id) + calls["n"]

    monkeypatch.setattr(experiments, "trial_count", counted)
    run(EXP, "--trial", "1")
    assert calls["n"] == 1
    assert {r["trials_at_run"] for r in rows(desk.registry)} == {4}


def test_the_sharpe_variance_is_never_taken_from_unauthenticated_siblings(desk,
                                                                          monkeypatch):
    """C2 supplies no cross-trial variance at all.

    The only source that ever existed read sibling `metrics.json` documents straight off
    disk — no digest, no registry binding, no provenance. A DSR deflated by a number
    anyone with write access could set is not a verified result, so the battery falls back
    to the asymptotic approximation and says so.
    """
    seen = {}
    real = orch.run_static_battery
    monkeypatch.setattr(orch, "run_static_battery",
                        lambda ctx, thresholds=None: (seen.setdefault("ctx", ctx),
                                                      real(ctx, thresholds))[1])
    run(EXP, "--trial", "1")
    assert seen["ctx"].sharpe_variance is None
    _, source = seen["ctx"].effective_sharpe_variance()
    assert source == ("asymptotic approximation; no authenticated cross-trial variance "
                      "available")


def test_the_unauthenticated_variance_helper_is_gone(desk):
    """Removed rather than left unused. A helper that reads unauthenticated artifacts is a
    loaded gun beside a module whose whole purpose is verified evidence."""
    assert not hasattr(orch, "sharpe_variance_from_registry")


def test_sweep_inputs_are_absent_without_registered_evidence(desk, monkeypatch):
    """No producer writes a sweep yet. The command must not invent one to make PBO run."""
    seen = {}
    real = orch.run_static_battery

    def spy(ctx, thresholds=None):
        seen["ctx"] = ctx
        return real(ctx, thresholds)

    monkeypatch.setattr(orch, "run_static_battery", spy)
    run(EXP, "--trial", "1")
    ctx = seen["ctx"]
    assert ctx.sweep_returns is None
    assert ctx.sweep_scores == {}
    assert ctx.chosen_config is None


def test_the_context_is_built_from_verified_evidence(desk, monkeypatch):
    seen = {}
    real = orch.run_static_battery

    def spy(ctx, thresholds=None):
        seen["ctx"] = ctx
        return real(ctx, thresholds)

    monkeypatch.setattr(orch, "run_static_battery", spy)
    run(EXP, "--trial", "1")
    ev, ctx = _evidence(), seen["ctx"]
    assert ctx.experiment_id == ev.experiment_id
    assert tuple(ctx.trades) == ev.trades
    assert ctx.starting_equity == ev.starting_equity
    assert ctx.model == ev.model
    assert np.array_equal(ctx.prices, ev.prices)
    assert np.array_equal(ctx.timestamps, ev.timestamps)


def test_a_verified_artifact_for_another_experiment_is_not_recorded_here(desk,
                                                                          monkeypatch):
    """Verification proves a document is a sealed trial. It does not prove it is the trial
    the operator asked about."""
    monkeypatch.setattr(loader, "verify",
                        lambda path: _evidence(experiment_id="EXP-000777"))
    result = run(EXP, "--trial", "1")
    said = refused(result)
    assert "REQUESTED_TRIAL_MISMATCH" in said
    assert rows(desk.registry) == []


def test_a_verified_artifact_for_another_sequence_is_not_recorded_here(desk, monkeypatch):
    monkeypatch.setattr(loader, "verify", lambda path: _evidence(trial_seq=5))
    assert "REQUESTED_TRIAL_MISMATCH" in refused(run(EXP, "--trial", "1"))
    assert rows(desk.registry) == []


#: Every shape a recorded `metrics_path` must not be allowed to take. Each would let a
#: registry row — or anyone who can write one — steer the reader at a file outside the
#: project before a single confinement check inside the loader ever runs.
ESCAPES = [
    ("an absolute path outside the project", lambda root, out: str(out)),
    ("a parent traversal", lambda root, out: f"02-experiments/../../{out.name}"),
    ("a symlink out of the project", lambda root, out: "02-experiments/escape.json"),
]


@pytest.mark.parametrize("label,build", ESCAPES, ids=[e[0] for e in ESCAPES])
def test_a_registered_path_that_escapes_the_project_is_never_opened(desk, monkeypatch,
                                                                   tmp_path_factory,
                                                                   label, build):
    """Confined before anything reads it. `loader.verify` opens the file as its first act,
    so a path checked only inside the loader has already been opened by the time it is
    rejected — and on this desk the neighbouring files are `.env` and the Keychain."""
    outside = tmp_path_factory.mktemp("outside") / "secret.json"
    outside.write_text('{"secret": true}', encoding="utf-8")
    if label.startswith("a symlink"):
        (desk.root / "02-experiments" / "escape.json").symlink_to(outside)

    with db.connect(desk.registry) as conn:
        conn.execute(
            "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
            "cost_model_version,seed,started_at,finished_at,outcome,metrics_path,"
            "artifact_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (EXP, 7, "c" * 64, "0" * 40, "d" * 64, "india-vda-1.0.0", 42, db.now(),
             db.now(), "ok", build(desk.root, outside), "a" * 64))

    opened: list[str] = []
    for reader in ("read_text", "read_bytes"):
        real = getattr(Path, reader)

        def spy(self, *a, _real=real, **kw):
            opened.append(str(self))
            return _real(self, *a, **kw)

        monkeypatch.setattr(Path, reader, spy)

    said = refused(run(EXP, "--trial", "7"))
    assert "ARTIFACT_PATH_MISMATCH" in said
    assert desk.verified == [], "loader.verify was reached"
    assert not [p for p in opened if str(outside) in p or "escape.json" in p]
    assert rows(desk.registry) == []


def test_the_report_separates_engine_reruns_from_missing_evidence(desk):
    """Four of the five need trial budget. Test 8 cannot be run at any price yet, and
    saying they all need a re-run misdescribes what promotion is waiting on."""
    said = flat(run(EXP, "--trial", "1").output)
    assert "1, 2, 4, 11 — registered engine re-runs" in said
    assert "8 — authenticated registered variant-family evidence" in said
    assert "SPA / Reality Check" in said
    # the two groups are separate: 8 must not be swept into the re-run sentence
    rerun = said[said.index("1, 2, 4, 11 —"):]
    assert "8" not in rerun[:rerun.index("registered engine re-runs")]


def test_exit_is_zero_only_when_every_static_result_passes(desk, monkeypatch):
    def all_pass(ctx, thresholds=None):
        return orch.Verdict(EXP, [orch.TestResult("3-cost-ladder", True, 1.0, 0.0, "ok")],
                            ctx.trials_consumed, 1)

    monkeypatch.setattr(orch, "run_static_battery", all_pass)
    assert run(EXP, "--trial", "1").exit_code == 0

    def one_fails(ctx, thresholds=None):
        return orch.Verdict(EXP, [orch.TestResult("3-cost-ladder", True, 1.0, 0.0, "ok"),
                                  orch.TestResult("5-walk-forward", False, 0.0, 1.0, "no")],
                            ctx.trials_consumed, 1)

    monkeypatch.setattr(orch, "run_static_battery", one_fails)
    assert run(EXP, "--trial", "1").exit_code != 0


# ── end to end, against a real sealed trial ─────────────────────────────────────
@pytest.fixture
def sealed_desk(tmp_path, monkeypatch):
    """One trial that a producer actually ran, registered and sealed.

    `GoldenStrategy` over the synthetic sawtooth, per law 6: engine validation never uses a
    plausible hypothesis. Hand-built evidence would only prove the command agrees with the
    test author, so the artifact under test comes out of `trial.execute` and `close_trial`.
    """
    from supercortex.backtest import golden
    from supercortex.backtest import runner as bt_runner
    from supercortex.backtest import trial as trial_mod

    _wire(tmp_path, monkeypatch, _config(), {})
    monkeypatch.setattr(falsify_cli, "PROJECT_ROOT", tmp_path)
    # `_wire` redirects db, experiments and loader. The orchestrator holds its own copy of
    # the constant, and leaving it pointing at the real tree made the sibling-document
    # test pass for the wrong reason: the siblings were simply never found.
    monkeypatch.setattr(orch, "PROJECT_ROOT", tmp_path)

    # config.yaml cannot express a synthetic frame, so the spec is decorated rather than
    # the runner replaced: a real Nautilus backtest still drives the fixture.
    def synthetic_spec(**kwargs):
        return bt_runner.BacktestSpec(
            **kwargs,
            synthetic_bars=golden.sawtooth(**SAWTOOTH),
            synthetic_source={"generator": "supercortex.backtest.golden:sawtooth",
                              "parameters": {**SAWTOOTH,
                                             "epoch_start_ms": golden.EPOCH_START,
                                             "bar_ms": golden.HOUR_MS}},
        )

    monkeypatch.setattr(trial_mod, "BacktestSpec", synthetic_spec)
    trial_mod.execute("EXP-000009", allow_dirty=True)
    return tmp_path


def _artifact(root: Path) -> Path:
    return (root / "02-experiments" / "EXP-000009-probe" / "artifacts"
            / "metrics-trial-0001.json")


def test_a_verified_sealed_trial_runs_the_static_battery_and_persists_it(sealed_desk):
    """The whole path, on evidence a producer actually sealed: resolve, verify,
    reconstruct, run, record."""
    result = run("EXP-000009", "--trial", "1")
    stored = rows(sealed_desk / "registry.sqlite")
    assert len(stored) == len(orch.STATIC_TESTS)
    assert {r["experiment_id"] for r in stored} == {"EXP-000009"}
    assert len({r["ran_at"] for r in stored}) == 1
    assert {r["trials_at_run"] for r in stored} == {1}
    assert "static battery" in flat(result.output)
    for row in stored:
        assert row["status"] in ("PASS", "FAIL")
        assert row["passed"] == int(row["status"] == "PASS")


def test_a_tampered_artifact_is_refused_and_nothing_is_persisted(sealed_desk):
    """The sealed bytes are edited on disk. The registry digest is immutable, so the
    document no longer authenticates and the battery never starts."""
    path = _artifact(sealed_desk)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["falsification_input"]["cost_model"]["maker_fee_bps"] = 999.0
    path.write_text(experiments.dump_metrics(document), encoding="utf-8")

    result = run("EXP-000009", "--trial", "1")
    assert "ARTIFACT_HASH_MISMATCH" in refused(result)
    assert rows(sealed_desk / "registry.sqlite") == []


def _register_trial(root: Path, seq: int, metrics_path: Path, *,
                    experiment_id: str = "EXP-000009") -> None:
    """A second sealed row, INSERTed. The trial table refuses edits to a sealed row, so a
    forger who can write to the registry adds rows rather than changing them."""
    with db.connect(root / "registry.sqlite") as conn:
        source = conn.execute(
            "SELECT config_hash, code_commit, data_snapshot, cost_model_version, seed"
            " FROM trial WHERE experiment_id = ? AND seq = 1", (experiment_id,)).fetchone()
        conn.execute(
            "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
            "cost_model_version,seed,started_at,finished_at,outcome,metrics_path,"
            "artifact_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (experiment_id, seq, source["config_hash"], source["code_commit"],
             source["data_snapshot"], source["cost_model_version"], source["seed"],
             db.now(), db.now(), "ok",
             metrics_path.relative_to(root).as_posix(), db.sha256_file(metrics_path)))


def test_another_trials_verified_artifact_is_not_recorded_under_the_one_requested(
        sealed_desk):
    """The substitution the identity check exists to stop.

    Trial 2 is registered pointing at trial 1's artifact. That document is genuine: it is
    a real sealed trial, its bytes hash to the digest the registry holds for trial 1, and
    `loader.verify` accepts it outright. What it is not is trial 2 — and without binding
    the verified identity back to the request, nine rows would be written describing trial
    2 on evidence from trial 1, with nothing anywhere recording the swap.
    """
    original = _artifact(sealed_desk)
    _register_trial(sealed_desk, 2, original)

    # In isolation the artifact verifies. The refusal below is therefore not the loader
    # rejecting a bad document; it is the command refusing to file a good one under the
    # wrong name.
    verified = loader.verify(original)
    assert verified.trial_seq == 1
    assert verified.experiment_id == "EXP-000009"

    said = refused(run("EXP-000009", "--trial", "2"))
    assert "REQUESTED_TRIAL_MISMATCH" in said
    assert rows(sealed_desk / "registry.sqlite") == []

    # and the same artifact, requested under its own sequence, is recorded normally
    run("EXP-000009", "--trial", "1")
    assert len(rows(sealed_desk / "registry.sqlite")) == len(orch.STATIC_TESTS)


def test_a_sibling_metrics_document_cannot_change_the_static_verdict(sealed_desk):
    """Three further sealed trials exist, each with its own artifact on disk. Their
    `trades.sharpe_per_trade` values are then rewritten to something absurd.

    Nothing in the verdict may move. The trial COUNT is a registry fact and does change the
    DSR — that is why the sibling rows are registered before the first run and left alone —
    but the sibling documents' CONTENTS are unauthenticated bytes and must reach nothing.
    """
    artifacts = _artifact(sealed_desk).parent
    original = _artifact(sealed_desk).read_text(encoding="utf-8")
    for seq in (2, 3, 4):
        sibling = artifacts / f"metrics-trial-{seq:04d}.json"
        sibling.write_text(original, encoding="utf-8")
        _register_trial(sealed_desk, seq, sibling)

    run("EXP-000009", "--trial", "1")
    before = {r["test_name"]: (r["status"], r["statistic"], r["trials_at_run"])
              for r in rows(sealed_desk / "registry.sqlite")}

    for seq, wild in ((2, 41.0), (3, -73.0), (4, 999.0)):
        sibling = artifacts / f"metrics-trial-{seq:04d}.json"
        document = json.loads(sibling.read_text(encoding="utf-8"))
        document["trades"]["sharpe_per_trade"] = wild
        document["falsification_input"]["returns"]["sharpe_per_trade"] = wild
        sibling.write_text(experiments.dump_metrics(document), encoding="utf-8")

    run("EXP-000009", "--trial", "1")
    after = {r["test_name"]: (r["status"], r["statistic"], r["trials_at_run"])
             for r in rows(sealed_desk / "registry.sqlite")}
    assert after == before
