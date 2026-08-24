"""`supercortex exp run` — the only sanctioned way to execute a backtest.

Law 2 lives here. The order is deliberate and not negotiable:

    verify reproducibility inputs -> register the trial -> run -> seal

Registering *before* running is the whole point. A run that crashes, or that an agent
abandons half way, has still consumed a look at the data and still counts toward the
Deflated Sharpe Ratio. Registering afterwards would quietly drop exactly the trials that
make the multiple-testing correction meaningful.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from supercortex.backtest.runner import BacktestSpec, ProbeSpec
from supercortex.backtest.runner import run as run_backtest
from supercortex.costs.model import CostModel, TaxRegime, apply, compare_regimes
from supercortex.registry import db, experiments


def load_config(experiment_id: str) -> tuple[Path, dict[str, Any]]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT slug FROM experiment WHERE id = ?", (experiment_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"unknown experiment {experiment_id}")
    path = experiments.EXPERIMENTS_DIR / f"{experiment_id}-{row['slug']}" / "config.yaml"
    return path, yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def dataset_snapshot(dataset_ids: list[str]) -> str:
    """A hash over the exact datasets this trial reads.

    Concatenating the registered sha256s means the snapshot changes if any underlying
    file changes — so a trial can never silently be compared against different data.
    """
    if not dataset_ids:
        return "no-datasets"
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT id, sha256 FROM dataset WHERE id IN "
            f"({','.join('?' * len(dataset_ids))}) ORDER BY id",
            dataset_ids,
        ).fetchall()
    missing = set(dataset_ids) - {r["id"] for r in rows}
    if missing:
        raise RuntimeError(
            f"config references unregistered datasets: {sorted(missing)}. "
            "A trial may not read data that has not been verified and carded."
        )
    return db.sha256_text("|".join(f"{r['id']}:{r['sha256']}" for r in rows))


def execute(
    experiment_id: str,
    *,
    agent_id: str | None = None,
    note: str | None = None,
    probe: ProbeSpec | None = None,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    """Register, run, cost, seal. Returns the metrics that were written."""
    config_path, config = load_config(experiment_id)
    data_cfg = config.get("data") or {}
    snapshot = dataset_snapshot(list(data_cfg.get("datasets") or []))

    cost_cfg = dict(config.get("cost_model") or {})
    model = CostModel(**cost_cfg)

    # 1. REGISTER — before a single bar is processed.
    seq = experiments.open_trial(
        experiment_id,
        config_text=config_path.read_text(encoding="utf-8"),
        data_snapshot=snapshot,
        cost_model_version=model.version,
        seed=int(config.get("seed", 42)),
        agent_id=agent_id,
        note=note or (None if probe is None or probe.is_baseline else f"probe: {probe.label}"),
        strict=not allow_dirty,
    )

    # 2. RUN — any failure from here seals the trial as 'error'; it still counts.
    try:
        strategy_ref = config.get("strategy")
        if not strategy_ref:
            raise RuntimeError(
                f"{experiment_id}: config.yaml declares no `strategy`. The strategy is a "
                "hashed parameter, not a runner default."
            )
        spec = BacktestSpec(
            strategy=strategy_ref,
            symbol=data_cfg.get("symbol", "BTCUSDT"),
            market=data_cfg.get("market", "um"),
            interval=data_cfg.get("interval", "1h"),
            start_ms=data_cfg.get("start_ms"),
            end_ms=data_cfg.get("end_ms"),
            starting_balance=float(config.get("starting_balance", 100_000.0)),
            leverage=float(config.get("leverage", 1.0)),
            latency_ms=int(config.get("latency_ms", 250)),
            seed=int(config.get("seed", 42)),
            params=config.get("params") or {},
            probe=probe or ProbeSpec(),
        )
        trades, diagnostics = run_backtest(spec)
    except Exception:
        experiments.close_trial(experiment_id, seq, "error")
        raise

    # 3. COST — the four numbers, under every regime.
    results = compare_regimes(trades, model)
    conservative = results[TaxRegime.VDA_STRICT]
    metrics = _metrics(experiment_id, seq, spec, model, conservative, results, diagnostics)

    # 4. SEAL.
    experiments.close_trial(experiment_id, seq, "ok", metrics,
                            is_headline=(probe is None or probe.is_baseline))
    return metrics


def _metrics(
    experiment_id: str,
    seq: int,
    spec: BacktestSpec,
    model: CostModel,
    conservative: Any,
    results: dict[TaxRegime, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    trials_to_date = experiments.trial_count(experiment_id)
    return {
        "experiment_id": experiment_id,
        "trial_seq": seq,
        "trials_consumed_to_date": trials_to_date,
        "cost_model_version": model.version,
        "regime": conservative.regime.value,
        "engine": {**diagnostics},
        "window": {
            "interval": spec.interval,
            "start_ms": spec.start_ms,
            "end_ms": spec.end_ms,
            "latency_ms": spec.latency_ms,
        },
        # Run configuration belongs in the artifact, not only in config.yaml. Omitting it
        # meant a report could not state the starting balance without typing a number the
        # artifact did not contain — which the anti-fabrication hook correctly rejected.
        "run_config": {
            "starting_balance": spec.starting_balance,
            "leverage": spec.leverage,
            "seed": spec.seed,
        },
        "trades": {
            "n_trades": conservative.n_trades,
            "n_wins": conservative.n_wins,
            "win_rate": round(conservative.win_rate, 6),
            "turnover": round(conservative.turnover, 2),
            "profit_factor_gross": round(conservative.profit_factor_gross, 6),
            "profit_factor_net": round(conservative.profit_factor_net, 6),
            "break_even_profit_factor": round(conservative.break_even_profit_factor, 6),
            "clears_break_even": bool(
                conservative.profit_factor_gross >= conservative.break_even_profit_factor
            ),
        },
        # A LIST of pairs, not a dict: `json.dumps(sort_keys=True)` alphabetises dict
        # keys, which would silently scramble a ladder whose only job is to be monotonic.
        "four_numbers_ordered": [[label, round(value, 2)]
                                 for label, value in conservative.four_numbers()],
        "four_numbers": {label: round(value, 2)
                         for label, value in conservative.four_numbers()},
        "detail": {
            "gross_pnl": round(conservative.gross_pnl, 2),
            "execution_costs": round(conservative.execution_costs, 2),
            "funding_pnl": round(conservative.funding_pnl, 2),
            "tds_withheld": round(conservative.tds_withheld, 2),
            "tds_credited": round(conservative.tds_credited, 2),
            "tds_carry_cost": round(conservative.tds_carry_cost, 2),
            "tax_total": round(conservative.tax_total, 2),
            "tax_payable": round(conservative.tax_payable, 2),
        },
        "regimes": {
            regime.value: {
                "net_after_tax": round(r.net_after_tax, 2),
                "tax_total": round(r.tax_total, 2),
                "survives": bool(r.survives_tax),
            }
            for regime, r in results.items()
        },
        "verdict": {
            "survives_conservative_regime": bool(conservative.survives_tax),
            "taxed_while_losing": bool(conservative.taxed_while_losing),
            "contingent_on_tax_question": bool(
                not conservative.survives_tax
                and results[TaxRegime.BUSINESS_INCOME].survives_tax
            ),
        },
    }


def write_metrics(experiment_id: str, metrics: dict[str, Any]) -> Path:
    """Path of the artifact `close_trial` already wrote. Probes get their own file."""
    with db.connect() as conn:
        slug = conn.execute(
            "SELECT slug FROM experiment WHERE id = ?", (experiment_id,)
        ).fetchone()["slug"]
    artifacts = experiments.EXPERIMENTS_DIR / f"{experiment_id}-{slug}" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    probe = metrics.get("engine", {}).get("probe") or {}
    label = probe.get("label", "baseline")
    name = "metrics.json" if label == "baseline" else f"metrics-probe-{label}.json"
    path = artifacts / name
    path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
    return path


__all__ = ["apply", "dataset_snapshot", "execute", "load_config", "write_metrics"]
