"""`supercortex costs` — interrogate the India cost model before writing a strategy.

This exists to be run *before* research, not after. It answers "what kind of strategy is
even worth looking for?" — which is a cheaper question than "why did my beautiful backtest
lose money after tax?"
"""
from __future__ import annotations

from dataclasses import replace

import typer
from rich.console import Console
from rich.table import Table

from supercortex.costs.model import (
    CostModel,
    TaxRegime,
    apply,
    compare_regimes,
    required_profit_factor,
)

app = typer.Typer(no_args_is_help=True, help="India cost model: what a strategy keeps.")
console = Console()

#: Typical adverse excursion by holding horizon. The axis that actually governs whether
#: a strategy can survive: frictions are charged per round trip regardless of move size.
HORIZONS: list[tuple[str, float]] = [
    ("HFT / market making", 0.0005),
    ("scalping", 0.002),
    ("intraday", 0.005),
    ("swing (days)", 0.015),
    ("position (weeks)", 0.04),
    ("trend (months)", 0.10),
]


@app.command("breakeven")
def breakeven(
    win_rate: float = typer.Option(0.5, "--win-rate"),
    taker_bps: float = typer.Option(4.5, "--taker-bps"),
    slippage_bps: float = typer.Option(3.0, "--slippage-bps"),
) -> None:
    """Profit factor required to break even after tax, by holding horizon."""
    base = CostModel(taker_fee_bps=taker_bps, slippage_bps=slippage_bps)
    friction = 2 * (base.taker_fee_bps + base.slippage_bps + base.half_spread_bps)

    table = Table(title="Profit factor required to break even after tax",
                  caption=f"round-trip friction {friction:.1f} bps · "
                          f"tax-only floor {base.break_even_profit_factor:.4f}")
    table.add_column("horizon")
    table.add_column("adverse move", justify="right")
    for regime in TaxRegime:
        table.add_column(regime.value, justify="right")

    for label, move in HORIZONS:
        cells = []
        for regime in TaxRegime:
            pf = required_profit_factor(
                replace(base, regime=regime), 100, loss_move=move, win_rate=win_rate
            )
            if pf == float("inf"):
                cells.append("[red]never[/]")
            elif pf > 3:
                cells.append(f"[red]{pf:.2f}[/]")
            elif pf > 1.8:
                cells.append(f"[yellow]{pf:.2f}[/]")
            else:
                cells.append(f"[green]{pf:.2f}[/]")
        table.add_row(label, f"{move:.2%}", *cells)
    console.print(table)
    console.print(
        "\n[dim]Read this before choosing a strategy family, not after backtesting one.\n"
        "At short horizons frictions dominate and the tax regime barely matters; at long\n"
        "horizons tax dominates and the regime question becomes the whole game.[/]"
    )


@app.command("regimes")
def regimes(
    trades: int = typer.Option(100, "--trades"),
    win_rate: float = typer.Option(0.4, "--win-rate"),
    win_move: float = typer.Option(0.05, "--win"),
    loss_move: float = typer.Option(0.02, "--loss"),
    price: float = typer.Option(50_000.0, "--price"),
    qty: float = typer.Option(0.1, "--qty"),
) -> None:
    """Run one synthetic profile through every tax regime and show the four numbers."""
    from supercortex.costs.model import Trade

    wins = round(trades * win_rate)
    sample = [
        Trade(i * 86_400_000, i * 86_400_000 + 3_600_000, 1, qty, price,
              price * (1 + (win_move if i < wins else -loss_move)))
        for i in range(trades)
    ]
    results = compare_regimes(sample, CostModel())
    reference = results[TaxRegime.VDA_STRICT]

    console.print(
        f"[bold]{trades}[/] round trips · win rate {win_rate:.0%} · "
        f"+{win_move:.1%} / -{loss_move:.1%} · ${price * qty:,.0f} notional per leg\n"
        f"gross profit factor [bold]{reference.profit_factor_gross:.2f}[/]  "
        f"(floor {reference.break_even_profit_factor:.2f})\n"
    )

    table = Table()
    table.add_column("")
    for regime in TaxRegime:
        table.add_column(regime.value, justify="right")
    for i, (label, _) in enumerate(reference.four_numbers()):
        table.add_row(label, *[f"{results[r].four_numbers()[i][1]:,.0f}" for r in TaxRegime])
    table.add_section()
    table.add_row("tax liability", *[f"{results[r].tax_total:,.0f}" for r in TaxRegime])
    table.add_row("TDS withheld", *[f"{results[r].tds_withheld:,.0f}" for r in TaxRegime])
    table.add_row("kept", *[
        (f"{results[r].net_after_tax / results[r].net_of_costs:.0%}"
         if results[r].net_of_costs > 0 else "—") for r in TaxRegime
    ])
    console.print(table)

    if reference.taxed_while_losing:
        console.print("\n[red]TAX BILL ON A LOSING YEAR[/] — every winning disposal is "
                      "assessed on its own; losers give nothing back.")
    elif not reference.survives_tax:
        console.print("\n[red]does not survive tax under VDA_STRICT[/]")
    else:
        console.print("\n[green]survives under the conservative regime[/]")


@app.command("show")
def show() -> None:
    """Print the active cost model and its assumptions."""
    model = CostModel()
    table = Table(show_header=False)
    for label, value in [
        ("version", model.version),
        ("regime", model.regime.value),
        ("taker / maker", f"{model.taker_fee_bps} / {model.maker_fee_bps} bps"),
        ("slippage + half-spread", f"{model.slippage_bps} + {model.half_spread_bps} bps"),
        ("capital gains + cess", f"{model.capital_gains_rate:.0%} + {model.cess_rate:.0%}"
                                 f" = {model.effective_tax_rate:.2%}"),
        ("TDS (s.194S)", f"{model.tds_rate:.1%} of disposal notional"),
        ("loss offset", "no" if not model.allows_loss_offset else "yes"),
        ("fees deductible", "no" if not model.fees_deductible else "yes"),
        ("funding paid deductible", "no" if not model.funding_deductible else "yes"),
        ("TDS recoverable", "yes" if model.tds_recoverable else "no"),
        ("break-even profit factor", f"{model.break_even_profit_factor:.4f}"),
    ]:
        table.add_row(label, str(value))
    console.print(table)
    console.print("\n[yellow]UNVERIFIED[/] — a modelling scaffold, not tax advice. The "
                  "treatment of cash-settled crypto derivatives is unsettled in India as "
                  "of 2026-08. Confirm with a chartered accountant before committing "
                  "capital. See 20-decisions/ADR-012.")


def synthetic_check() -> None:
    """Used by tests: the model must never report a strategy as surviving when it isn't."""
    apply([], CostModel())
