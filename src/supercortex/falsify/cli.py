"""`supercortex falsify` — the engine whose job is to kill strategies."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from supercortex.falsify import probes
from supercortex.registry import db, experiments
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
