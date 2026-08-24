"""`supercortex falsify` — the engine whose job is to kill strategies."""
from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from supercortex.falsify import probes
from supercortex.registry import db, experiments

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
    baseline = json.loads(headline.read_text(encoding="utf-8"))

    console.print(f"{len(specs)} probe runs — each is a registered trial and counts "
                  f"toward the budget.\n")
    results: dict[str, dict] = {}
    for spec in specs:
        with console.status(f"probe {spec.label}…"):
            metrics = trial.execute(experiment_id, agent_id=agent, probe=spec)
        trial.write_metrics(experiment_id, metrics)
        results[spec.label] = metrics
        console.print(f"  {spec.label:<18} PF {metrics['trades']['profit_factor_gross']:.4f}")

    passed, verdicts = probes.verdict(baseline, results, names)

    table = Table(title="test 1 — look-ahead battery", show_header=True, header_style="dim")
    for col in ("channel", "scramble", "advance", "delay", "verdict"):
        table.add_column(col, justify="right" if col not in ("channel", "verdict") else "left")
    colour = {"PASS": "green", "SUSPICIOUS": "red", "INERT": "yellow"}
    for v in verdicts:
        table.add_row(v.channel, f"{v.scramble_delta:+.1%}", f"{v.advance_delta:+.1%}",
                      f"{v.delay_delta:+.1%}",
                      f"[{colour[v.status]}]{v.status}[/]")
    console.print(table)
    for v in verdicts:
        console.print(f"\n[bold]{v.channel}[/] — {v.reasoning}")

    _record(experiment_id, verdicts, passed)
    console.print(f"\ntest 1: [{'green' if passed else 'red'}]"
                  f"{'PASS' if passed else 'FAIL'}[/]")
    if not passed:
        raise typer.Exit(1)


def _artifacts(experiment_id: str):
    with db.connect() as conn:
        row = conn.execute("SELECT slug FROM experiment WHERE id = ?",
                           (experiment_id,)).fetchone()
    if row is None:
        raise typer.BadParameter(f"unknown experiment {experiment_id}")
    return experiments.EXPERIMENTS_DIR / f"{experiment_id}-{row['slug']}" / "artifacts"


def _record(experiment_id: str, verdicts, passed: bool) -> None:
    trials = experiments.trial_count(experiment_id)
    with db.connect() as conn:
        for v in verdicts:
            conn.execute(
                "INSERT INTO falsification(experiment_id, test_name, statistic, threshold,"
                " passed, ran_at, artifact_path, trials_at_run)"
                " VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(experiment_id, test_name) DO UPDATE SET"
                " statistic=excluded.statistic, passed=excluded.passed,"
                " ran_at=excluded.ran_at, trials_at_run=excluded.trials_at_run",
                (experiment_id, f"1-lookahead-{v.channel}", v.advance_delta,
                 probes.MATERIAL, int(v.passed), db.now(), None, trials),
            )
