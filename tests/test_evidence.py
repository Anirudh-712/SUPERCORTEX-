"""Falsification evidence, slice A: what a sealed trial must record about itself.

The audit found that a sealed `metrics-trial-NNNN.json` cannot reconstruct a falsification
`Context`. It carries aggregates — `n_trades`, `win_rate`, three profit factors — and not
one individual round trip, no prices, and no timestamps. Nine of eleven Context fields were
unavailable from sealed evidence, so the battery could only ever run on objects a test
handed it.

This module fixes that at the source: the run writes down what it did, while it still has
the objects in hand. Nothing here reconstructs, infers, or defaults — a value that cannot
be read from a runtime object or the open registry row is a failure, not a guess.

The series digest is the load-bearing part. It covers the bars **after** shift/scramble —
the exact objects handed to the engine — so a probe run and its baseline are provably
different inputs, and a dataset that changed underneath a trial cannot masquerade as the
one that produced it.
"""
from __future__ import annotations

import json
import math
import struct
from hashlib import sha256

import pytest

from supercortex.backtest import evidence
from supercortex.costs.model import CostModel, TaxRegime, Trade

NS = 1_000_000


# ══════════════════════════════════════════════════════════════════════════════
# the canonical series encoding
# ══════════════════════════════════════════════════════════════════════════════
def test_encoding_is_declared_and_versioned():
    """The digest is worthless unless the encoding that produced it is named."""
    assert evidence.SERIES_ENCODING == "scx-series-1"
    assert evidence.SCHEMA_VERSION == 1


def test_series_digest_is_big_endian_struct_over_ts_and_close():
    """Explicitly big-endian, so the digest does not depend on the host's byte order."""
    pairs = [(1_600_000_000_000 * NS, 100.5), (1_600_003_600_000 * NS, 101.25)]
    expected = sha256(
        evidence.SERIES_ENCODING.encode("utf-8") + b"\n"
        + b"".join(struct.pack(">qd", ts, close) for ts, close in pairs)
    ).hexdigest()
    assert evidence.series_digest(pairs) == expected


def test_series_digest_is_stable_across_calls():
    pairs = [(1, 1.0), (2, 2.0)]
    assert evidence.series_digest(pairs) == evidence.series_digest(list(pairs))


def test_series_digest_is_order_sensitive():
    """Bars are delivered in order. Two different orderings are two different inputs."""
    a = [(1, 1.0), (2, 2.0)]
    assert evidence.series_digest(a) != evidence.series_digest(list(reversed(a)))


def test_series_digest_distinguishes_a_shifted_series():
    """The probe case: same closes, stamps moved. Must not collide with the baseline."""
    base = [(1_000 * NS, 100.0), (2_000 * NS, 101.0)]
    shifted = [(ts - 1_000 * NS, close) for ts, close in base]
    assert evidence.series_digest(base) != evidence.series_digest(shifted)


def test_series_digest_distinguishes_a_scrambled_series():
    """Same stamps, closes permuted. Scramble must be visible in the evidence."""
    base = [(1, 100.0), (2, 101.0), (3, 102.0)]
    scrambled = [(1, 102.0), (2, 100.0), (3, 101.0)]
    assert evidence.series_digest(base) != evidence.series_digest(scrambled)


def test_series_digest_separates_values_that_share_a_decimal_prefix():
    """A text encoding rounded to 2dp would collide these. IEEE-754 does not."""
    a = [(1, 100.123456789)]
    b = [(1, 100.123456788)]
    assert evidence.series_digest(a) != evidence.series_digest(b)


def test_series_digest_of_an_empty_series_is_refused():
    """An empty engine input is not a series; it is a run that had no data."""
    with pytest.raises(evidence.EvidenceError, match="empty"):
        evidence.series_digest([])


@pytest.mark.parametrize("bad", [
    [(1.5, 100.0)],                       # non-integer stamp
    [(1, "100.0")],                       # string close
    [(None, 100.0)],
])
def test_series_digest_refuses_values_it_cannot_encode_canonically(bad):
    with pytest.raises(evidence.EvidenceError):
        evidence.series_digest(bad)


def test_series_digest_refuses_a_non_finite_close():
    """NaN never equals itself, so a NaN in the series makes the digest meaningless."""
    with pytest.raises(evidence.EvidenceError, match="finite"):
        evidence.series_digest([(1, float("nan"))])


# ══════════════════════════════════════════════════════════════════════════════
# per-trade returns and the Sharpe the registry looks for
# ══════════════════════════════════════════════════════════════════════════════
def _trade(entry_ms: int, exit_ms: int, entry: float, exit_: float) -> Trade:
    return Trade(entry_ms, exit_ms, 1, 1.0, entry, exit_)


def test_per_trade_returns_are_ordered_by_exit_and_scaled_by_equity():
    """Must match falsify Context.trade_returns() exactly or the DSR compares apples to
    oranges across trials."""
    model = CostModel()
    trades = [_trade(20, 30, 100.0, 101.0), _trade(0, 10, 100.0, 102.0)]
    got = evidence.per_trade_returns(trades, model, 1_000.0)
    expected = [model.net_of_costs(t) / 1_000.0
                for t in sorted(trades, key=lambda t: t.exit_ts_ms)]
    assert got == expected


def test_per_trade_returns_of_no_trades_is_empty():
    assert evidence.per_trade_returns([], CostModel(), 1_000.0) == []


def test_zero_starting_equity_is_refused_not_divided_by():
    with pytest.raises(evidence.EvidenceError, match="starting equity"):
        evidence.per_trade_returns([_trade(0, 10, 100.0, 101.0)], CostModel(), 0.0)


def test_sharpe_per_trade_matches_the_falsification_definition():
    from supercortex.falsify import statistics as st

    model = CostModel()
    trades = [_trade(i * 10, i * 10 + 5, 100.0, 100.0 + (i % 3)) for i in range(12)]
    returns = evidence.per_trade_returns(trades, model, 100_000.0)
    assert evidence.sharpe_per_trade(returns) == st.sharpe_ratio(
        __import__("numpy").array(returns)
    )


def test_sharpe_per_trade_of_an_empty_series_is_zero_not_nan():
    assert evidence.sharpe_per_trade([]) == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# the price-series evidence block
# ══════════════════════════════════════════════════════════════════════════════
def _bars(n: int = 3) -> list[tuple[int, float]]:
    return [(1_600_000_000_000 * NS + i * 3_600_000 * NS, 100.0 + i) for i in range(n)]


def test_registered_dataset_block_names_its_provenance():
    block = evidence.price_series_evidence(
        facts=evidence.series_facts(_bars()),
        source=evidence.SOURCE_REGISTERED,
        dataset_ids=["DATA-000002"],
        data_snapshot="abc123",
        symbol="BTCUSDT", market="um", interval="1h",
        start_ms=None, end_ms=None,
        probe={"label": "baseline", "bars_shift": 0, "bars_scramble": False,
               "funding_shift": 0, "funding_scramble": False},
    )
    assert block["source"] == "registered_dataset"
    assert block["dataset_ids"] == ["DATA-000002"]
    assert block["data_snapshot"] == "abc123"
    assert block["n_points"] == 3
    assert block["first_ts_event_ns"] == _bars()[0][0]
    assert block["last_ts_event_ns"] == _bars()[-1][0]
    assert block["encoding"] == evidence.SERIES_ENCODING
    assert block["series_sha256"] == evidence.series_digest(_bars())
    assert block["transform"]["interval"] == "1h"
    assert block["transform"]["probe"]["label"] == "baseline"


def test_registered_dataset_without_dataset_ids_is_refused():
    """`no-datasets` is a legal snapshot for a synthetic run, never for a market run."""
    with pytest.raises(evidence.EvidenceError, match="dataset"):
        evidence.price_series_evidence(
            facts=evidence.series_facts(_bars()), source=evidence.SOURCE_REGISTERED,
            dataset_ids=[], data_snapshot="abc",
            symbol="BTCUSDT", market="um", interval="1h",
            start_ms=None, end_ms=None, probe={},
        )


def test_synthetic_fixture_must_declare_its_generator_and_every_parameter():
    block = evidence.price_series_evidence(
        facts=evidence.series_facts(_bars()), source=evidence.SOURCE_SYNTHETIC,
        generator="supercortex.backtest.golden:sawtooth",
        generator_parameters={"n_bars": 240, "low": 100.0, "high": 140.0, "period": 40,
                              "epoch_start_ms": 1_600_000_000_000, "bar_ms": 3_600_000},
        interval="1h", start_ms=None, end_ms=None, probe={},
    )
    assert block["source"] == "synthetic_fixture"
    assert block["generator"] == "supercortex.backtest.golden:sawtooth"
    assert block["generator_parameters"]["period"] == 40
    assert block["generator_parameters"]["epoch_start_ms"] == 1_600_000_000_000


def test_synthetic_fixture_without_a_generator_reference_is_refused():
    """No guessing which generator produced the frame."""
    with pytest.raises(evidence.EvidenceError, match="generator"):
        evidence.price_series_evidence(
            facts=evidence.series_facts(_bars()), source=evidence.SOURCE_SYNTHETIC,
            generator=None, generator_parameters={"n_bars": 240},
            interval="1h", start_ms=None, end_ms=None, probe={},
        )


def test_synthetic_fixture_without_the_epoch_convention_is_refused():
    """`period` and the epoch convention are what make the series recomputable."""
    with pytest.raises(evidence.EvidenceError, match="epoch_start_ms"):
        evidence.price_series_evidence(
            facts=evidence.series_facts(_bars()), source=evidence.SOURCE_SYNTHETIC,
            generator="supercortex.backtest.golden:sawtooth",
            generator_parameters={"n_bars": 240, "low": 100.0, "high": 140.0,
                                  "period": 40, "bar_ms": 3_600_000},
            interval="1h", start_ms=None, end_ms=None, probe={},
        )


def test_an_unknown_source_kind_is_refused():
    with pytest.raises(evidence.EvidenceError, match="source"):
        evidence.price_series_evidence(
            facts=evidence.series_facts(_bars()), source="whatever_was_lying_around",
            interval="1h", start_ms=None, end_ms=None, probe={},
        )


# ══════════════════════════════════════════════════════════════════════════════
# the assembled block
# ══════════════════════════════════════════════════════════════════════════════
IDENTITY = {
    "experiment_id": "EXP-000009", "trial_seq": 3,
    "config_hash": "c0ffee", "config_path": "02-experiments/EXP-000009-x/config.yaml",
    "code_commit": "deadbeef", "data_snapshot": "snap", "dataset_ids": ["DATA-000002"],
    "seed": 42,
}
RUN_CONFIG = {"starting_balance": 100_000.0, "leverage": 1.0, "seed": 42,
              "latency_ms": 250, "interval": "1h", "symbol": "BTCUSDT", "market": "um"}


def _block(**over):
    model = over.pop("model", CostModel())
    trades = over.pop("trades", [_trade(0, 10, 100.0, 101.0), _trade(20, 30, 100.0, 99.0)])
    kwargs = dict(
        identity=IDENTITY, trades=trades, model=model, run_config=RUN_CONFIG,
        price_series=evidence.price_series_evidence(
            facts=evidence.series_facts(_bars()), source=evidence.SOURCE_REGISTERED,
            dataset_ids=["DATA-000002"], data_snapshot="snap",
            symbol="BTCUSDT", market="um", interval="1h",
            start_ms=None, end_ms=None, probe={"label": "baseline"},
        ),
        reconciliation={"engine_realized_pnl": 1.0, "our_gross_pnl": 1.0,
                        "reconciliation_diff": 0.0},
    )
    kwargs.update(over)
    return evidence.build(**kwargs)


def test_block_is_versioned():
    assert _block()["schema_version"] == evidence.SCHEMA_VERSION


def test_block_carries_identity_from_the_registry_row():
    identity = _block()["identity"]
    for key, value in IDENTITY.items():
        assert identity[key] == value, key


def test_block_preserves_every_trade_field_exactly():
    trades = [Trade(1, 2, -1, 0.5, 100.0, 99.0, entry_taker=False, exit_taker=True,
                    funding_pnl=-0.25)]
    recorded = _block(trades=trades)["trades"]
    assert len(recorded) == 1
    assert recorded[0] == {
        "entry_ts_ms": 1, "exit_ts_ms": 2, "direction": -1, "qty": 0.5,
        "entry_price": 100.0, "exit_price": 99.0,
        "entry_taker": False, "exit_taker": True, "funding_pnl": -0.25,
    }


def test_recorded_trades_round_trip_back_into_Trade():
    """The point of preserving them: they must reconstruct without interpretation."""
    original = [_trade(0, 10, 100.0, 101.0), _trade(20, 30, 100.0, 99.0)]
    rebuilt = [Trade(**row) for row in _block(trades=original)["trades"]]
    assert rebuilt == original


def test_block_records_every_cost_model_field():
    import dataclasses

    recorded = _block()["cost_model"]
    for field in dataclasses.fields(CostModel):
        assert field.name in recorded, field.name
    assert recorded["regime"] == TaxRegime.VDA_STRICT.value
    assert recorded["version"] == CostModel().version
    assert recorded["fees_deductible"] is False
    assert isinstance(recorded["notes"], dict)


def test_a_non_default_cost_model_is_recorded_as_configured():
    """A version name is not a configuration. The effective fields are what count."""
    model = CostModel(taker_fee_bps=9.0, fees_deductible=True,
                      regime=TaxRegime.BUSINESS_INCOME)
    recorded = _block(model=model)["cost_model"]
    assert recorded["taker_fee_bps"] == 9.0
    assert recorded["fees_deductible"] is True
    assert recorded["regime"] == "business_income"


def test_block_carries_run_config_and_returns():
    block = _block()
    assert block["run_config"] == RUN_CONFIG
    assert len(block["returns"]["per_trade"]) == 2
    assert block["returns"]["sharpe_per_trade"] == evidence.sharpe_per_trade(
        block["returns"]["per_trade"]
    )


def test_block_carries_reconciliation_and_price_series():
    block = _block()
    assert block["reconciliation"]["reconciliation_diff"] == 0.0
    assert block["price_series"]["series_sha256"] == evidence.series_digest(_bars())


def test_block_is_json_serialisable_and_stable():
    block = _block()
    once = json.dumps(block, sort_keys=True)
    assert json.loads(once) == block


@pytest.mark.parametrize("missing", [
    "experiment_id", "trial_seq", "config_hash", "config_path",
    "code_commit", "data_snapshot", "seed",
])
def test_identity_missing_any_required_field_is_refused(missing):
    identity = {k: v for k, v in IDENTITY.items() if k != missing}
    with pytest.raises(evidence.EvidenceError, match=missing):
        _block(identity=identity)


def test_a_non_finite_value_anywhere_is_refused():
    """JSON will happily emit NaN; nothing downstream can compare it."""
    with pytest.raises(evidence.EvidenceError, match="finite"):
        _block(reconciliation={"engine_realized_pnl": float("inf"),
                               "our_gross_pnl": 1.0, "reconciliation_diff": 0.0})


def test_evidence_error_is_a_runtime_error():
    assert issubclass(evidence.EvidenceError, RuntimeError)
    assert not math.isnan(0.0)


# ══════════════════════════════════════════════════════════════════════════════
# the sealed trial: evidence reaches the artifact, or the trial is not `ok`
# ══════════════════════════════════════════════════════════════════════════════
from pathlib import Path  # noqa: E402

from supercortex.backtest import trial as trial_mod  # noqa: E402
from supercortex.backtest.runner import BacktestSpec, ProbeSpec  # noqa: E402
from supercortex.costs.model import compare_regimes  # noqa: E402
from supercortex.registry import db, experiments  # noqa: E402

SERIES = {"source": evidence.SOURCE_REGISTERED, "encoding": evidence.SERIES_ENCODING,
          "series_sha256": "d" * 64, "n_points": 3,
          "first_ts_event_ns": 1, "last_ts_event_ns": 3}
DIAGNOSTICS = {"series": SERIES, "bars": 3, "engine_realized_pnl": 1.0,
               "our_gross_pnl": 1.0, "reconciliation_diff": 0.0}


def _spec(**over) -> BacktestSpec:
    base = dict(strategy="supercortex.backtest.golden:GoldenStrategy",
                symbol="BTCUSDT", market="um", interval="1h",
                starting_balance=100_000.0, probe=ProbeSpec())
    base.update(over)
    return BacktestSpec(**base)


def test_falsification_input_is_built_from_runtime_objects():
    trades = [_trade(0, 10, 100.0, 101.0)]
    block = trial_mod._falsification_input(
        _spec(), CostModel(), trades, IDENTITY | {"dataset_ids": ["DATA-000002"]},
        DIAGNOSTICS,
    )
    assert block["schema_version"] == evidence.SCHEMA_VERSION
    assert block["price_series"]["series_sha256"] == "d" * 64
    assert block["price_series"]["source"] == "registered_dataset"
    assert block["price_series"]["transform"]["probe"]["label"] == "baseline"
    assert block["reconciliation"]["reconciliation_diff"] == 0.0


def test_a_run_that_reported_no_series_cannot_produce_evidence():
    """The bars handed to the engine are the only thing that can honestly be hashed."""
    with pytest.raises(evidence.EvidenceError, match="series"):
        trial_mod._falsification_input(
            _spec(), CostModel(), [], IDENTITY | {"dataset_ids": []},
            {"bars": 0},
        )


def test_a_synthetic_run_without_a_declared_generator_cannot_produce_evidence():
    synthetic = dict(SERIES, source=evidence.SOURCE_SYNTHETIC)
    with pytest.raises(evidence.EvidenceError, match="generator"):
        trial_mod._falsification_input(
            _spec(synthetic_bars=object()), CostModel(),
            [_trade(0, 10, 100.0, 101.0)], IDENTITY | {"dataset_ids": []},
            dict(DIAGNOSTICS, series=synthetic),
        )


def test_a_generator_reference_that_is_not_a_string_is_refused():
    """Not a nested dict somebody meant to unpack. (Was shadowed by a same-named test
    further down the file, so it never ran until ruff's F811 caught the collision.)"""
    synthetic = dict(SERIES, source=evidence.SOURCE_SYNTHETIC)
    with pytest.raises(evidence.EvidenceError, match="module:function"):
        trial_mod._falsification_input(
            _spec(synthetic_bars=object(), synthetic_source={"generator": {"name": "x"}}),
            CostModel(), [_trade(0, 10, 100.0, 101.0)],
            IDENTITY | {"dataset_ids": []}, dict(DIAGNOSTICS, series=synthetic),
        )


def test_metrics_carry_the_block_and_the_summary_sharpe(tmp_registry):
    trades = [_trade(0, 10, 100.0, 101.0), _trade(20, 30, 100.0, 99.5)]
    model = CostModel()
    results = compare_regimes(trades, model)
    metrics = trial_mod._metrics(
        "EXP-000009", 3, _spec(), model, results[TaxRegime.VDA_STRICT], results,
        DIAGNOSTICS, IDENTITY | {"dataset_ids": ["DATA-000002"]}, trades,
    )
    assert metrics["falsification_input"]["schema_version"] == evidence.SCHEMA_VERSION
    assert len(metrics["falsification_input"]["trades"]) == 2
    assert metrics["trades"]["sharpe_per_trade"] == (
        metrics["falsification_input"]["returns"]["sharpe_per_trade"]
    )
    # the raw series must not leak into the engine diagnostics block
    assert "series" not in metrics["engine"]
    json.dumps(metrics)


# ── identity comes from the registry row, not the caller ────────────────────────
@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.sqlite"
    db.init(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(experiments, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(experiments, "EXPERIMENTS_DIR", tmp_path / "02-experiments")
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
            " VALUES ('EXP-000009','golden','golden','BACKTESTING',10,?)", (db.now(),))
        conn.execute(
            "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
            "cost_model_version,seed,started_at) VALUES"
            " ('EXP-000009',1,'CFG-HASH','COMMIT-SHA','SNAP','india-vda-1.0.0',7,?)",
            (db.now(),))
        yield tmp_path


def test_identity_is_copied_from_the_open_registry_row(tmp_registry):
    config = tmp_registry / "02-experiments" / "EXP-000009-golden" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("experiment_id: EXP-000009\n", encoding="utf-8")

    identity = trial_mod.trial_identity("EXP-000009", 1, config, ["DATA-000002"])
    assert identity["config_hash"] == "CFG-HASH"
    assert identity["code_commit"] == "COMMIT-SHA"
    assert identity["data_snapshot"] == "SNAP"
    assert identity["seed"] == 7
    assert identity["config_path"] == "02-experiments/EXP-000009-golden/config.yaml"
    assert identity["dataset_ids"] == ["DATA-000002"]


def test_identity_for_an_unregistered_trial_is_refused(tmp_registry):
    with pytest.raises(evidence.EvidenceError, match="registered"):
        trial_mod.trial_identity("EXP-000009", 99, Path("x/config.yaml"), [])


# ── the contract that matters: never an `ok` without evidence ───────────────────
def test_a_trial_whose_evidence_fails_seals_as_error_never_ok(tmp_registry, monkeypatch):
    """The whole point. An unfalsifiable run still consumed a look at the data, so it
    counts — but it must never be presented as a result."""
    exp_dir = tmp_registry / "02-experiments" / "EXP-000009-golden"
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "config.yaml").write_text(
        "experiment_id: EXP-000009\nstrategy: supercortex.backtest.golden:GoldenStrategy\n"
        "starting_balance: 100000.0\nseed: 42\n", encoding="utf-8")

    def fake_open_trial(experiment_id, **kw):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,"
                "data_snapshot,cost_model_version,seed,started_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (experiment_id, 2, "CFG", "SHA", kw["data_snapshot"],
                 kw["cost_model_version"], kw["seed"], db.now()))
        return 2

    # a run that returns trades but no series evidence
    monkeypatch.setattr(experiments, "open_trial", fake_open_trial)
    monkeypatch.setattr(trial_mod, "run_backtest",
                        lambda spec: ([_trade(0, 10, 100.0, 101.0)], {"bars": 1}))

    with pytest.raises(evidence.EvidenceError):
        trial_mod.execute("EXP-000009", allow_dirty=True)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT outcome, metrics_path FROM trial WHERE experiment_id='EXP-000009'"
            " AND seq=2").fetchone()
    assert row["outcome"] == "error", "an unfalsifiable trial must not seal as ok"
    assert row["metrics_path"] is None, "no artifact may claim to be its result"


def test_a_failed_trial_increments_the_budget_by_exactly_one(tmp_registry, monkeypatch):
    """Sealed `error`, still a look at the data — and counted once, not zero and not twice.
    The previous version asserted `>= 1`, which the fixture alone already satisfied."""
    before = experiments.trial_count("EXP-000009")
    _arm_failing_run(tmp_registry, monkeypatch, seq=5)

    with pytest.raises(evidence.EvidenceError):
        trial_mod.execute("EXP-000009", allow_dirty=True)

    assert experiments.trial_count("EXP-000009") == before + 1
    row = _trial_row("EXP-000009", 5)
    assert row["outcome"] == "error"
    assert row["metrics_path"] is None


# ══════════════════════════════════════════════════════════════════════════════
# helpers for the sealing-path tests
# ══════════════════════════════════════════════════════════════════════════════
def _chain(error: BaseException) -> list[str]:
    """Every exception reachable from `error`, in order. Cycle-safe."""
    seen: set[int] = set()
    out: list[str] = []
    node: BaseException | None = error
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        out.append(str(node))
        node = node.__cause__ or node.__context__
    return out


def _trial_row(experiment_id: str, seq: int):
    with db.connect() as conn:
        return conn.execute(
            "SELECT outcome, metrics_path FROM trial WHERE experiment_id = ? AND seq = ?",
            (experiment_id, seq),
        ).fetchone()


def _write_config(root: Path) -> Path:
    exp_dir = root / "02-experiments" / "EXP-000009-golden"
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "config.yaml").write_text(
        "experiment_id: EXP-000009\nstrategy: supercortex.backtest.golden:GoldenStrategy\n"
        "starting_balance: 100000.0\nseed: 42\n"
        "data:\n  symbol: BTCUSDT\n  market: um\n  interval: 1h\n"
        "  datasets: [DATA-000002]\n", encoding="utf-8")
    return exp_dir


def _fake_open_trial(seq: int):
    def opener(experiment_id, **kw):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,"
                "data_snapshot,cost_model_version,seed,started_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (experiment_id, seq, "CFG", "SHA", kw["data_snapshot"],
                 kw["cost_model_version"], kw["seed"], db.now()))
        return seq
    return opener


def _arm_failing_run(root: Path, monkeypatch, *, seq: int) -> Path:
    """A registered run whose evidence cannot be built (the runner reports no series)."""
    exp_dir = _write_config(root)
    monkeypatch.setattr(trial_mod, "dataset_snapshot", lambda ids: "SNAP")
    monkeypatch.setattr(experiments, "open_trial", _fake_open_trial(seq))
    monkeypatch.setattr(trial_mod, "run_backtest",
                        lambda spec: ([_trade(0, 10, 100.0, 101.0)], {"bars": 1}))
    return exp_dir


def _arm_good_run(root: Path, monkeypatch, *, seq: int) -> Path:
    exp_dir = _write_config(root)
    monkeypatch.setattr(trial_mod, "dataset_snapshot", lambda ids: "SNAP")
    monkeypatch.setattr(experiments, "open_trial", _fake_open_trial(seq))
    # one win and one loss: an all-winning trial has an infinite profit factor, which
    # strict serialization now refuses. See test_an_all_winning_trial_cannot_be_sealed.
    monkeypatch.setattr(
        trial_mod, "run_backtest",
        lambda spec: ([_trade(0, 10, 100.0, 101.0), _trade(20, 30, 100.0, 99.0)],
                      dict(DIAGNOSTICS)))
    return exp_dir


# ══════════════════════════════════════════════════════════════════════════════
# 1. strict serialization, inside the guarded region
# ══════════════════════════════════════════════════════════════════════════════
def test_serialization_settings_are_shared_with_the_artifact_writer():
    """One definition, so a pre-flight check cannot pass what the writer would reject."""
    document = {"b": 1, "a": {"z": 2.5}}
    assert trial_mod.serialise_metrics(document) == experiments.dump_metrics(document)
    assert experiments.dump_metrics(document) == (
        '{\n  "a": {\n    "z": 2.5\n  },\n  "b": 1\n}'
    )
    with pytest.raises(ValueError):
        experiments.dump_metrics({"x": float("nan")})


def test_a_non_finite_value_outside_the_evidence_block_is_still_rejected():
    """`_assert_finite` only walks falsification_input. JSON would emit a bare NaN here,
    producing an artifact no conforming parser can read."""
    with pytest.raises(evidence.EvidenceError, match="serial"):
        trial_mod.serialise_metrics({"trades": {"win_rate": float("nan")}})


def test_a_metrics_document_that_cannot_be_serialised_seals_error(tmp_registry,
                                                                  monkeypatch):
    _arm_good_run(tmp_registry, monkeypatch, seq=6)
    monkeypatch.setattr(trial_mod, "_metrics",
                        lambda *a, **k: {"corrupt": float("nan")})

    with pytest.raises(evidence.EvidenceError):
        trial_mod.execute("EXP-000009", allow_dirty=True)

    row = _trial_row("EXP-000009", 6)
    assert row["outcome"] == "error"
    assert row["metrics_path"] is None


# ══════════════════════════════════════════════════════════════════════════════
# 2. a failure while sealing must not leave the trial open or claiming `ok`
# ══════════════════════════════════════════════════════════════════════════════
def test_a_failure_while_sealing_does_not_leave_the_trial_open(tmp_registry, monkeypatch):
    exp_dir = _arm_good_run(tmp_registry, monkeypatch, seq=7)
    real_close = experiments.close_trial

    def flaky(experiment_id, seq, outcome, metrics=None, *, is_headline=True):
        if outcome == "ok":
            # mimic the real order: artifacts land, then the database update fails
            artifacts = exp_dir / "artifacts"
            artifacts.mkdir(parents=True, exist_ok=True)
            payload = experiments.dump_metrics(metrics)
            (artifacts / f"metrics-trial-{seq:04d}.json").write_text(payload)
            if is_headline:
                (artifacts / "metrics.json").write_text(payload)
            raise OSError("registry unavailable while sealing")
        return real_close(experiment_id, seq, outcome, metrics, is_headline=is_headline)

    monkeypatch.setattr(experiments, "close_trial", flaky)

    with pytest.raises(OSError, match="sealing"):
        trial_mod.execute("EXP-000009", allow_dirty=True)

    row = _trial_row("EXP-000009", 7)
    assert row["outcome"] == "error", "a trial that failed to seal must not stay open"
    assert row["metrics_path"] is None


def test_a_seal_failure_leaves_no_headline_artifact(tmp_registry, monkeypatch):
    """A headline is the DESIGNATED result. A run that sealed `error` has none."""
    exp_dir = _arm_good_run(tmp_registry, monkeypatch, seq=8)
    real_close = experiments.close_trial

    def flaky(experiment_id, seq, outcome, metrics=None, *, is_headline=True):
        if outcome == "ok":
            artifacts = exp_dir / "artifacts"
            artifacts.mkdir(parents=True, exist_ok=True)
            payload = experiments.dump_metrics(metrics)
            (artifacts / f"metrics-trial-{seq:04d}.json").write_text(payload)
            (artifacts / "metrics.json").write_text(payload)
            raise OSError("registry unavailable while sealing")
        return real_close(experiment_id, seq, outcome, metrics, is_headline=is_headline)

    monkeypatch.setattr(experiments, "close_trial", flaky)
    with pytest.raises(OSError):
        trial_mod.execute("EXP-000009", allow_dirty=True)

    assert not (exp_dir / "artifacts" / "metrics.json").exists()
    assert not (exp_dir / "artifacts" / "metrics-trial-0008.json").exists()


def test_a_seal_failure_restores_a_previous_headline(tmp_registry, monkeypatch):
    """Rollback, not scorched earth: an earlier trial's designated result survives."""
    exp_dir = _arm_good_run(tmp_registry, monkeypatch, seq=9)
    artifacts = exp_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "metrics.json").write_text('{"earlier": true}', encoding="utf-8")
    real_close = experiments.close_trial

    def flaky(experiment_id, seq, outcome, metrics=None, *, is_headline=True):
        if outcome == "ok":
            payload = experiments.dump_metrics(metrics)
            (artifacts / f"metrics-trial-{seq:04d}.json").write_text(payload)
            (artifacts / "metrics.json").write_text(payload)
            raise OSError("registry unavailable while sealing")
        return real_close(experiment_id, seq, outcome, metrics, is_headline=is_headline)

    monkeypatch.setattr(experiments, "close_trial", flaky)
    with pytest.raises(OSError):
        trial_mod.execute("EXP-000009", allow_dirty=True)

    assert (artifacts / "metrics.json").read_text() == '{"earlier": true}'


# ══════════════════════════════════════════════════════════════════════════════
# 4. the generator reference is validated, and restricted to the approved fixture
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("reference", [
    "not-a-reference", "supercortex.backtest.golden", ":sawtooth",
    "supercortex.backtest.golden:", "a:b:c", "",
])
def test_a_malformed_generator_reference_is_refused(reference):
    with pytest.raises(evidence.EvidenceError):
        evidence.price_series_evidence(
            facts=evidence.series_facts(_bars()), source=evidence.SOURCE_SYNTHETIC,
            generator=reference,
            generator_parameters={"n_bars": 240, "low": 100.0, "high": 140.0,
                                  "period": 40, "epoch_start_ms": 1, "bar_ms": 1},
            interval="1h", start_ms=None, end_ms=None, probe={})


def test_only_the_approved_golden_fixture_may_generate_synthetic_evidence():
    """Well-formed but unapproved. Authorising a generator is a CIO decision."""
    with pytest.raises(evidence.EvidenceError, match="CIO"):
        evidence.price_series_evidence(
            facts=evidence.series_facts(_bars()), source=evidence.SOURCE_SYNTHETIC,
            generator="somewhere.else:make_prices",
            generator_parameters={"n_bars": 240, "low": 100.0, "high": 140.0,
                                  "period": 40, "epoch_start_ms": 1, "bar_ms": 1},
            interval="1h", start_ms=None, end_ms=None, probe={})


def test_the_approved_generator_is_exactly_the_golden_fixture():
    assert evidence.APPROVED_GENERATORS == ("supercortex.backtest.golden:sawtooth",)


# ══════════════════════════════════════════════════════════════════════════════
# 5. where each identity field actually comes from
# ══════════════════════════════════════════════════════════════════════════════
def test_dataset_ids_come_from_the_caller_not_the_registry(tmp_registry):
    """The row stores a `data_snapshot` hash, not the id list. Claiming otherwise in the
    docstring would misdescribe what a later reader is verifying."""
    config = tmp_registry / "02-experiments" / "EXP-000009-golden" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("experiment_id: EXP-000009\n", encoding="utf-8")

    first = trial_mod.trial_identity("EXP-000009", 1, config, ["DATA-000002"])
    second = trial_mod.trial_identity("EXP-000009", 1, config, ["DATA-000003"])
    assert first["dataset_ids"] == ["DATA-000002"]
    assert second["dataset_ids"] == ["DATA-000003"]
    assert first["data_snapshot"] == second["data_snapshot"] == "SNAP"


def test_config_path_is_derived_from_the_argument_not_the_row(tmp_registry):
    elsewhere = tmp_registry / "02-experiments" / "EXP-000009-golden" / "other.yaml"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    elsewhere.write_text("x: 1\n", encoding="utf-8")
    identity = trial_mod.trial_identity("EXP-000009", 1, elsewhere, [])
    assert identity["config_path"] == "02-experiments/EXP-000009-golden/other.yaml"


def test_the_row_sourced_fields_are_exactly_the_six_reproducibility_inputs():
    assert trial_mod.ROW_SOURCED == (
        "experiment_id", "trial_seq", "config_hash", "code_commit", "data_snapshot", "seed",
    )


def test_an_all_winning_trial_seals_ok_with_evidence(tmp_registry, monkeypatch):
    """A rare and suspicious result is still a result. It keeps its artifact so the
    battery can attack it; it does not become a technical error."""
    exp_dir = _write_config(tmp_registry)
    monkeypatch.setattr(trial_mod, "dataset_snapshot", lambda ids: "SNAP")
    monkeypatch.setattr(experiments, "open_trial", _fake_open_trial(11))
    monkeypatch.setattr(trial_mod, "run_backtest",
                        lambda spec: ([_trade(0, 10, 100.0, 101.0)], dict(DIAGNOSTICS)))

    metrics = trial_mod.execute("EXP-000009", allow_dirty=True)

    row = _trial_row("EXP-000009", 11)
    assert row["outcome"] == "ok"
    assert row["metrics_path"] is not None
    assert metrics["trades"]["profit_factor_gross"] is None
    assert metrics["trades"]["profit_factor_gross_state"] == "unbounded"
    assert metrics["trades"]["clears_break_even"] is True
    assert metrics["falsification_input"]["schema_version"] == evidence.SCHEMA_VERSION
    assert len(metrics["falsification_input"]["trades"]) == 1
    # and the artifact is readable by a strict parser
    landed = json.loads((exp_dir / "artifacts" / "metrics-trial-0011.json").read_text())
    assert landed["trades"]["profit_factor_gross_state"] == "unbounded"


def test_a_zero_trade_trial_seals_ok_as_a_valid_negative_result(tmp_registry,
                                                                monkeypatch):
    """The strategy declined to trade. That is information, not a failure to run."""
    exp_dir = _write_config(tmp_registry)
    monkeypatch.setattr(trial_mod, "dataset_snapshot", lambda ids: "SNAP")
    monkeypatch.setattr(experiments, "open_trial", _fake_open_trial(12))
    monkeypatch.setattr(trial_mod, "run_backtest",
                        lambda spec: ([], dict(DIAGNOSTICS)))

    metrics = trial_mod.execute("EXP-000009", allow_dirty=True)

    assert _trial_row("EXP-000009", 12)["outcome"] == "ok"
    assert metrics["trades"]["n_trades"] == 0
    assert metrics["trades"]["profit_factor_gross"] is None
    assert metrics["trades"]["profit_factor_gross_state"] == "undefined"
    assert metrics["trades"]["clears_break_even"] is False
    assert metrics["falsification_input"]["trades"] == []
    assert metrics["falsification_input"]["returns"]["per_trade"] == []
    json.loads((exp_dir / "artifacts" / "metrics-trial-0012.json").read_text())


def test_a_finite_trial_still_seals_the_number_it_always_did(tmp_registry, monkeypatch):
    _write_config(tmp_registry)
    monkeypatch.setattr(trial_mod, "dataset_snapshot", lambda ids: "SNAP")
    monkeypatch.setattr(experiments, "open_trial", _fake_open_trial(13))
    monkeypatch.setattr(
        trial_mod, "run_backtest",
        lambda spec: ([_trade(0, 10, 100.0, 101.0), _trade(20, 30, 100.0, 99.0)],
                      dict(DIAGNOSTICS)))

    metrics = trial_mod.execute("EXP-000009", allow_dirty=True)
    assert _trial_row("EXP-000009", 13)["outcome"] == "ok"
    assert isinstance(metrics["trades"]["profit_factor_gross"], float)
    assert metrics["trades"]["profit_factor_gross_state"] == "finite"


# ══════════════════════════════════════════════════════════════════════════════
# 2. the recovery path must itself be able to fail
# ══════════════════════════════════════════════════════════════════════════════
# This is exception recovery, not crash-atomicity. Nothing here survives a kill -9 between
# the artifact write and the row update: SQLite and the filesystem are two stores and this
# code does not span them transactionally. What it does guarantee is that a raised
# exception cannot leave the row open, and that a filesystem cleanup failure cannot
# prevent the database seal from being attempted while the registry is reachable.
def _flaky_seal(exp_dir, real_close):
    def flaky(experiment_id, seq, outcome, metrics=None, *, is_headline=True):
        if outcome == "ok":
            artifacts = exp_dir / "artifacts"
            artifacts.mkdir(parents=True, exist_ok=True)
            payload = experiments.dump_metrics(metrics)
            (artifacts / f"metrics-trial-{seq:04d}.json").write_text(payload)
            if is_headline:
                (artifacts / "metrics.json").write_text(payload)
            raise OSError("registry unavailable while sealing")
        return real_close(experiment_id, seq, outcome, metrics, is_headline=is_headline)
    return flaky


def test_failure_while_reading_the_previous_headline_still_seals_error(tmp_registry,
                                                                       monkeypatch):
    """The snapshot happens before the try in the naive version, so a headline that
    cannot be read leaves the row open forever."""
    exp_dir = _arm_good_run(tmp_registry, monkeypatch, seq=20)
    artifacts = exp_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "metrics.json").write_text('{"earlier": true}', encoding="utf-8")

    original_read = Path.read_bytes

    def unreadable(self, *a, **kw):
        if self.name == "metrics.json":
            raise OSError("headline is unreadable")
        return original_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_bytes", unreadable)

    with pytest.raises(OSError, match="unreadable"):
        trial_mod.execute("EXP-000009", allow_dirty=True)

    row = _trial_row("EXP-000009", 20)
    assert row["outcome"] == "error"
    assert row["metrics_path"] is None


def test_failure_while_cleaning_artifacts_still_attempts_the_database_seal(tmp_registry,
                                                                           monkeypatch):
    """Filesystem cleanup is best effort. The row is not."""
    exp_dir = _arm_good_run(tmp_registry, monkeypatch, seq=21)
    monkeypatch.setattr(experiments, "close_trial",
                        _flaky_seal(exp_dir, experiments.close_trial))

    def unlinkable(self, *a, **kw):
        raise OSError("cannot remove artifact")

    monkeypatch.setattr(Path, "unlink", unlinkable)

    with pytest.raises(OSError):
        trial_mod.execute("EXP-000009", allow_dirty=True)

    row = _trial_row("EXP-000009", 21)
    assert row["outcome"] == "error", "cleanup failure must not block the error seal"


def test_a_cleanup_failure_never_claims_the_recovery_succeeded(tmp_registry, monkeypatch):
    """Both the original sealing failure and the cleanup failure stay visible."""
    exp_dir = _arm_good_run(tmp_registry, monkeypatch, seq=22)
    monkeypatch.setattr(experiments, "close_trial",
                        _flaky_seal(exp_dir, experiments.close_trial))
    # Distinct messages per file: identical ones would let this pass while an earlier
    # failure was silently dropped from the chain, which is exactly what happened once.
    monkeypatch.setattr(Path, "unlink",
                        lambda self, *a, **kw: (_ for _ in ()).throw(
                            OSError(f"cannot remove {self.name}")))

    with pytest.raises(OSError) as caught:
        trial_mod.execute("EXP-000009", allow_dirty=True)

    joined = " | ".join(_chain(caught.value))
    assert "registry unavailable while sealing" in joined
    assert "cannot remove metrics.json" in joined, "the headline cleanup failure vanished"
    assert "cannot remove metrics-trial-0022.json" in joined


def test_a_seal_failure_when_the_registry_is_also_gone_reports_both(tmp_registry,
                                                                    monkeypatch):
    _arm_good_run(tmp_registry, monkeypatch, seq=23)

    def always_fails(experiment_id, seq, outcome, metrics=None, *, is_headline=True):
        raise OSError(f"registry unavailable ({outcome})")

    monkeypatch.setattr(experiments, "close_trial", always_fails)
    with pytest.raises(OSError) as caught:
        trial_mod.execute("EXP-000009", allow_dirty=True)

    joined = " | ".join(_chain(caught.value))
    assert "registry unavailable (ok)" in joined
    assert "registry unavailable (error)" in joined
