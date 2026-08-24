"""The `supercortex` command line. The only sanctioned way to touch the registry."""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from supercortex import __version__
from supercortex.registry import db, experiments

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Project Supercortex — autonomous quant research desk.")
exp_app = typer.Typer(no_args_is_help=True, help="Experiments and trials.")
app.add_typer(exp_app, name="exp")

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
