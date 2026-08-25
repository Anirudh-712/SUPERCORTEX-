"""`supercortex falsify` — the engine whose job is to kill strategies."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from supercortex.falsify import loader, probes
from supercortex.falsify import orchestrator as orch
from supercortex.registry import db, experiments
from supercortex.registry.db import PROJECT_ROOT
from supercortex.report.experiment import render_profit_factor

app = typer.Typer(no_args_is_help=True, help="Falsification battery.")
console = Console()


@app.command("probe")
def probe(
    experiment_id: str = typer.Argument(...),
    channels: str = typer.Option("bars,funding", "--channels",
                                 help="Input series the strategy reads."),
    agent: str | None = typer.Option(None, "--agent"),
) -> None:
    """Test 1 — the look-ahead battery. Each run is a registered trial and counts."""
    from supercortex.backtest import trial

    names = tuple(c.strip() for c in channels.split(",") if c.strip())
    specs = probes.battery(names)

    exp_dir = _artifacts(experiment_id)
    headline = exp_dir / "metrics.json"
    if not headline.exists():
        console.print("[red]no baseline[/] — run `supercortex exp run` first")
        raise typer.Exit(1)
    baseline: dict[str, Any] = json.loads(headline.read_text(encoding="utf-8"))

    console.print(f"{len(specs)} probe runs — each is a registered trial and counts "
                  f"toward the budget.\n")
    #: probe label -> that run's metrics document, the shape `probes.verdict` compares.
    results: dict[str, dict[str, Any]] = {}
    for spec in specs:
        with console.status(f"probe {spec.label}…"):
            metrics = trial.execute(experiment_id, agent_id=agent, probe=spec)
        trial.write_metrics(experiment_id, metrics)
        results[spec.label] = metrics
        console.print(f"  {spec.label:<18} PF "
                      f"{render_profit_factor(metrics['trades'], '.4f')}")

    passed, verdicts = probes.verdict(baseline, results, names)

    table = Table(title="test 1 — look-ahead battery", show_header=True, header_style="dim")
    for col in ("channel", "scramble", "advance", "delay", "verdict"):
        table.add_column(col, justify="right" if col not in ("channel", "verdict") else "left")
    colour = {"PASS": "green", "SUSPICIOUS": "red", "INERT": "yellow",
              "UNDEFINED": "red"}
    def delta(value: float | None) -> str:
        return "—" if value is None else f"{value:+.1%}"
    for v in verdicts:
        table.add_row(v.channel, delta(v.scramble_delta), delta(v.advance_delta),
                      delta(v.delay_delta),
                      f"[{colour.get(v.status, 'red')}]{v.status}[/]")
    console.print(table)
    for v in verdicts:
        console.print(f"\n[bold]{v.channel}[/] — {v.reasoning}")

    _record(experiment_id, verdicts)
    console.print(f"\ntest 1: [{'green' if passed else 'red'}]"
                  f"{'PASS' if passed else 'FAIL'}[/]")
    if not passed:
        raise typer.Exit(1)


def _artifacts(experiment_id: str) -> Path:
    with db.connect() as conn:
        row = conn.execute("SELECT slug FROM experiment WHERE id = ?",
                           (experiment_id,)).fetchone()
    if row is None:
        raise typer.BadParameter(f"unknown experiment {experiment_id}")
    return experiments.EXPERIMENTS_DIR / f"{experiment_id}-{row['slug']}" / "artifacts"


def _record(experiment_id: str, verdicts: list[probes.ChannelVerdict]) -> None:
    """One row per channel. Each carries its own `v.passed`; the aggregate is the caller's
    to report and is not stored again here."""
    trials = experiments.trial_count(experiment_id)
    for v in verdicts:
        experiments.record_falsification(
            experiment_id, f"1-lookahead-{v.channel}",
            status=v.status, statistic=v.advance_delta, threshold=probes.MATERIAL,
            trials_at_run=trials,
        )


# ── test 3, 5, 6, 7, 9, 10 and the structural gates, on verified evidence ───────
#: Small counts read as words so the report is prose rather than a status line. The count
#: itself is always derived from the results — spelling it does not hard-code it.
_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
          "ten", "eleven")


def _count(n: int) -> str:
    return _WORDS[n] if n < len(_WORDS) else str(n)


def _registered_artifact(experiment_id: str, seq: int) -> Path:
    """The sealed document the registry recorded for exactly this trial.

    Both the experiment and the sequence are named by the operator. Nothing here falls back
    to the latest trial or to `metrics.json`: the headline artifact belongs to whichever
    run wrote it last, so judging it would produce a verdict nobody can attribute to a run.
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT metrics_path, outcome FROM trial WHERE experiment_id = ? AND seq = ?",
            (experiment_id, seq),
        ).fetchone()
    if row is None:
        raise loader.refusal(
            loader.Code.TRIAL_NOT_FOUND,
            "the registry has no trial with this experiment id and sequence, so there is "
            "nothing to verify. The trial is named, never guessed.",
            experiment_id=experiment_id, trial_seq=seq,
            observed=f"{experiment_id} #{seq}",
        )
    recorded = row["metrics_path"]
    if not isinstance(recorded, str) or not recorded:
        raise loader.refusal(
            loader.Code.ARTIFACT_PATH_MISMATCH,
            "the registered trial records no sealed artifact, so it produced no document "
            "to judge. The headline artifact is not a substitute for it.",
            experiment_id=experiment_id, trial_seq=seq, observed=row["outcome"],
        )
    # Confined here, before the file is opened. `loader.verify` reads the document as its
    # first act, so a path checked only inside the loader has already been read by the time
    # it is refused. A registry row is not automatically trustworthy input: an absolute
    # path, a `..` segment or a symlink would otherwise aim this reader at `.env` or a key
    # file and hand its contents to a diagnostic.
    confined = loader.confined_path(recorded, PROJECT_ROOT)
    if confined is None:
        raise loader.refusal(
            loader.Code.ARTIFACT_PATH_MISMATCH,
            "the artifact path recorded for this trial does not stay inside the project, "
            "so it is refused unread. Nothing outside the repository is a sealed result.",
            experiment_id=experiment_id, trial_seq=seq, observed=recorded,
        )
    return confined


def _bind_to_request(verified: loader.TrialEvidence, experiment_id: str, seq: int,
                     path: Path) -> None:
    """The verified evidence must be the trial that was asked for.

    Verification proves a document is a genuine sealed trial. It does not prove it is
    *this* trial: the loader binds the artifact to whichever row the document claims, so if
    two registry rows record the same `metrics_path`, one trial's real artifact verifies
    perfectly when resolved for the other. Nine rows would then describe trial 7 on
    evidence from trial 3, with nothing anywhere recording the swap.
    """
    if verified.experiment_id == experiment_id and verified.trial_seq == seq:
        return
    raise loader.refusal(
        loader.Code.REQUESTED_TRIAL_MISMATCH,
        "the artifact registered for the requested trial verifies as a different trial, "
        "so recording its verdict here would file one trial's evidence under another's "
        "name",
        experiment_id=experiment_id, trial_seq=seq,
        expected=f"{experiment_id} #{seq}",
        observed=f"{verified.experiment_id} #{verified.trial_seq}",
        artifact=str(path),
    )


def _context(verified: loader.TrialEvidence, trials: int) -> orch.Context:
    """A battery context built only from verified evidence and verdict-time registry facts.

    The split is deliberate. Trades, prices, timestamps, equity and the cost model are
    reconstructed and digest-checked, so they come from the evidence. `trials_consumed` is
    read now, from the registry, because it describes how many looks the desk has taken *by
    the time the verdict is reached* — a count sealed into the artifact would deflate the
    Sharpe against the desk as it was, not as it is.

    `sharpe_variance` stays None. The only source that ever existed read sibling metrics
    documents off disk with no digest, no registry binding and no provenance, which put the
    DSR's deflation in reach of anyone who could write a file. The battery falls back to
    the asymptotic approximation and says so, which is a weaker claim honestly labelled
    rather than a stronger one nobody can check.

    The sweep fields stay at their defaults for the same reason. No producer writes a
    registered parameter sweep, and supplying one from anywhere else would let test 6 run
    on inputs no trial recorded.
    """
    return orch.Context(
        experiment_id=verified.experiment_id,
        trades=list(verified.trades),
        prices=verified.prices,
        timestamps=verified.timestamps,
        starting_equity=verified.starting_equity,
        model=verified.model,
        trials_consumed=trials,
    )


def _refused(diagnostic: loader.Diagnostic) -> None:
    """Machine-readable first, prose second. The JSON is what a report or a later reader
    parses; the sentence is for whoever is looking at the terminal right now."""
    typer.echo(json.dumps(diagnostic.as_dict(), indent=2, sort_keys=True))
    console.print(
        f"\n[red]static battery refused[/] — {diagnostic.message}", soft_wrap=True)
    console.print(
        "no falsification rows were written: a battery that could not verify its evidence "
        "has concluded nothing, and a half-recorded verdict reads as passes with no "
        "failures.", soft_wrap=True)
    if diagnostic.retryable:
        console.print("[yellow]retryable[/] — the world could change to satisfy this; the "
                      "evidence does not have to.", soft_wrap=True)


def _report(verdict: orch.Verdict, experiment_id: str, seq: int, artifact: str) -> None:
    """What ran, what failed, and what promotion still requires.

    The summary lines print with `soft_wrap=True`. Rich otherwise breaks a long line at
    the terminal width, and it breaks `5-walk-forward` at the hyphen, so a test name in a
    piped or captured log arrives split across two lines and stops being greppable.
    """
    covered = orch.covered_numbers(verdict.results)
    gates = orch.structural_gates(verdict.results)
    missing = orch.uncovered_numbers(verdict.results)

    table = Table(title=f"static battery — {experiment_id} trial {seq}",
                  show_header=True, header_style="dim")
    table.add_column("test")
    table.add_column("statistic", justify="right")
    table.add_column("threshold", justify="right")
    table.add_column("status")
    for r in verdict.results:
        table.add_row(
            r.name,
            "—" if r.statistic is None else f"{r.statistic:,.4f}",
            "—" if r.threshold is None else f"{r.threshold:,.4f}",
            f"[{'green' if r.passed else 'red'}]{r.status}[/]",
        )
    console.print(table)
    for r in verdict.results:
        console.print(f"\n[bold]{r.name}[/] — {r.detail}")

    console.print(
        f"\nran {_count(len(covered))} numbered tests ({', '.join(covered)}) and "
        f"{_count(len(gates))} structural gates ({', '.join(gates)}) against thresholds "
        f"version {verdict.thresholds_version}, on {artifact}, against "
        f"{verdict.trials_at_verdict} registered "
        f"{'trial' if verdict.trials_at_verdict == 1 else 'trials'}.", soft_wrap=True)

    if verdict.failures:
        console.print(
            f"[red]failed[/]: {', '.join(r.name for r in verdict.failures)}",
            soft_wrap=True)
    else:
        console.print("[green]every static result passed.[/]", soft_wrap=True)

    console.print(
        f"\nThis does not satisfy the eleven-test promotion gate. "
        f"{_count(len(missing))} of the eleven were not run here:", soft_wrap=True)
    for need, numbers in orch.unrun_by_requirement(verdict.results):
        console.print(f"  {', '.join(numbers)} — {need}.", soft_wrap=True)


@app.command("static")
def static(
    experiment_id: str = typer.Argument(..., help="The experiment, e.g. EXP-000042."),
    trial: int = typer.Option(..., "--trial",
                              help="The trial sequence. Required: the trial is named, "
                                   "never guessed, and never the headline artifact."),
) -> None:
    """Run the static battery against one verified sealed trial. Costs zero trials.

    Six of the eleven promotion tests plus the three structural gates. Every one of them
    reads the sealed result rather than re-running the engine, which is why this command
    spends no trial budget — and why it cannot, on its own, clear a strategy for promotion.
    """
    try:
        metrics_path = _registered_artifact(experiment_id, trial)
        verified = loader.verify(metrics_path)
        _bind_to_request(verified, experiment_id, trial, metrics_path)
    except loader.Unverifiable as exc:
        _refused(exc.diagnostic)
        raise typer.Exit(1) from exc

    # Measured once, here. A count re-read per test would deflate two Sharpes against two
    # different N and call the pair one verdict.
    trials = experiments.trial_count(experiment_id)
    ctx = _context(verified, trials)

    try:
        thresholds = orch.load_thresholds()
    except orch.ThresholdsError as exc:
        console.print(f"[red]static battery refused[/] — {exc}", soft_wrap=True)
        console.print("no falsification rows were written.", soft_wrap=True)
        raise typer.Exit(1) from exc

    verdict = orch.run_static_battery(ctx, thresholds)

    # The whole battery or none of it, and every row carrying the same N and the same
    # timestamp. `r.status` is the BatteryStatus the test itself concluded — never a
    # status re-derived from `passed` further down the pipe.
    artifact = metrics_path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    experiments.record_falsification_batch(
        experiment_id,
        [experiments.FalsificationResult(r.name, str(r.status), r.statistic, r.threshold)
         for r in verdict.results],
        trials_at_run=verdict.trials_at_verdict,
        artifact_path=artifact,
    )

    _report(verdict, experiment_id, trial, artifact)
    if not verdict.passed:
        raise typer.Exit(1)
