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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from supercortex.backtest import evidence
from supercortex.backtest.runner import BacktestSpec, ProbeSpec
from supercortex.backtest.runner import run as run_backtest
from supercortex.costs.model import (
    CostModel,
    TaxRegime,
    apply,
    compare_regimes,
    profit_factor_json,
)
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


def model_from_config(config: dict[str, Any]) -> CostModel:
    """The effective cost model a config implies. One definition, so the verifier derives
    exactly what the producer built rather than re-stating its defaults."""
    return CostModel(**dict(config.get("cost_model") or {}))


def run_values_from_config(config: dict[str, Any]) -> dict[str, Any]:
    """The effective run contract a config implies, with the producer's own defaults."""
    data = config.get("data") or {}
    return {
        "starting_balance": float(config.get("starting_balance", 100_000.0)),
        "leverage": float(config.get("leverage", 1.0)),
        "seed": int(config.get("seed", 42)),
        "latency_ms": int(config.get("latency_ms", 250)),
        "interval": data.get("interval", "1h"),
        "symbol": data.get("symbol", "BTCUSDT"),
        "market": data.get("market", "um"),
    }


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


#: Read back from the open trial row — the registry is authoritative for these, because
#: the row is what a later reader verifies against. Evidence agreeing with the caller but
#: not with the row would be evidence for a trial that does not exist.
ROW_SOURCED = (
    "experiment_id", "trial_seq", "config_hash", "code_commit", "data_snapshot", "seed",
)


def serialise_metrics(metrics: dict[str, Any]) -> str:
    """Render the artifact exactly as the writer will, or refuse.

    Run inside the guarded region so a document that cannot be written never reaches the
    seal. `allow_nan=False` is the point: the default emits bare `NaN`, producing a file
    that looks sealed and that no conforming parser can read.
    """
    try:
        return experiments.dump_metrics(metrics)
    except (ValueError, TypeError) as exc:
        raise evidence.EvidenceError(
            f"metrics cannot be serialised as strict JSON ({exc}). An artifact that "
            f"cannot be written is not a result."
        ) from exc


def trial_identity(experiment_id: str, seq: int, config_path: Path,
                   dataset_ids: list[str]) -> dict[str, Any]:
    """Trial identity, from three different sources — stated, because they are not equal.

    * **Row-sourced** (`ROW_SOURCED`): experiment id, sequence, config hash, code commit,
      data snapshot, seed. Authoritative, and re-read here rather than reused from the
      caller's variables.
    * **Path-derived**: `config_path`, rendered relative to the project root. The row
      stores the config's *hash*, never its location.
    * **Caller-sourced**: `dataset_ids`, which come from the experiment's config. The row
      stores only the `data_snapshot` hash computed over them, so the id list cannot be
      recovered from the registry and is recorded here to make the snapshot checkable.
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT experiment_id, seq, config_hash, code_commit, data_snapshot, seed"
            " FROM trial WHERE experiment_id = ? AND seq = ?",
            (experiment_id, seq),
        ).fetchone()
    if row is None:
        raise evidence.EvidenceError(
            f"{experiment_id} trial {seq} is not in the registry; a trial must be "
            f"registered before it can produce evidence"
        )
    return {
        "experiment_id": row["experiment_id"],
        "trial_seq": int(row["seq"]),
        "config_hash": row["config_hash"],
        "config_path": config_path.relative_to(db.PROJECT_ROOT).as_posix(),
        "code_commit": row["code_commit"],
        "data_snapshot": row["data_snapshot"],
        "dataset_ids": list(dataset_ids),
        "seed": int(row["seed"]),
    }


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

    model = model_from_config(config)

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
        run_values = run_values_from_config(config)
        spec = BacktestSpec(
            strategy=strategy_ref,
            start_ms=data_cfg.get("start_ms"),
            end_ms=data_cfg.get("end_ms"),
            params=config.get("params") or {},
            probe=probe or ProbeSpec(),
            **run_values,
        )
        trades, diagnostics = run_backtest(spec)

        # 3. COST — the four numbers, under every regime.
        results = compare_regimes(trades, model)
        conservative = results[TaxRegime.VDA_STRICT]

        # 4. EVIDENCE — inside the guarded region on purpose. A trial whose evidence
        # cannot be built is not a result: it seals as `error`, still counting against the
        # budget because it still consumed a look at the data.
        identity = trial_identity(experiment_id, seq, config_path,
                                  list(data_cfg.get("datasets") or []))
        metrics = _metrics(experiment_id, seq, spec, model, conservative, results,
                           diagnostics, identity, trades)
        serialise_metrics(metrics)
    except Exception:
        experiments.close_trial(experiment_id, seq, "error")
        raise

    # 5. SEAL — with a rollback. `close_trial` writes the artifacts and *then* updates
    # the row, so a failure between the two would leave a headline on disk describing a
    # trial that never sealed.
    _seal(experiment_id, seq, metrics,
          is_headline=(probe is None or probe.is_baseline),
          artifacts=config_path.parent / "artifacts")
    return metrics


def _seal(experiment_id: str, seq: int, metrics: dict[str, Any], *,
          is_headline: bool, artifacts: Path) -> None:
    """Seal `ok`, or undo the half-written result and seal `error` instead.

    **Exception recovery, not crash-atomicity.** SQLite and the filesystem are two stores
    and nothing here spans them transactionally: a power loss between `close_trial`'s
    artifact write and its row update leaves exactly the inconsistency this function
    exists to repair, and no amount of ordering fixes that. What is guaranteed is
    narrower and still worth having — a *raised exception* anywhere in the seal cannot
    leave the row open, and a filesystem cleanup failure cannot stop the database seal
    from being attempted while the registry is reachable.

    A headline is the DESIGNATED result of an experiment, not merely its most recent
    file. Leaving one from a trial that never sealed would let a reader quote a number the
    registry does not vouch for, so it is restored to what it was — an earlier trial's
    result survives — or removed if there was none.
    """
    headline = artifacts / "metrics.json"
    per_trial = artifacts / f"metrics-trial-{seq:04d}.json"
    #: Distinguishes "there was no headline" from "we never got far enough to look".
    unsnapshotted = object()
    previous: bytes | object | None = unsnapshotted
    failures: list[BaseException] = []

    def attempt(action: Callable[[], object]) -> None:
        """Best effort. A cleanup failure is recorded, never allowed to mask the seal."""
        try:
            action()
        except Exception as exc:
            failures.append(exc)

    try:
        # Snapshotting is inside the try. Reading it outside would mean a headline that
        # cannot be read leaves the row open forever.
        previous = headline.read_bytes() if (is_headline and headline.exists()) else None
        experiments.close_trial(experiment_id, seq, "ok", metrics,
                                is_headline=is_headline)
    except Exception as exc:
        if is_headline and previous is not unsnapshotted:
            if previous is None:
                attempt(lambda: headline.unlink(missing_ok=True))
            else:
                snapshot = previous
                assert isinstance(snapshot, bytes)
                attempt(lambda: headline.write_bytes(snapshot))
        # If the snapshot never happened, close_trial never ran, so the headline on disk
        # is untouched and must be left exactly as it is.
        attempt(lambda: per_trial.unlink(missing_ok=True))

        try:
            experiments.close_trial(experiment_id, seq, "error")
        except Exception as seal_failure:
            failures.append(seal_failure)

        # Everything that went wrong stays visible. Each failure is linked to the one
        # before it, so walking __cause__ reaches all of them — chaining them all to
        # `exc` instead would leave every failure but the last unreachable.
        if not failures:
            raise
        link: BaseException = exc
        for failure in failures[:-1]:
            failure.__cause__ = link
            link = failure
        raise failures[-1] from link


def _falsification_input(
    spec: BacktestSpec,
    model: CostModel,
    trades: list[Any],
    identity: dict[str, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    """The versioned block a later falsification run reads instead of guessing."""
    funding_series = diagnostics.get("funding_series")
    if not isinstance(funding_series, dict):
        raise evidence.EvidenceError(
            "the runner returned no funding evidence; a run that supplied no funding must "
            "say so explicitly rather than leave it absent"
        )
    series = diagnostics.get("series")
    if not isinstance(series, dict):
        raise evidence.EvidenceError(
            "the runner returned no series evidence; the bars actually delivered to the "
            "engine are the only thing that can be hashed"
        )
    synthetic = spec.synthetic_source or {}
    generator = synthetic.get("generator")
    if generator is not None and not isinstance(generator, str):
        raise evidence.EvidenceError(
            f"synthetic_source.generator is {type(generator).__name__}, expected a "
            f"'module:function' reference"
        )

    price_series = evidence.price_series_evidence(
        facts=series,
        source=series["source"],
        interval=spec.interval,
        start_ms=spec.start_ms,
        end_ms=spec.end_ms,
        probe=spec.probe.as_dict(),
        dataset_ids=identity["dataset_ids"],
        data_snapshot=identity["data_snapshot"],
        symbol=spec.symbol,
        market=spec.market,
        generator=generator,
        generator_parameters=synthetic.get("parameters"),
    )
    return evidence.build(
        funding_series=dict(funding_series),
        identity=identity,
        trades=trades,
        model=model,
        run_config={
            "starting_balance": spec.starting_balance,
            "leverage": spec.leverage,
            "seed": spec.seed,
            "latency_ms": spec.latency_ms,
            "interval": spec.interval,
            "symbol": spec.symbol,
            "market": spec.market,
        },
        price_series=price_series,
        reconciliation={
            key: diagnostics[key]
            for key in ("engine_realized_pnl", "our_gross_pnl", "reconciliation_diff")
            if key in diagnostics
        },
    )


def _metrics(
    experiment_id: str,
    seq: int,
    spec: BacktestSpec,
    model: CostModel,
    conservative: Any,
    results: dict[TaxRegime, Any],
    diagnostics: dict[str, Any],
    identity: dict[str, Any],
    trades: list[Any],
) -> dict[str, Any]:
    trials_to_date = experiments.trial_count(experiment_id)
    falsification_input = _falsification_input(spec, model, trades, identity, diagnostics)
    return {
        "experiment_id": experiment_id,
        "trial_seq": seq,
        "trials_consumed_to_date": trials_to_date,
        "cost_model_version": model.version,
        "regime": conservative.regime.value,
        "engine": {k: v for k, v in diagnostics.items()
                   if k not in ("series", "funding_series")},
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
            # A ratio has three states. Finite is the number it always was; unbounded
            # and undefined are null with an explicit tag, because `Infinity` is not JSON
            # and a large sentinel is a number nobody could tell from a real one.
            "profit_factor_gross": profit_factor_json(
                conservative.profit_factor_gross, conservative.profit_factor_gross_state),
            "profit_factor_gross_state": str(conservative.profit_factor_gross_state),
            "profit_factor_net": profit_factor_json(
                conservative.profit_factor_net, conservative.profit_factor_net_state),
            "profit_factor_net_state": str(conservative.profit_factor_net_state),
            "break_even_profit_factor": round(conservative.break_even_profit_factor, 6),
            "clears_break_even": conservative.clears_break_even,
            # Written so `falsify.orchestrator.sharpe_variance_from_registry` finds the
            # field it has always looked for. Same definition as Context.trade_returns().
            "sharpe_per_trade": falsification_input["returns"]["sharpe_per_trade"],
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
        "falsification_input": falsification_input,
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
