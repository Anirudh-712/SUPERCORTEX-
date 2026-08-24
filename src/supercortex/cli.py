"""The `supercortex` command line. The only sanctioned way to touch the registry."""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from supercortex import __version__
from supercortex.costs import cli as costs_cli
from supercortex.data import cli as data_cli
from supercortex.falsify import cli as falsify_cli
from supercortex.health import hook_checks
from supercortex.lifecycle import machine as lifecycle
from supercortex.registry import db, experiments
from supercortex.report.experiment import render_profit_factor

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Project Supercortex — autonomous quant research desk.")
exp_app = typer.Typer(no_args_is_help=True, help="Experiments and trials.")
app.add_typer(exp_app, name="exp")
app.add_typer(data_cli.app, name="data")
app.add_typer(costs_cli.app, name="costs")
app.add_typer(falsify_cli.app, name="falsify")
life_app = typer.Typer(no_args_is_help=True, help="Strategy lifecycle transitions.")
app.add_typer(life_app, name="lifecycle")

console = Console()
DISK_FLOOR_GB = 2.0


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show version and exit."),
) -> None:
    if version:
        console.print(f"supercortex {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


@app.command()
def init() -> None:
    """Create registry.sqlite and verify the append-only guarantees."""
    path = db.init()
    with db.connect() as conn:
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        triggers = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name")]
    console.print(f"[green]registry initialised[/] {path.name}")
    console.print(f"  tables   {', '.join(tables)}")
    console.print(f"  triggers [bold]{', '.join(triggers)}[/]  ← history is immutable")


@app.command()
def health() -> None:
    """One-screen system health. Written to 00-dashboard/health.md by launchd."""
    free = db.free_gb()
    disk_ok = free >= DISK_FLOOR_GB
    rows = experiments.summary()
    open_trials = sum(r["trials_open"] or 0 for r in rows)
    over = [r["id"] for r in rows if r["over_budget"]]

    table = Table(show_header=True, header_style="dim")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    table.add_row("Disk free", "🟢" if disk_ok else "🔴",
                  f"{free:.1f} GB (floor {DISK_FLOOR_GB} GB)")
    table.add_row("Registry", "🟢" if db.DB_PATH.exists() else "🔴",
                  f"{len(rows)} experiments, {sum(r['trials_consumed'] for r in rows)} trials")
    table.add_row(
        "Open trials", "🟢" if open_trials == 0 else "🟡",
        f"{open_trials} unsealed — `supercortex exp reap` if stale",
    )
    table.add_row(
        "Trial budgets", "🟢" if not over else "🟡",
        "all within budget" if not over else f"over: {', '.join(over)}",
    )
    killed = (db.PROJECT_ROOT / "06-risk/KILL").exists()
    table.add_row(
        "Kill switch", "🔴 ENGAGED" if killed else "🟢",
        "trading halted" if killed else "not engaged",
    )
    console.print(table)
    if not disk_ok:
        raise typer.Exit(1)


@exp_app.command("register")
def exp_register(
    title: str = typer.Argument(..., help="What are you testing?"),
    budget: int = typer.Option(..., "--budget", "-b",
                               help="Trial budget, declared BEFORE research begins."),
    agent: str | None = typer.Option(None, "--agent", help="Owning agent id."),
) -> None:
    """Register an experiment. Creates the folder and the pre-registration stubs."""
    exp = experiments.register(title, budget, agent)
    console.print(f"[green]{exp.id}[/] registered — budget {budget} trials")
    console.print(f"  {exp.dir.relative_to(db.PROJECT_ROOT)}/")
    console.print("\n[bold]Next:[/] write hypothesis.md, then [bold]commit it[/] — "
                  "a trial refuses to run until the hypothesis is in git history.")


@exp_app.command("open")
def exp_open(
    experiment_id: str = typer.Argument(...),
    data_snapshot: str = typer.Option("unverified", "--data-snapshot"),
    agent: str | None = typer.Option(None, "--agent"),
    note: str | None = typer.Option(None, "--note"),
    allow_dirty: bool = typer.Option(
        False, "--allow-dirty",
        help="Escape hatch for scaffolding only. Never for real trials.",
    ),
) -> None:
    """Open a trial. Every backtest must pass through here."""
    with db.connect() as conn:
        row = conn.execute("SELECT slug FROM experiment WHERE id = ?", (experiment_id,)).fetchone()
    if row is None:
        console.print(f"[red]unknown experiment {experiment_id}[/]")
        raise typer.Exit(1)
    config = experiments.EXPERIMENTS_DIR / f"{experiment_id}-{row['slug']}" / "config.yaml"
    try:
        seq = experiments.open_trial(
            experiment_id,
            config_text=config.read_text(encoding="utf-8"),
            data_snapshot=data_snapshot,
            cost_model_version="india-vda-1.0.0",
            seed=42, agent_id=agent, note=note, strict=not allow_dirty,
        )
    except RuntimeError as exc:
        console.print(f"[red]refused:[/] {exc}")
        raise typer.Exit(1) from exc
    n = experiments.trial_count(experiment_id)
    console.print(f"[green]{experiment_id} trial {seq}[/] open — {n} trials consumed to date")


@exp_app.command("run")
def exp_run(
    experiment_id: str = typer.Argument(...),
    agent: str | None = typer.Option(None, "--agent"),
    note: str | None = typer.Option(None, "--note"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty"),
) -> None:
    """Register a trial, run the backtest, apply the cost model, seal the trial.

    Probes live in `supercortex falsify probe` — a diagnostic that deliberately corrupts
    its inputs is not a variant of the baseline run.
    """
    from supercortex.backtest import trial

    try:
        metrics = trial.execute(
            experiment_id, agent_id=agent, note=note, allow_dirty=allow_dirty,
        )
    except (RuntimeError, KeyError) as exc:
        console.print(f"[red]refused:[/] {exc}")
        raise typer.Exit(1) from exc

    path = trial.write_metrics(experiment_id, metrics)
    t = metrics["trades"]

    console.print(
        f"[green]{experiment_id} trial {metrics['trial_seq']}[/] sealed — "
        f"{metrics['trials_consumed_to_date']} trials consumed to date"
    )
    console.print(f"  engine reconciliation diff "
                  f"{metrics['engine']['reconciliation_diff']}")

    ladder = Table(show_header=False, title="the four numbers (VDA_STRICT)")
    for label, value in metrics["four_numbers"].items():
        ladder.add_row(label, f"{value:,.2f}")
    console.print(ladder)

    stats = Table(show_header=False)
    stats.add_row("trades", f"{t['n_trades']} ({t['win_rate']:.1%} win rate)")
    stats.add_row("profit factor", render_profit_factor(t, ".3f"))
    stats.add_row("break-even PF", f"{t['break_even_profit_factor']:.3f}")
    stats.add_row("clears break-even", "yes" if t["clears_break_even"] else "[red]NO[/]")
    for regime, r in metrics["regimes"].items():
        stats.add_row(regime, f"{r['net_after_tax']:>12,.0f}  "
                              f"{'survives' if r['survives'] else '[red]fails[/]'}")
    console.print(stats)

    v = metrics["verdict"]
    if v["taxed_while_losing"]:
        console.print("[red]TAX BILL ON A LOSING RUN[/]")
    if v["contingent_on_tax_question"]:
        console.print("[yellow]CONTINGENT[/] — survives only under BUSINESS_INCOME. "
                      "That is a bet on an unsettled tax question, not a result. ADR-012.")
    console.print(f"\n  metrics: {path.relative_to(db.PROJECT_ROOT)}")


@exp_app.command("report")
def exp_report(experiment_id: str = typer.Argument(...)) -> None:
    """Regenerate results.md from the experiment's sealed artifacts."""
    from supercortex.report import experiment as report

    path = report.render(experiment_id)
    console.print(f"[green]written[/] {path.relative_to(db.PROJECT_ROOT)}")


@exp_app.command("reap")
def exp_reap(hours: float = typer.Option(6.0, "--hours")) -> None:
    """Mark stale open trials abandoned. They still count toward the trial total."""
    reaped = experiments.reap_stale(hours)
    if not reaped:
        console.print("no stale trials")
        return
    for exp_id, seq in reaped:
        console.print(f"[yellow]abandoned[/] {exp_id} trial {seq}")
    console.print(f"\n{len(reaped)} marked abandoned — they remain in the trial count by design.")


@exp_app.command("list")
def exp_list() -> None:
    """Every experiment and its true trial count."""
    rows = experiments.summary()
    if not rows:
        console.print("no experiments yet — `supercortex exp register`")
        return
    table = Table(show_header=True, header_style="dim")
    for col in ("ID", "Title", "Status", "Trials", "Budget", "Abandoned"):
        table.add_column(col)
    for r in rows:
        over = r["over_budget"]
        table.add_row(r["id"], r["title"][:40], r["status"],
                      f"[red]{r['trials_consumed']}[/]" if over else str(r["trials_consumed"]),
                      str(r["trial_budget"]), str(r["trials_abandoned"] or 0))
    console.print(table)


@app.command("doctor")
def doctor() -> None:
    """Verify the guarantees this desk depends on. Run after any change to the registry."""
    checks: list[tuple[str, bool, str]] = []

    free = db.free_gb()
    checks.append(("disk above floor", free >= DISK_FLOOR_GB, f"{free:.1f} GB free"))
    checks.append(("registry exists", db.DB_PATH.exists(), db.DB_PATH.name))

    hooks = db.PROJECT_ROOT / ".claude/hooks"
    for name in ("guard_write.py", "guard_bash.py", "no_fabricated_numbers.py"):
        path = hooks / name
        ok = path.exists() and path.stat().st_mode & 0o111 != 0
        checks.append((f"hook {name}", ok, "executable" if ok else "MISSING or not executable"))

    # Preventive validation is only a guarantee while it is REGISTERED. A hook that exists
    # but is not wired leaves the repository looking protected while nothing fires.
    for check in hook_checks(db.PROJECT_ROOT):
        checks.append((check.name, check.ok, check.detail))

    # the guarantee that everything else rests on
    if db.DB_PATH.exists():
        probe = db.PROJECT_ROOT / ".doctor-probe.sqlite"
        probe.unlink(missing_ok=True)
        db.init(probe)
        with db.connect(probe) as conn:
            conn.execute("INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
                         " VALUES ('EXP-000001','probe','probe','RESEARCH',1,?)", (db.now(),))
            conn.execute("INSERT INTO trial(experiment_id,seq,config_hash,code_commit,"
                         "data_snapshot,cost_model_version,seed,started_at)"
                         " VALUES ('EXP-000001',1,'c','g','d','v',1,?)", (db.now(),))
            try:
                conn.execute("DELETE FROM trial WHERE seq = 1")
                blocked = False
            except Exception:
                blocked = True
        probe.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(probe) + suffix).unlink(missing_ok=True)
        checks.append(("trial table is append-only", blocked,
                       "DELETE rejected by trigger" if blocked else "DELETE SUCCEEDED — CRITICAL"))

    table = Table(show_header=True, header_style="dim")
    table.add_column("Check")
    table.add_column("")
    table.add_column("Detail")
    for name, ok, detail in checks:
        table.add_row(name, "🟢" if ok else "🔴", detail)
    console.print(table)
    if not all(ok for _, ok, _ in checks):
        raise typer.Exit(1)
    console.print("\n[green]all guarantees hold[/]")


if __name__ == "__main__":
    app()


@life_app.command("state")
def lifecycle_state(experiment_id: str = typer.Argument(...)) -> None:
    """Current state and the full transition history."""
    state = lifecycle.current_state(experiment_id)
    console.print(f"[bold]{experiment_id}[/] is [bold]{state}[/]")
    allowed = lifecycle.TRANSITIONS.get(state, set())
    console.print(f"  may go to: {', '.join(sorted(allowed)) or 'nowhere'} (or KILLED)")
    events = lifecycle.history(experiment_id)
    if not events:
        console.print("  no transitions recorded")
        return
    table = Table(show_header=True, header_style="dim")
    for col in ("when", "from", "to", "decision", "approved by"):
        table.add_column(col)
    for e in events:
        table.add_row(str(e["at"]), str(e["from_state"] or "-"), str(e["to_state"]),
                      str(e["gate_report_path"] or "-"), str(e["approved_by"] or "-"))
    console.print(table)


@life_app.command("promote")
def lifecycle_promote(
    experiment_id: str = typer.Argument(...),
    to: str = typer.Option(..., "--to"),
    approved_by: str | None = typer.Option(None, "--approved-by"),
    decision: str | None = typer.Option(None, "--decision"),
) -> None:
    """Advance a state. Refuses whenever a gate cannot be verified."""
    try:
        was = lifecycle.transition(experiment_id, to.upper(),
                                   decision_id=decision, approved_by=approved_by)
    except (lifecycle.TransitionRefused, KeyError) as exc:
        console.print(f"[red]refused:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]{experiment_id}[/] {was} -> {to.upper()}")


@life_app.command("kill")
def lifecycle_kill(
    experiment_id: str = typer.Argument(...),
    decision: str = typer.Option(..., "--decision", help="DEC-NNNNNN note recording why."),
) -> None:
    """Kill a line of research. Requires a decision note; a killed experiment is a result."""
    try:
        was = lifecycle.transition(experiment_id, "KILLED", decision_id=decision)
    except (lifecycle.TransitionRefused, KeyError) as exc:
        console.print(f"[red]refused:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]{experiment_id}[/] {was} -> KILLED  ({decision})")
    console.print("[dim]A killed experiment is evidence. Mine it later.[/]")
