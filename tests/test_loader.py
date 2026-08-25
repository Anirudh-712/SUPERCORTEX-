"""Slice B - can this sealed trial be independently reconstructed and trusted?

The loader answers one question and answers it in one of two ways: a verified
`TrialEvidence`, or a typed refusal naming the invariant that failed.

Two properties govern the fixtures here.

**Law 6.** Engine validation uses `supercortex.backtest.golden`, never a plausible
strategy. Both fixtures run `GoldenStrategy`: one over the synthetic sawtooth (proving the
`synthetic_fixture` provenance contract Slice A wrote), one over a registered catalog
(proving the dataset contract). No CIO-owned strategy is executed or edited.

**Authenticity.** The evidence under test is producer-generated - a real Nautilus backtest
through `runner.run`, sealed by `trial.execute` and `close_trial`. Hand-built evidence
would only prove the loader agrees with the test author.
"""
from __future__ import annotations

import dataclasses
import json
import math
import shutil
import subprocess
from pathlib import Path

import polars as pl
import pytest

from supercortex.backtest import evidence, feed, golden, runner
from supercortex.backtest import trial as trial_mod
from supercortex.costs.model import CostModel, Trade
from supercortex.falsify import loader
from supercortex.registry import db, experiments

BASE = golden.EPOCH_START
MINUTE = 60_000
HOUR = 3_600_000

#: The implementations reconstruction actually depends on. Committed into every fixture
#: repository so `git show` at the recorded commit returns the same bytes the reader runs.
LOAD_BEARING = (
    "src/supercortex/backtest/feed.py",
    "src/supercortex/backtest/runner.py",
    "src/supercortex/backtest/evidence.py",
    "src/supercortex/backtest/golden.py",
    "src/supercortex/backtest/trial.py",
    "src/supercortex/costs/model.py",
    "src/supercortex/falsify/statistics.py",
    "src/supercortex/falsify/loader.py",
    "src/supercortex/registry/db.py",
    "uv.lock",
)

GOLDEN_PARAMS = {"entry_bar": 10, "exit_bar": 30, "quantity": 1.0, "repeat_every": 60}
SAWTOOTH = {"n_bars": 240, "low": 100.0, "high": 140.0, "period": 40}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def head_of(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def _config(*, datasets: list[str] | None = None) -> str:
    block = ""
    if datasets is not None:
        block = ("data:\n  symbol: BTCUSDT\n  market: um\n  interval: 1h\n"
                 f"  datasets: [{', '.join(datasets)}]\n")
    return (
        "experiment_id: EXP-000009\n"
        "strategy: supercortex.backtest.golden:GoldenStrategy\n"
        "seed: 42\nstarting_balance: 1000000.0\nleverage: 1.0\nlatency_ms: 250\n"
        "params:\n" + "".join(f"  {k}: {v}\n" for k, v in GOLDEN_PARAMS.items()) + block
    )


def _write_catalog(root: Path) -> tuple[Path, Path]:
    """A small L1 catalog. Prices step by more than the instrument's 0.1 precision so a
    one-minute change is a change the engine could actually have seen."""
    cat = root / "data" / "catalog"
    (cat / "klines" / "BTCUSDT.um").mkdir(parents=True)
    (cat / "funding_rate" / "BTCUSDT.um").mkdir(parents=True)
    n = 4000
    wave = [10 * math.sin(i / 20) for i in range(n)]
    pl.DataFrame({
        "ts_ms": [BASE + i * MINUTE for i in range(n)],
        "open": [100.0 + w for w in wave], "high": [101.0 + w for w in wave],
        "low": [99.0 + w for w in wave], "close": [100.5 + w for w in wave],
        "volume": [1.0] * n, "quote_volume": [100.0] * n, "trades": [1] * n,
        "taker_buy_base": [0.5] * n, "taker_buy_quote": [50.0] * n,
    }).with_columns(
        pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.replace_time_zone("UTC").alias("ts")
    ).write_parquet(cat / "klines" / "BTCUSDT.um" / "1m.parquet")
    events = 9
    pl.DataFrame({
        "ts_ms": [BASE + i * 8 * HOUR for i in range(events)],
        # Distinct rates: a scramble over identical values is a no-op, and the digest
        # test would then pass for the wrong reason.
        "funding_rate": [0.0001 * (i + 1) * (-1 if i % 2 else 1) for i in range(events)],
        "funding_interval_hours": [8] * events,
    }).with_columns(
        pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.replace_time_zone("UTC").alias("ts")
    ).write_parquet(cat / "funding_rate" / "BTCUSDT.um" / "funding.parquet")
    return (cat / "klines" / "BTCUSDT.um" / "1m.parquet",
            cat / "funding_rate" / "BTCUSDT.um" / "funding.parquet")


def _seed_repo(root: Path, config_text: str) -> str:
    exp = root / "02-experiments" / "EXP-000009-probe"
    exp.mkdir(parents=True, exist_ok=True)
    (exp / "config.yaml").write_text(config_text, encoding="utf-8")
    for relative in LOAD_BEARING:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(relative), target)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "fixture")
    _git(root, "add", "02-experiments", "src", "uv.lock")
    _git(root, "commit", "-q", "-m", "fixture")
    return head_of(root)


def _wire(root: Path, monkeypatch, config_text: str, datasets: dict[str, Path]) -> str:
    registry = root / "registry.sqlite"
    db.init(registry)
    monkeypatch.setattr(db, "DB_PATH", registry)
    monkeypatch.setattr(db, "PROJECT_ROOT", root)
    monkeypatch.setattr(experiments, "PROJECT_ROOT", root)
    monkeypatch.setattr(experiments, "EXPERIMENTS_DIR", root / "02-experiments")
    monkeypatch.setattr(loader, "PROJECT_ROOT", root)
    commit = _seed_repo(root, config_text)
    with db.connect(registry) as conn:
        conn.execute(
            "INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
            " VALUES ('EXP-000009','probe','probe','BACKTESTING',10,?)", (db.now(),))
        for ident, path in datasets.items():
            conn.execute(
                "INSERT INTO dataset(id,title,venue,symbol,sha256,rows,start_ts,end_ts,"
                "schema_path,verified_at,kind,parquet_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ident, ident, "binance", "BTCUSDT.um", db.sha256_file(path), 1,
                 "a", "b", "s", db.now(),
                 ("klines" if "/klines/" in path.as_posix()
                  else "funding" if "/funding_rate/" in path.as_posix() else "metrics"),
                 path.relative_to(root).as_posix()))

    def opener(experiment_id, **kw):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
                "cost_model_version,seed,started_at) VALUES (?,?,?,?,?,?,?,?)",
                (experiment_id, 1, db.sha256_text(config_text), commit,
                 kw["data_snapshot"], kw["cost_model_version"], kw["seed"], db.now()))
        return 1

    monkeypatch.setattr(experiments, "open_trial", opener)
    return commit


@pytest.fixture
def golden_desk(tmp_path, monkeypatch):
    """GoldenStrategy over the synthetic sawtooth: the `synthetic_fixture` contract."""
    _wire(tmp_path, monkeypatch, _config(), {})

    # config.yaml cannot express a synthetic frame, so the spec is decorated rather than
    # the runner replaced: `trial.execute` still registers, seals, and assembles evidence,
    # and the REAL `runner.run` drives a real Nautilus backtest over the fixture.
    def synthetic_spec(**kwargs):
        return runner.BacktestSpec(
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


@pytest.fixture(autouse=True)
def _fresh_tamper_state():
    _CURRENT.clear()
    yield
    _CURRENT.clear()


@pytest.fixture
def three_dataset_desk(tmp_path, monkeypatch):
    """A trial that DECLARES three datasets and reads two. Everything else is consistent."""
    klines, funding = _write_catalog(tmp_path)
    extra = tmp_path / "data" / "catalog" / "metrics" / "BTCUSDT.um"
    extra.mkdir(parents=True)
    metrics = extra / "metrics.parquet"
    pl.DataFrame({"ts_ms": [BASE], "open_interest": [1.0]}).write_parquet(metrics)
    monkeypatch.setattr(feed, "CATALOG", tmp_path / "data" / "catalog")
    _wire(tmp_path, monkeypatch,
          _config(datasets=["DATA-000002", "DATA-000003", "DATA-000004"]),
          {"DATA-000002": klines, "DATA-000003": funding, "DATA-000004": metrics})
    trial_mod.execute("EXP-000009", allow_dirty=True)
    return tmp_path


@pytest.fixture
def catalog_desk(tmp_path, monkeypatch):
    """GoldenStrategy over a registered catalog: the `registered_dataset` contract."""
    klines, funding = _write_catalog(tmp_path)
    monkeypatch.setattr(feed, "CATALOG", tmp_path / "data" / "catalog")
    _wire(tmp_path, monkeypatch, _config(datasets=["DATA-000002", "DATA-000003"]),
          {"DATA-000002": klines, "DATA-000003": funding})
    trial_mod.execute("EXP-000009", allow_dirty=True)
    return tmp_path


#: The artifact each root currently points at. `edit` moves it, because a tampered
#: document is only interesting if it is also REGISTERED - the artifact digest makes an
#: unregistered edit a one-line refusal, so every downstream check would be unreachable.
_CURRENT: dict[Path, Path] = {}
_NEXT_SEQ = [900]


def artifact(root: Path) -> Path:
    return _CURRENT.get(root, root / "02-experiments" / "EXP-000009-probe" / "artifacts"
                        / "metrics-trial-0001.json")


def edit(path: Path, mutate) -> Path:
    """Tamper with the evidence AND register the result as a sealed trial of its own.

    Modelling a forger who can only edit the file would test one thing forever: the
    artifact digest. Giving the forger a matching registry row is the stronger threat, and
    it is what keeps every other invariant under test.
    """
    root = path.parents[3]
    document = json.loads(path.read_text(encoding="utf-8"))
    seq = next_seq()
    document["trial_seq"] = seq
    document["falsification_input"]["identity"]["trial_seq"] = seq
    mutate(document)

    target = path.parent / f"metrics-trial-{seq:04d}.json"
    payload = experiments.dump_metrics(document).encode("utf-8")
    target.write_bytes(payload)

    _register(root, seq, target)
    _CURRENT[root] = target
    return target


def clone_trial(root: Path, seq: int, *, outcome: str | None = "ok",
                commit: str | None = None, config_text: str | None = None) -> Path:
    """A second registered trial, INSERTed rather than updated - the trial table is
    append-only and refuses edits to a sealed row, which is the guarantee the whole desk
    rests on. The artifact is copied and relabelled to match."""
    source = artifact(root)
    target = source.parent / f"metrics-trial-{seq:04d}.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    document["trial_seq"] = seq
    document["falsification_input"]["identity"]["trial_seq"] = seq
    if commit is not None:
        document["falsification_input"]["identity"]["code_commit"] = commit
    target.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")

    identity = document["falsification_input"]["identity"]
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
            "cost_model_version,seed,started_at,finished_at,outcome,metrics_path,"
            "artifact_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("EXP-000009", seq,
             db.sha256_text(config_text) if config_text else identity["config_hash"],
             commit or identity["code_commit"], identity["data_snapshot"],
             "india-vda-1.0.0", identity["seed"], db.now(), db.now(), outcome,
             target.relative_to(root).as_posix(),
             db.sha256_file(target) if outcome == "ok" else None))
    return target


def _register(root: Path, seq: int, target: Path) -> None:
    """Seal a row for a forged artifact, carrying the ORIGINAL trial's row values.

    The forger controls the document, not the registry: the row is copied from the real
    trial so identity-versus-row checks still have something authoritative to disagree
    with.
    """
    with db.connect() as conn:
        source = conn.execute(
            "SELECT config_hash, code_commit, data_snapshot, cost_model_version, seed"
            " FROM trial WHERE experiment_id = 'EXP-000009' AND seq = 1").fetchone()
        conn.execute(
            "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
            "cost_model_version,seed,started_at,finished_at,outcome,metrics_path,"
            "artifact_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("EXP-000009", seq, source["config_hash"], source["code_commit"],
             source["data_snapshot"], source["cost_model_version"], source["seed"],
             db.now(), db.now(), "ok", target.relative_to(root).as_posix(),
             db.sha256_file(target)))


def next_seq() -> int:
    _NEXT_SEQ[0] += 1
    return _NEXT_SEQ[0] - 1


def forged_document(root: Path, seq: int) -> dict:
    """The sealed document, relabelled for a new trial the caller is about to register."""
    document = json.loads(artifact(root).read_text(encoding="utf-8"))
    document["trial_seq"] = seq
    document["falsification_input"]["identity"]["trial_seq"] = seq
    return document


def register_raw(root: Path, text: str, seq: int) -> Path:
    """Register bytes that are not valid JSON, so the parser boundary can be tested."""
    target = (root / "02-experiments" / "EXP-000009-probe" / "artifacts"
              / f"metrics-trial-{seq:04d}.json")
    target.write_text(text, encoding="utf-8")
    _register(root, seq, target)
    _CURRENT[root] = target
    return target


def refusal(path: Path) -> loader.Diagnostic:
    with pytest.raises(loader.Unverifiable) as caught:
        loader.verify(path)
    return caught.value.diagnostic


# ══════════════════════════════════════════════════════════════════════════════
# the two happy paths
# ══════════════════════════════════════════════════════════════════════════════
def test_the_golden_fixture_verifies(golden_desk):
    verified = loader.verify(artifact(golden_desk))
    assert verified.experiment_id == "EXP-000009"
    assert verified.series_source == "synthetic_fixture"
    assert verified.dataset_ids == ()
    assert verified.n_points == SAWTOOTH["n_bars"]


def test_a_registered_dataset_trial_verifies(catalog_desk):
    verified = loader.verify(artifact(catalog_desk))
    assert verified.series_source == "registered_dataset"
    assert verified.dataset_ids == ("DATA-000002", "DATA-000003")
    assert verified.n_points > 0


# ══════════════════════════════════════════════════════════════════════════════
# 1. the evidence must be the REGISTERED trial, not a self-consistent document
# ══════════════════════════════════════════════════════════════════════════════
def test_an_artifact_for_a_trial_that_is_not_registered_is_refused(golden_desk):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["identity"].__setitem__("trial_seq", 99))
    assert refusal(artifact(golden_desk)).code is loader.Code.TRIAL_NOT_FOUND


def test_a_copied_artifact_claiming_another_experiment_is_refused(golden_desk):
    def relabel(document):
        document["experiment_id"] = "EXP-000042"
        document["falsification_input"]["identity"]["experiment_id"] = "EXP-000042"
    edit(artifact(golden_desk), relabel)
    assert refusal(artifact(golden_desk)).code is loader.Code.TRIAL_NOT_FOUND


@pytest.mark.parametrize("field", ["config_hash", "code_commit", "data_snapshot", "seed"])
def test_identity_that_disagrees_with_the_registry_row_is_refused(golden_desk, field):
    """A consistently edited document must still fail: the row is authoritative."""
    replacement = 999 if field == "seed" else "f" * 40
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["identity"].__setitem__(field, replacement))
    diagnostic = refusal(artifact(golden_desk))
    assert diagnostic.code is loader.Code.IDENTITY_ROW_MISMATCH
    assert field in diagnostic.message


def test_top_level_metrics_identity_must_match_the_evidence(golden_desk):
    edit(artifact(golden_desk), lambda d: d.__setitem__("trial_seq", 7))
    assert refusal(artifact(golden_desk)).code is loader.Code.METRICS_IDENTITY_MISMATCH


def test_an_artifact_at_the_wrong_path_is_refused(golden_desk):
    original = artifact(golden_desk)
    moved = original.parent / "metrics-copy.json"
    shutil.copy2(original, moved)
    diagnostic = refusal(moved)
    assert diagnostic.code is loader.Code.ARTIFACT_PATH_MISMATCH


def test_a_trial_not_sealed_ok_is_refused(golden_desk):
    path = clone_trial(golden_desk, 2, outcome="error")
    diagnostic = refusal(path)
    assert diagnostic.code is loader.Code.TRIAL_NOT_SEALED
    assert "error" in diagnostic.observed


def test_an_open_trial_is_refused(golden_desk):
    path = clone_trial(golden_desk, 3, outcome=None)
    assert refusal(path).code is loader.Code.TRIAL_NOT_SEALED


# ══════════════════════════════════════════════════════════════════════════════
# 2. the config path is untrusted input, and the commit's config is authoritative
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("hostile", [
    "/etc/passwd", "../../.env", "02-experiments/../../.env",
    "02-experiments/EXP-000009-probe/../../../.env", "",
])
def test_a_hostile_config_path_is_refused(golden_desk, hostile):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["identity"].__setitem__("config_path", hostile))
    assert refusal(artifact(golden_desk)).code is loader.Code.CONFIG_PATH_UNSAFE


def test_a_config_path_for_another_experiment_is_refused(golden_desk):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["identity"].__setitem__(
             "config_path", "02-experiments/EXP-000001-other/config.yaml"))
    assert refusal(artifact(golden_desk)).code is loader.Code.CONFIG_PATH_UNSAFE


def test_a_symlink_escape_is_refused(golden_desk):
    outside = golden_desk.parent / "outside-secrets.yaml"
    outside.write_text("stolen: true\n", encoding="utf-8")
    link = golden_desk / "02-experiments" / "EXP-000009-probe" / "escape.yaml"
    link.symlink_to(outside)
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["identity"].__setitem__(
             "config_path", "02-experiments/EXP-000009-probe/escape.yaml"))
    assert refusal(artifact(golden_desk)).code is loader.Code.CONFIG_PATH_UNSAFE


def test_the_config_is_read_from_the_recorded_commit(golden_desk):
    """Working-tree edits do not invalidate a trial: the trial ran against the committed
    config, and that is what is hashed. Stated as a test so the contract is explicit."""
    config = golden_desk / "02-experiments" / "EXP-000009-probe" / "config.yaml"
    config.write_text(_config() + "\n# edited after the trial\n", encoding="utf-8")
    loader.verify(artifact(golden_desk))


def test_a_config_absent_at_the_recorded_commit_is_refused(golden_desk):
    _git(golden_desk, "rm", "-q", "02-experiments/EXP-000009-probe/config.yaml")
    _git(golden_desk, "commit", "-q", "-m", "drop config")
    path = clone_trial(golden_desk, 5, commit=head_of(golden_desk))
    assert refusal(path).code is loader.Code.CONFIG_NOT_FOUND


def test_a_config_changed_in_the_commit_is_a_hash_mismatch(golden_desk):
    config = golden_desk / "02-experiments" / "EXP-000009-probe" / "config.yaml"
    config.write_text(_config().replace("entry_bar: 10", "entry_bar: 11"), encoding="utf-8")
    _git(golden_desk, "add", "-A")
    _git(golden_desk, "commit", "-q", "-m", "change config")
    path = clone_trial(golden_desk, 6, commit=head_of(golden_desk))
    diagnostic = refusal(path)
    assert diagnostic.code is loader.Code.CONFIG_HASH_MISMATCH
    assert "entry_bar" not in (diagnostic.message + str(diagnostic.observed))


# ══════════════════════════════════════════════════════════════════════════════
# 3. datasets are verified as FILES, not as rows of text
# ══════════════════════════════════════════════════════════════════════════════
def test_a_mutated_klines_file_is_refused(catalog_desk):
    path = catalog_desk / "data" / "catalog" / "klines" / "BTCUSDT.um" / "1m.parquet"
    frame = pl.read_parquet(path)
    closes = frame["close"].to_list()
    closes[-1] = closes[-1] + 0.5
    frame.with_columns(pl.Series("close", closes)).write_parquet(path)
    assert refusal(artifact(catalog_desk)).code is loader.Code.DATASET_FILE_MISMATCH


def test_a_mutated_funding_file_is_refused(catalog_desk):
    """The price digest cannot see funding. Only file hashing catches this."""
    path = catalog_desk / "data" / "catalog" / "funding_rate" / "BTCUSDT.um" / "funding.parquet"
    frame = pl.read_parquet(path)
    rates = frame["funding_rate"].to_list()
    rates[0] = rates[0] * 5
    frame.with_columns(pl.Series("funding_rate", rates)).write_parquet(path)
    diagnostic = refusal(artifact(catalog_desk))
    assert diagnostic.code is loader.Code.DATASET_FILE_MISMATCH
    assert "DATA-000003" in diagnostic.observed


def test_a_sub_precision_klines_edit_is_still_caught_by_file_hashing(catalog_desk):
    """Below the instrument's price precision the digest cannot see it. The file hash can."""
    path = catalog_desk / "data" / "catalog" / "klines" / "BTCUSDT.um" / "1m.parquet"
    frame = pl.read_parquet(path)
    closes = frame["close"].to_list()
    closes[-1] = math.nextafter(closes[-1], math.inf)
    frame.with_columns(pl.Series("close", closes)).write_parquet(path)
    assert refusal(artifact(catalog_desk)).code is loader.Code.DATASET_FILE_MISMATCH


def test_a_missing_dataset_file_is_refused(catalog_desk):
    (catalog_desk / "data" / "catalog" / "funding_rate" / "BTCUSDT.um"
     / "funding.parquet").unlink()
    assert refusal(artifact(catalog_desk)).code is loader.Code.DATASET_FILE_MISSING


def test_a_dataset_without_a_parquet_path_is_refused(catalog_desk):
    with db.connect() as conn:
        conn.execute("UPDATE dataset SET parquet_path = NULL WHERE id = 'DATA-000002'")
    assert refusal(artifact(catalog_desk)).code is loader.Code.DATASET_PATH_UNSAFE


@pytest.mark.parametrize("hostile", ["/etc/passwd", "../../.env"])
def test_a_hostile_dataset_path_is_refused(catalog_desk, hostile):
    with db.connect() as conn:
        conn.execute("UPDATE dataset SET parquet_path = ? WHERE id = 'DATA-000002'",
                     (hostile,))
    assert refusal(artifact(catalog_desk)).code is loader.Code.DATASET_PATH_UNSAFE


def test_the_loaded_bars_source_must_be_a_registered_dataset(catalog_desk):
    """The klines file the engine read must be one of the datasets the trial declared.

    Isolated deliberately: the decoy is a byte-identical COPY at a different path, so the
    file hash and the snapshot both still pass and only the source binding can fail. An
    earlier check firing first would leave this one unproven.
    """
    catalog = catalog_desk / "data" / "catalog" / "klines"
    (catalog / "OTHER.um").mkdir(parents=True)
    decoy = catalog / "OTHER.um" / "1m.parquet"
    shutil.copy2(catalog / "BTCUSDT.um" / "1m.parquet", decoy)
    with db.connect() as conn:
        conn.execute("UPDATE dataset SET parquet_path = ? WHERE id = 'DATA-000002'",
                     (decoy.relative_to(catalog_desk).as_posix(),))
    assert refusal(artifact(catalog_desk)).code is loader.Code.DATASET_SOURCE_UNVERIFIED


def test_a_dataset_no_longer_registered_is_refused(catalog_desk):
    with db.connect() as conn:
        conn.execute("DELETE FROM dataset WHERE id = 'DATA-000003'")
    assert refusal(artifact(catalog_desk)).code is loader.Code.DATASET_NOT_FOUND


def test_a_changed_registry_hash_is_refused(catalog_desk):
    with db.connect() as conn:
        conn.execute("UPDATE dataset SET sha256 = ? WHERE id = 'DATA-000002'", ("c" * 64,))
    assert refusal(artifact(catalog_desk)).code in {
        loader.Code.DATASET_SNAPSHOT_MISMATCH, loader.Code.DATASET_FILE_MISMATCH}


# ══════════════════════════════════════════════════════════════════════════════
# 4. no inference from a malformed transform
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("field", ["bars_shift", "bars_scramble", "funding_shift",
                                   "funding_scramble", "label"])
def test_a_probe_missing_a_field_is_refused(golden_desk, field):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["price_series"]["transform"]["probe"].pop(field))
    diagnostic = refusal(artifact(golden_desk))
    assert diagnostic.code is loader.Code.MALFORMED_EVIDENCE
    assert field in diagnostic.message


@pytest.mark.parametrize(("field", "value"), [
    ("bars_shift", "1"), ("bars_shift", 1.5), ("bars_scramble", "true"),
    ("bars_scramble", 1), ("funding_shift", None),
])
def test_a_probe_field_of_the_wrong_type_is_refused(golden_desk, field, value):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["price_series"]["transform"]["probe"]
         .__setitem__(field, value))
    assert refusal(artifact(golden_desk)).code is loader.Code.MALFORMED_EVIDENCE


@pytest.mark.parametrize("value", ["0", 1.5, True])
def test_a_window_bound_of_the_wrong_type_is_refused(golden_desk, value):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["price_series"]["transform"]["window"]
         .__setitem__("start_ms", value))
    assert refusal(artifact(golden_desk)).code is loader.Code.MALFORMED_EVIDENCE


def test_a_missing_window_is_refused(golden_desk):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["price_series"]["transform"].pop("window"))
    assert refusal(artifact(golden_desk)).code is loader.Code.MALFORMED_EVIDENCE


@pytest.mark.parametrize("fact", ["encoding", "n_points", "first_ts_event_ns",
                                  "last_ts_event_ns"])
def test_a_sealed_series_fact_that_disagrees_with_reconstruction_is_refused(golden_desk,
                                                                            fact):
    wrong = "scx-series-999" if fact == "encoding" else 123456789
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["price_series"].__setitem__(fact, wrong))
    diagnostic = refusal(artifact(golden_desk))
    assert diagnostic.code in {loader.Code.SERIES_FACT_MISMATCH,
                               loader.Code.MALFORMED_EVIDENCE}
    assert fact in diagnostic.message


def test_a_changed_generator_parameter_is_refused(golden_desk):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["price_series"]["generator_parameters"]
         .__setitem__("period", 80))
    assert refusal(artifact(golden_desk)).code is loader.Code.SERIES_DIGEST_MISMATCH


def test_an_unapproved_generator_is_refused(golden_desk):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["price_series"].__setitem__(
             "generator", "somewhere.else:make_prices"))
    assert refusal(artifact(golden_desk)).code is loader.Code.SERIES_RECONSTRUCTION_FAILED


def test_a_changed_epoch_convention_is_refused(golden_desk):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["price_series"]["generator_parameters"]
         .__setitem__("epoch_start_ms", 1))
    assert refusal(artifact(golden_desk)).code is loader.Code.SERIES_RECONSTRUCTION_FAILED


# ══════════════════════════════════════════════════════════════════════════════
# 5. headline replay is mandatory, not conditional
# ══════════════════════════════════════════════════════════════════════════════
def test_a_missing_four_number_ladder_is_refused(golden_desk):
    edit(artifact(golden_desk), lambda d: d.pop("four_numbers"))
    assert refusal(artifact(golden_desk)).code is loader.Code.MALFORMED_EVIDENCE


def test_a_ladder_that_is_not_an_object_is_refused(golden_desk):
    edit(artifact(golden_desk), lambda d: d.__setitem__("four_numbers", [1, 2, 3, 4]))
    assert refusal(artifact(golden_desk)).code is loader.Code.MALFORMED_EVIDENCE


def test_a_ladder_with_an_extra_rung_is_refused(golden_desk):
    edit(artifact(golden_desk), lambda d: d["four_numbers"].__setitem__("bonus", 1.0))
    assert refusal(artifact(golden_desk)).code is loader.Code.HEADLINE_REPLAY_MISMATCH


def test_a_changed_sealed_sharpe_is_refused(golden_desk):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["returns"].__setitem__("sharpe_per_trade", 9.9))
    diagnostic = refusal(artifact(golden_desk))
    assert diagnostic.code is loader.Code.HEADLINE_REPLAY_MISMATCH
    assert "sharpe" in diagnostic.message


def test_a_changed_profit_factor_state_is_refused(golden_desk):
    edit(artifact(golden_desk),
         lambda d: d["trades"].__setitem__("profit_factor_gross_state", "unbounded"))
    diagnostic = refusal(artifact(golden_desk))
    assert diagnostic.code is loader.Code.HEADLINE_REPLAY_MISMATCH
    assert "profit_factor" in diagnostic.message


def test_a_changed_profit_factor_value_is_refused(golden_desk):
    document = json.loads(artifact(golden_desk).read_text())
    if document["trades"]["profit_factor_gross"] is None:
        pytest.skip("this fixture's profit factor is not finite")
    edit(artifact(golden_desk),
         lambda d: d["trades"].__setitem__("profit_factor_gross", 42.0))
    assert refusal(artifact(golden_desk)).code is loader.Code.HEADLINE_REPLAY_MISMATCH


@pytest.mark.parametrize("field", ["engine_realized_pnl", "our_gross_pnl",
                                   "reconciliation_diff"])
def test_a_missing_reconciliation_field_is_refused(golden_desk, field):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["reconciliation"].pop(field))
    diagnostic = refusal(artifact(golden_desk))
    assert diagnostic.code is loader.Code.MALFORMED_EVIDENCE
    assert field in diagnostic.message


def test_a_changed_n_trades_is_refused(golden_desk):
    edit(artifact(golden_desk),
         lambda d: d["trades"].__setitem__("n_trades", d["trades"]["n_trades"] + 1))
    assert refusal(artifact(golden_desk)).code is loader.Code.HEADLINE_REPLAY_MISMATCH


# ══════════════════════════════════════════════════════════════════════════════
# 7. the commit check is load-bearing
# ══════════════════════════════════════════════════════════════════════════════
def test_a_commit_absent_from_the_checkout_is_refused(golden_desk):
    path = clone_trial(golden_desk, 4, commit="0" * 40)
    diagnostic = refusal(path)
    assert diagnostic.code is loader.Code.COMMIT_NOT_FOUND
    assert diagnostic.retryable is True


@pytest.mark.parametrize("module", LOAD_BEARING[:5])
def test_reconstruction_code_drift_is_refused(golden_desk, module):
    """With HEAD pinned to the recorded commit, the residual risk is a DIRTY tree: the
    revision is right and the file on disk is not. Committing the edit would move HEAD and
    be caught one check earlier."""
    target = golden_desk / module
    target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    diagnostic = refusal(artifact(golden_desk))
    assert diagnostic.code is loader.Code.RECONSTRUCTION_CODE_DRIFT
    assert Path(module).name in diagnostic.message


def test_the_load_bearing_set_is_declared(golden_desk):
    assert set(loader.RECONSTRUCTION_SOURCES) == set(LOAD_BEARING)


# ══════════════════════════════════════════════════════════════════════════════
# legacy, diagnostics, zero consumption, zero mutation
# ══════════════════════════════════════════════════════════════════════════════
def test_a_legacy_artifact_is_refused_as_legacy(golden_desk):
    edit(artifact(golden_desk), lambda d: d.pop("falsification_input"))
    diagnostic = refusal(artifact(golden_desk))
    assert diagnostic.code is loader.Code.LEGACY_ARTIFACT
    assert "predates" in diagnostic.message


def test_the_real_legacy_artifacts_are_refused_cleanly():
    real = sorted(Path("02-experiments").glob("*/artifacts/metrics-trial-*.json"))
    assert real
    for path in real:
        with pytest.raises(loader.Unverifiable) as caught:
            loader.verify(path)
        assert caught.value.diagnostic.code is loader.Code.LEGACY_ARTIFACT


def test_an_unsupported_schema_version_is_refused(golden_desk):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"].__setitem__("schema_version", 3))
    assert refusal(artifact(golden_desk)).code is loader.Code.UNSUPPORTED_SCHEMA_VERSION


def test_every_diagnostic_names_its_invariant_and_identity(golden_desk):
    diagnostic = refusal(clone_trial(golden_desk, 7, outcome="error"))
    assert diagnostic.experiment_id == "EXP-000009"
    assert diagnostic.trial_seq == 7
    assert diagnostic.artifact.endswith(".json")
    assert isinstance(diagnostic.retryable, bool)


def test_diagnostics_never_carry_a_huge_payload(golden_desk):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["identity"].__setitem__("config_hash",
                                                                    "z" * 5000))
    diagnostic = refusal(artifact(golden_desk))
    for field in (diagnostic.expected, diagnostic.observed):
        assert field is None or len(field) <= loader.FIELD_LIMIT


def test_verification_consumes_zero_trials(catalog_desk):
    before = experiments.trial_count("EXP-000009")
    loader.verify(artifact(catalog_desk))
    loader.verify(artifact(catalog_desk))
    assert experiments.trial_count("EXP-000009") == before


def test_verification_mutates_nothing(catalog_desk):
    def fingerprint(root: Path) -> dict[str, bytes]:
        return {p.relative_to(root).as_posix(): p.read_bytes()
                for p in sorted(root.rglob("*")) if p.is_file() and ".git/" not in
                p.relative_to(root).as_posix()}

    before = fingerprint(catalog_desk)
    loader.verify(artifact(catalog_desk))
    assert fingerprint(catalog_desk) == before


def test_the_real_project_state_is_untouched():
    watched = [Path("registry.sqlite"),
               *sorted(Path("02-experiments").glob("*/artifacts/*.json"))]
    before = {p: p.read_bytes() for p in watched}
    for path in watched[1:]:
        with pytest.raises(loader.Unverifiable):
            loader.verify(path)
    assert {p: p.read_bytes() for p in watched} == before


def test_the_loader_never_mutates_the_working_tree_through_git(catalog_desk, monkeypatch):
    issued: list[list[str]] = []
    real_run = subprocess.run

    def recording(args, *rest, **kw):
        if args and args[0] == "git":
            issued.append(list(args))
        return real_run(args, *rest, **kw)

    monkeypatch.setattr(subprocess, "run", recording)
    loader.verify(artifact(catalog_desk))
    assert issued
    mutating = {"checkout", "reset", "stash", "clean", "switch", "restore",
                "worktree", "fetch", "pull", "merge", "commit", "add", "rm"}
    for command in issued:
        verbs = {token for token in command if not token.startswith("-")}
        assert not (verbs & mutating), command


def test_the_context_carries_no_unverified_field(golden_desk):
    verified = loader.verify(artifact(golden_desk))
    assert not hasattr(verified, "trials_consumed")
    assert not hasattr(verified, "sharpe_variance")


def test_series_digest_order_sensitivity_is_proven_in_the_encoding():
    assert evidence.series_digest([(1, 1.0), (2, 2.0)]) != \
        evidence.series_digest([(2, 2.0), (1, 1.0)])


# ══════════════════════════════════════════════════════════════════════════════
# 1. both loaded files must be bound, and the declared set must be exact
# ══════════════════════════════════════════════════════════════════════════════
def test_a_byte_identical_funding_decoy_is_caught_by_source_binding(catalog_desk):
    """Isolated: the decoy is a COPY, so its hash and the snapshot both still pass. Only
    source binding can catch it - and the engine reads the hard-coded funding path, not
    whatever the registry happens to point at."""
    catalog = catalog_desk / "data" / "catalog" / "funding_rate"
    (catalog / "OTHER.um").mkdir(parents=True)
    decoy = catalog / "OTHER.um" / "funding.parquet"
    shutil.copy2(catalog / "BTCUSDT.um" / "funding.parquet", decoy)
    with db.connect() as conn:
        conn.execute("UPDATE dataset SET parquet_path = ? WHERE id = 'DATA-000003'",
                     (decoy.relative_to(catalog_desk).as_posix(),))
    assert refusal(artifact(catalog_desk)).code is loader.Code.DATASET_SOURCE_UNVERIFIED


def test_a_dataset_of_the_wrong_kind_is_refused(catalog_desk):
    with db.connect() as conn:
        conn.execute("UPDATE dataset SET kind = 'metrics' WHERE id = 'DATA-000003'")
    assert refusal(artifact(catalog_desk)).code is loader.Code.DATASET_SOURCE_UNVERIFIED


def test_an_unrelated_extra_dataset_is_refused(catalog_desk):
    """The evidence claims these are the datasets the trial read.

    Caught by the snapshot check rather than by source binding: `data_snapshot` is computed
    over the declared id set, so adding an id changes it. The source-binding `extra` branch
    is a second guard for a declared dataset that resolves to an unread file, which the
    funding-decoy test exercises through the missing-file branch.
    """
    extra = catalog_desk / "data" / "catalog" / "metrics" / "BTCUSDT.um"
    extra.mkdir(parents=True)
    path = extra / "metrics.parquet"
    pl.DataFrame({"ts_ms": [1]}).write_parquet(path)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO dataset(id,title,venue,symbol,sha256,rows,start_ts,end_ts,"
            "schema_path,verified_at,kind,parquet_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("DATA-000004", "extra", "binance", "BTCUSDT.um", db.sha256_file(path), 1,
             "a", "b", "s", db.now(), "metrics",
             path.relative_to(catalog_desk).as_posix()))
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["identity"]["dataset_ids"].append("DATA-000004"))
    assert refusal(artifact(catalog_desk)).code in {
        loader.Code.DATASET_SNAPSHOT_MISMATCH, loader.Code.IDENTITY_ROW_MISMATCH,
        loader.Code.DATASET_SOURCE_UNVERIFIED}


def test_a_duplicated_dataset_id_is_refused(catalog_desk):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["identity"].__setitem__(
             "dataset_ids", ["DATA-000002", "DATA-000002", "DATA-000003"]))
    assert refusal(artifact(catalog_desk)).code is loader.Code.MALFORMED_EVIDENCE


# ══════════════════════════════════════════════════════════════════════════════
# 2. reconciliation is verified, not merely present
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("field", ["engine_realized_pnl", "our_gross_pnl",
                                   "reconciliation_diff"])
def test_a_changed_reconciliation_value_is_refused(catalog_desk, field):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["reconciliation"].__setitem__(field, 12345.0))
    diagnostic = refusal(artifact(catalog_desk))
    assert diagnostic.code is loader.Code.RECONCILIATION_MISMATCH
    assert field in diagnostic.message


def test_reconciliation_must_agree_with_the_sealed_engine_diagnostics(catalog_desk):
    edit(artifact(catalog_desk),
         lambda d: d["engine"].__setitem__("our_gross_pnl", 999.0))
    diagnostic = refusal(artifact(catalog_desk))
    assert diagnostic.code is loader.Code.RECONCILIATION_MISMATCH
    assert "engine" in diagnostic.message


@pytest.mark.parametrize("value", ["1.0", None, True, float("inf")])
def test_a_non_finite_reconciliation_value_is_refused(catalog_desk, value):
    seq = next_seq()
    document = forged_document(catalog_desk, seq)
    document["falsification_input"]["reconciliation"]["our_gross_pnl"] = value
    path = register_raw(catalog_desk,
                        json.dumps(document, indent=2, sort_keys=True, allow_nan=True),
                        seq)
    assert refusal(path).code is loader.Code.MALFORMED_EVIDENCE


def test_the_top_level_sharpe_is_verified_too(catalog_desk):
    """The number appears twice in the document. Validating only the evidence block would
    leave the headline copy free to say something else, and a reader quoting the headline
    would be quoting an unreplayed number out of a document labelled verified."""
    edit(artifact(catalog_desk),
         lambda d: d["trades"].__setitem__("sharpe_per_trade", 9.99))
    diagnostic = refusal(artifact(catalog_desk))
    assert diagnostic.code is loader.Code.HEADLINE_REPLAY_MISMATCH
    assert "sharpe_per_trade" in diagnostic.message


# ══════════════════════════════════════════════════════════════════════════════
# 3. every malformed document is a typed refusal, never a raw exception
# ══════════════════════════════════════════════════════════════════════════════
MALFORMED = [
    ("profit factor as a string",
     lambda d: d["trades"].__setitem__("profit_factor_gross", "1.5")),
    ("ladder value as a string",
     lambda d: d["four_numbers"].__setitem__(next(iter(d["four_numbers"])), "x")),
    ("ladder value as null",
     lambda d: d["four_numbers"].__setitem__(next(iter(d["four_numbers"])), None)),
    ("run_config missing",
     lambda d: d["falsification_input"].pop("run_config")),
    ("run_config not an object",
     lambda d: d["falsification_input"].__setitem__("run_config", [1, 2])),
    ("starting_balance missing",
     lambda d: d["falsification_input"]["run_config"].pop("starting_balance")),
    ("starting_balance as a string",
     lambda d: d["falsification_input"]["run_config"].__setitem__("starting_balance", "1")),
    ("starting_balance zero",
     lambda d: d["falsification_input"]["run_config"].__setitem__("starting_balance", 0)),
    ("dataset_ids not a list",
     lambda d: d["falsification_input"]["identity"].__setitem__("dataset_ids", "DATA-1")),
    ("dataset_ids holding a number",
     lambda d: d["falsification_input"]["identity"].__setitem__("dataset_ids", [7])),
    ("seed as a string",
     lambda d: d["falsification_input"]["identity"].__setitem__("seed", "42")),
    ("top-level experiment_id missing", lambda d: d.pop("experiment_id")),
    ("top-level trial_seq missing", lambda d: d.pop("trial_seq")),
    ("trades not a list",
     lambda d: d["falsification_input"].__setitem__("trades", {})),
    ("cost_model not an object",
     lambda d: d["falsification_input"].__setitem__("cost_model", "india-vda-1.0.0")),
    ("price_series not an object",
     lambda d: d["falsification_input"].__setitem__("price_series", 3)),
    ("returns not an object",
     lambda d: d["falsification_input"].__setitem__("returns", [0.1])),
    ("sharpe as a string",
     lambda d: d["falsification_input"]["returns"].__setitem__("sharpe_per_trade", "0")),
]


@pytest.mark.parametrize(("label", "mutate"), MALFORMED, ids=[m[0] for m in MALFORMED])
def test_a_malformed_document_is_a_typed_refusal(catalog_desk, label, mutate):
    fingerprint = {p: p.read_bytes() for p in sorted(catalog_desk.rglob("*.parquet"))}
    edit(artifact(catalog_desk), mutate)
    # taken AFTER tampering: the helper registers the forged artifact as a trial of its
    # own, and what must not move is the count across `verify` itself
    before = experiments.trial_count("EXP-000009")

    with pytest.raises(loader.Unverifiable) as caught:
        loader.verify(artifact(catalog_desk))
    diagnostic = caught.value.diagnostic
    assert diagnostic.code is not None, label
    assert diagnostic.message.strip(), label
    assert experiments.trial_count("EXP-000009") == before
    assert {p: p.read_bytes() for p in sorted(catalog_desk.rglob("*.parquet"))} == fingerprint


def test_json_with_nan_is_rejected(catalog_desk):
    """Python's parser accepts `NaN` and `Infinity`; JSON does not."""
    seq = next_seq()
    text = json.dumps(forged_document(catalog_desk, seq), indent=2, sort_keys=True)
    path = register_raw(catalog_desk,
                        text.replace('"n_trades"', '"nan_probe": NaN, "n_trades"', 1), seq)
    assert refusal(path).code is loader.Code.MALFORMED_EVIDENCE


def test_json_with_infinity_is_rejected(catalog_desk):
    seq = next_seq()
    text = json.dumps(forged_document(catalog_desk, seq), indent=2, sort_keys=True)
    path = register_raw(
        catalog_desk, text.replace('"n_trades"', '"inf_probe": Infinity, "n_trades"', 1),
        seq)
    assert refusal(path).code is loader.Code.MALFORMED_EVIDENCE


# ══════════════════════════════════════════════════════════════════════════════
# 4. the reconstruction boundary includes every dependency, and the environment
# ══════════════════════════════════════════════════════════════════════════════
def test_the_reconstruction_boundary_covers_every_dependency():
    assert set(loader.RECONSTRUCTION_SOURCES) >= {
        "src/supercortex/backtest/feed.py",
        "src/supercortex/backtest/runner.py",
        "src/supercortex/backtest/evidence.py",
        "src/supercortex/backtest/golden.py",
        "src/supercortex/backtest/trial.py",
        "src/supercortex/costs/model.py",
        "src/supercortex/falsify/statistics.py",
        "src/supercortex/registry/db.py",
        "uv.lock",
    }


@pytest.mark.parametrize("module", [
    "src/supercortex/backtest/trial.py",
    "src/supercortex/falsify/statistics.py",
    "src/supercortex/registry/db.py",
    "uv.lock",
])
def test_drift_in_a_newly_bound_dependency_is_refused(golden_desk, module):
    target = golden_desk / module
    target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    diagnostic = refusal(artifact(golden_desk))
    assert diagnostic.code is loader.Code.RECONSTRUCTION_CODE_DRIFT
    assert Path(module).name in diagnostic.message


def test_an_installed_version_that_disagrees_with_the_lockfile_is_refused(golden_desk,
                                                                          monkeypatch):
    """The engine's behaviour is part of reconstruction. A different NautilusTrader than
    the one pinned is a different reconstruction."""
    real = loader._installed_version

    def wrong(name: str) -> str | None:
        return "0.0.0" if name == "nautilus-trader" else real(name)

    monkeypatch.setattr(loader, "_installed_version", wrong)
    diagnostic = refusal(artifact(golden_desk))
    assert diagnostic.code is loader.Code.ENVIRONMENT_DRIFT
    assert "nautilus-trader" in diagnostic.message


def test_a_package_absent_from_the_environment_is_refused(golden_desk, monkeypatch):
    real = loader._installed_version
    monkeypatch.setattr(loader, "_installed_version",
                        lambda name: None if name == "polars" else real(name))
    assert refusal(artifact(golden_desk)).code is loader.Code.ENVIRONMENT_DRIFT


def test_the_pinned_packages_are_declared():
    assert set(loader.RECONSTRUCTION_PACKAGES) >= {"nautilus-trader", "polars", "numpy"}


# ══════════════════════════════════════════════════════════════════════════════
# 1. the reconciliation identity, with a NON-ZERO difference
# ══════════════════════════════════════════════════════════════════════════════
# Every fixture reconciles exactly, so a reversed sign was invisible: 0 == -0. The
# producer writes `our_gross_pnl - engine_realized_pnl`; anything else is a different
# quantity that happens to agree only when the two sides match.
def _reconciliation_case(our: float, engine: float, diff: float):
    trade = Trade(0, 10, 1, 1.0, 100.0, 100.0 + our)      # gross_pnl == our
    document = {"engine": {"engine_realized_pnl": engine, "our_gross_pnl": our,
                           "reconciliation_diff": diff}}
    block = {"reconciliation": {"engine_realized_pnl": engine, "our_gross_pnl": our,
                                "reconciliation_diff": diff}}
    return document, block, (trade,)


def test_reconciliation_follows_the_producers_sign_convention():
    """`round(our_gross - engine_pnl, 6)` - see runner.py."""
    document, block, trades = _reconciliation_case(our=10.0, engine=4.0, diff=6.0)
    loader._verify_reconciliation(document, block, trades,
                                  experiment_id="E", trial_seq=1, artifact="a")


def test_the_reversed_reconciliation_sign_is_refused():
    document, block, trades = _reconciliation_case(our=10.0, engine=4.0, diff=-6.0)
    with pytest.raises(loader.Unverifiable) as caught:
        loader._verify_reconciliation(document, block, trades,
                                      experiment_id="E", trial_seq=1, artifact="a")
    assert caught.value.diagnostic.code is loader.Code.RECONCILIATION_MISMATCH


def test_reconciliation_tolerance_matches_the_producers_rounding():
    """The producer rounds to six decimals, so the check must not wave through 1e-4."""
    document, block, trades = _reconciliation_case(our=10.0, engine=4.0, diff=6.0 + 1e-5)
    with pytest.raises(loader.Unverifiable):
        loader._verify_reconciliation(document, block, trades,
                                      experiment_id="E", trial_seq=1, artifact="a")


def test_reconciliation_accepts_the_producers_rounding_error():
    document, block, trades = _reconciliation_case(our=10.0, engine=4.0, diff=6.0 + 4e-7)
    loader._verify_reconciliation(document, block, trades,
                                  experiment_id="E", trial_seq=1, artifact="a")


# ══════════════════════════════════════════════════════════════════════════════
# 2. no Trade field is ever reconstructed from a default
# ══════════════════════════════════════════════════════════════════════════════
TRADE_FIELDS = ("entry_ts_ms", "exit_ts_ms", "direction", "qty", "entry_price",
                "exit_price", "entry_taker", "exit_taker", "funding_pnl")


def test_the_trade_schema_is_declared_in_full():
    assert loader.TRADE_FIELDS == TRADE_FIELDS


@pytest.mark.parametrize("field", TRADE_FIELDS)
def test_a_trade_missing_any_field_is_refused(catalog_desk, field):
    """`entry_taker`, `exit_taker` and `funding_pnl` used to fall back to the dataclass
    default, silently inventing execution and funding the trial never had."""
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["trades"][0].pop(field))
    diagnostic = refusal(artifact(catalog_desk))
    assert diagnostic.code is loader.Code.MALFORMED_EVIDENCE
    assert field in diagnostic.message


def test_a_trade_with_an_unknown_field_is_refused(catalog_desk):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["trades"][0].__setitem__("slippage", 1.0))
    assert refusal(artifact(catalog_desk)).code is loader.Code.MALFORMED_EVIDENCE


@pytest.mark.parametrize(("field", "value"), [
    ("entry_ts_ms", "0"), ("entry_ts_ms", 1.5), ("entry_ts_ms", True),
    ("direction", 1.0), ("qty", "1"), ("entry_taker", 1), ("entry_taker", "true"),
    ("funding_pnl", None),
])
def test_a_trade_field_of_the_wrong_type_is_refused(catalog_desk, field, value):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["trades"][0].__setitem__(field, value))
    assert refusal(artifact(catalog_desk)).code is loader.Code.MALFORMED_EVIDENCE


# ══════════════════════════════════════════════════════════════════════════════
# 3. no CostModel field is ever reconstructed from a default
# ══════════════════════════════════════════════════════════════════════════════
COST_FIELDS = tuple(f.name for f in dataclasses.fields(CostModel))


@pytest.mark.parametrize("field", COST_FIELDS)
def test_a_cost_model_missing_any_field_is_refused(catalog_desk, field):
    """Sealing the full effective model is pointless if the reader fills the gaps from
    today's defaults."""
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["cost_model"].pop(field))
    diagnostic = refusal(artifact(catalog_desk))
    assert diagnostic.code is loader.Code.MALFORMED_EVIDENCE
    assert field in diagnostic.message


def test_an_unknown_cost_model_field_is_refused(catalog_desk):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["cost_model"].__setitem__("stt_rate", 0.001))
    assert refusal(artifact(catalog_desk)).code is loader.Code.MALFORMED_EVIDENCE


@pytest.mark.parametrize(("field", "value"), [
    ("taker_fee_bps", "4.5"), ("taker_fee_bps", None), ("fees_deductible", 1),
    ("regime", "invented_regime"), ("regime", 3), ("notes", "a note"),
    ("version", 1),
])
def test_a_cost_model_field_of_the_wrong_type_is_refused(catalog_desk, field, value):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["cost_model"].__setitem__(field, value))
    assert refusal(artifact(catalog_desk)).code is loader.Code.MALFORMED_EVIDENCE


# ══════════════════════════════════════════════════════════════════════════════
# 4. the full run contract, cross-checked
# ══════════════════════════════════════════════════════════════════════════════
RUN_FIELDS = ("starting_balance", "leverage", "seed", "latency_ms", "interval",
              "symbol", "market")


@pytest.mark.parametrize("field", RUN_FIELDS)
def test_a_run_config_missing_any_field_is_refused(catalog_desk, field):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["run_config"].pop(field))
    diagnostic = refusal(artifact(catalog_desk))
    assert diagnostic.code is loader.Code.MALFORMED_EVIDENCE
    assert field in diagnostic.message


def test_an_unknown_run_config_field_is_refused(catalog_desk):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["run_config"].__setitem__("venue", "BINANCE"))
    assert refusal(artifact(catalog_desk)).code is loader.Code.MALFORMED_EVIDENCE


def test_a_run_config_seed_that_disagrees_with_identity_is_refused(catalog_desk):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["run_config"].__setitem__("seed", 7))
    diagnostic = refusal(artifact(catalog_desk))
    assert diagnostic.code is loader.Code.MALFORMED_EVIDENCE
    assert "seed" in diagnostic.message


def test_a_run_config_interval_that_disagrees_with_the_transform_is_refused(catalog_desk):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["run_config"].__setitem__("interval", "4h"))
    diagnostic = refusal(artifact(catalog_desk))
    assert diagnostic.code is loader.Code.MALFORMED_EVIDENCE
    assert "interval" in diagnostic.message


@pytest.mark.parametrize("field", ["symbol", "market"])
def test_a_run_config_symbol_that_disagrees_with_the_series_is_refused(catalog_desk, field):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["run_config"].__setitem__(field, "WRONG"))
    assert refusal(artifact(catalog_desk)).code is loader.Code.MALFORMED_EVIDENCE


@pytest.mark.parametrize("field", ["starting_balance", "leverage", "seed"])
def test_top_level_run_config_must_agree_with_the_evidence(catalog_desk, field):
    edit(artifact(catalog_desk),
         lambda d: d["run_config"].__setitem__(field, 12345))
    diagnostic = refusal(artifact(catalog_desk))
    assert diagnostic.code is loader.Code.MALFORMED_EVIDENCE
    assert field in diagnostic.message


def test_an_unknown_probe_field_is_refused(catalog_desk):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["price_series"]["transform"]["probe"]
         .__setitem__("metrics_shift", 1))
    assert refusal(artifact(catalog_desk)).code is loader.Code.MALFORMED_EVIDENCE


def test_an_unknown_window_field_is_refused(catalog_desk):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["price_series"]["transform"]["window"]
         .__setitem__("step_ms", 1))
    assert refusal(artifact(catalog_desk)).code is loader.Code.MALFORMED_EVIDENCE


def test_an_unknown_transform_field_is_refused(catalog_desk):
    edit(artifact(catalog_desk),
         lambda d: d["falsification_input"]["price_series"]["transform"]
         .__setitem__("resample", "1h"))
    assert refusal(artifact(catalog_desk)).code is loader.Code.MALFORMED_EVIDENCE


@pytest.mark.parametrize(("field", "value"), [
    ("n_bars", "240"), ("n_bars", 0), ("n_bars", -1), ("period", 0),
    ("low", "100"), ("high", None), ("bar_ms", 0),
])
def test_a_synthetic_generator_parameter_of_the_wrong_type_is_refused(golden_desk,
                                                                       field, value):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["price_series"]["generator_parameters"]
         .__setitem__(field, value))
    assert refusal(artifact(golden_desk)).code in {
        loader.Code.MALFORMED_EVIDENCE, loader.Code.SERIES_RECONSTRUCTION_FAILED}


def test_an_unknown_generator_parameter_is_refused(golden_desk):
    edit(artifact(golden_desk),
         lambda d: d["falsification_input"]["price_series"]["generator_parameters"]
         .__setitem__("drift", 0.1))
    assert refusal(artifact(golden_desk)).code in {
        loader.Code.MALFORMED_EVIDENCE, loader.Code.SERIES_RECONSTRUCTION_FAILED}


# ══════════════════════════════════════════════════════════════════════════════
# 5. the returned headline contains only replayed values
# ══════════════════════════════════════════════════════════════════════════════
HEADLINE_FIELDS = ("n_trades", "n_wins", "win_rate", "turnover", "profit_factor_gross",
                   "profit_factor_gross_state", "profit_factor_net",
                   "profit_factor_net_state", "break_even_profit_factor",
                   "clears_break_even", "sharpe_per_trade")


def test_the_returned_headline_is_exactly_the_replayed_fields(catalog_desk):
    verified = loader.verify(artifact(catalog_desk))
    assert set(verified.headline) == set(HEADLINE_FIELDS)


def test_the_returned_headline_is_the_replay_not_the_document(catalog_desk):
    """What is returned must be what the loader computed, so an unverified document field
    can never be handed downstream as trusted evidence."""
    from supercortex.costs.model import apply

    verified = loader.verify(artifact(catalog_desk))
    replay = apply(list(verified.trades), verified.model)
    assert verified.headline["n_trades"] == replay.n_trades
    assert verified.headline["turnover"] == pytest.approx(replay.turnover, abs=0.01)
    assert verified.headline["clears_break_even"] is replay.clears_break_even


def test_a_within_tolerance_document_value_is_not_returned(catalog_desk):
    """The distinguishing case. A value nudged inside the comparison tolerance still
    verifies - so if the document were returned, a slightly wrong number would be handed
    downstream wearing the label `verified`. What comes back must be the replay."""
    document = json.loads(artifact(catalog_desk).read_text(encoding="utf-8"))
    nudged = document["trades"]["turnover"] + 1e-6
    edit(artifact(catalog_desk), lambda d: d["trades"].__setitem__("turnover", nudged))

    verified = loader.verify(artifact(catalog_desk))
    assert verified.headline["turnover"] != nudged
    assert verified.headline["turnover"] == document["trades"]["turnover"]


@pytest.mark.parametrize("field", [f for f in HEADLINE_FIELDS
                                   if f not in {"profit_factor_gross",
                                                "profit_factor_net"}])
def test_a_changed_headline_field_is_refused(catalog_desk, field):
    document = json.loads(artifact(catalog_desk).read_text(encoding="utf-8"))
    current = document["trades"][field]
    replacement = (not current) if isinstance(current, bool) else (
        "drifted" if isinstance(current, str) else (current or 0) + 7)
    edit(artifact(catalog_desk), lambda d: d["trades"].__setitem__(field, replacement))
    diagnostic = refusal(artifact(catalog_desk))
    assert diagnostic.code is loader.Code.HEADLINE_REPLAY_MISMATCH
    assert field in diagnostic.message


def test_an_unknown_headline_field_is_refused(catalog_desk):
    edit(artifact(catalog_desk), lambda d: d["trades"].__setitem__("alpha", 1.0))
    assert refusal(artifact(catalog_desk)).code is loader.Code.HEADLINE_REPLAY_MISMATCH


def test_the_ordered_ladder_must_agree_with_the_verified_ladder(catalog_desk):
    """`four_numbers_ordered` is separately reportable, so it is separately verified."""
    edit(artifact(catalog_desk),
         lambda d: d["four_numbers_ordered"].__setitem__(
             0, [d["four_numbers_ordered"][0][0], d["four_numbers_ordered"][0][1] + 5]))
    diagnostic = refusal(artifact(catalog_desk))
    assert diagnostic.code is loader.Code.HEADLINE_REPLAY_MISMATCH
    assert "four_numbers_ordered" in diagnostic.message


def test_a_reordered_ladder_is_refused(catalog_desk):
    edit(artifact(catalog_desk),
         lambda d: d.__setitem__("four_numbers_ordered",
                                 list(reversed(d["four_numbers_ordered"]))))
    assert refusal(artifact(catalog_desk)).code is loader.Code.HEADLINE_REPLAY_MISMATCH


def test_unverified_document_sections_are_not_returned(catalog_desk):
    """`detail`, `regimes` and `verdict` are NOT replayed, so they are not handed on."""
    verified = loader.verify(artifact(catalog_desk))
    for section in ("detail", "regimes", "verdict"):
        assert section not in verified.headline
    assert not hasattr(verified, section)


# ══════════════════════════════════════════════════════════════════════════════
# 6. the boundary includes the loader, and the exact-source-set check is isolated
# ══════════════════════════════════════════════════════════════════════════════
def test_the_loader_is_inside_its_own_reconstruction_boundary():
    assert "src/supercortex/falsify/loader.py" in loader.RECONSTRUCTION_SOURCES


def test_loader_drift_is_refused(golden_desk):
    target = golden_desk / "src" / "supercortex" / "falsify" / "loader.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    diagnostic = refusal(artifact(golden_desk))
    assert diagnostic.code is loader.Code.RECONSTRUCTION_CODE_DRIFT
    assert "loader.py" in diagnostic.message


def test_a_declared_but_unread_dataset_is_refused_by_source_binding(three_dataset_desk):
    """Fully consistent: the row, the snapshot and the artifact identity all agree, every
    file hashes correctly, and the ONLY thing wrong is that the trial declared a dataset
    it never read. Nothing earlier can reject it."""
    diagnostic = refusal(artifact(three_dataset_desk))
    assert diagnostic.code is loader.Code.DATASET_SOURCE_UNVERIFIED
    assert "metrics" in diagnostic.observed


# ══════════════════════════════════════════════════════════════════════════════
# the funding stream is reconstructed, not merely described
# ══════════════════════════════════════════════════════════════════════════════
def test_a_changed_funding_shift_is_refused(catalog_desk):
    """The bar digest cannot see this. Only rebuilding the funding stream can."""
    path = edit(artifact(catalog_desk),
                lambda d: d["falsification_input"]["price_series"]["transform"]["probe"]
                .__setitem__("funding_shift", 7))
    # A shift moves the stamps as well as the stream, so the fact check may reach it
    # first. Either way the trial is refused, and neither code existed before.
    assert refusal(path).code in {loader.Code.FUNDING_DIGEST_MISMATCH,
                                  loader.Code.FUNDING_FACT_MISMATCH}


def test_a_changed_funding_scramble_is_refused(catalog_desk):
    path = edit(artifact(catalog_desk),
                lambda d: d["falsification_input"]["price_series"]["transform"]["probe"]
                .__setitem__("funding_scramble", True))
    assert refusal(path).code is loader.Code.FUNDING_DIGEST_MISMATCH


def test_a_changed_funding_digest_is_refused(catalog_desk):
    path = edit(artifact(catalog_desk),
                lambda d: d["falsification_input"]["funding_series"]
                .__setitem__("funding_sha256", "e" * 64))
    assert refusal(path).code is loader.Code.FUNDING_DIGEST_MISMATCH


@pytest.mark.parametrize("fact", ["n_events", "first_ts_event_ns", "last_ts_event_ns"])
def test_a_changed_funding_fact_is_refused(catalog_desk, fact):
    path = edit(artifact(catalog_desk),
                lambda d: d["falsification_input"]["funding_series"]
                .__setitem__(fact, 424242))
    assert refusal(path).code is loader.Code.FUNDING_FACT_MISMATCH


def test_a_mutated_funding_rate_changes_the_funding_digest(catalog_desk):
    """The file hash sees it too, but the digest is what proves the STREAM changed."""
    path = (catalog_desk / "data" / "catalog" / "funding_rate" / "BTCUSDT.um"
            / "funding.parquet")
    frame = pl.read_parquet(path)
    rates = frame["funding_rate"].to_list()
    rates[0] = rates[0] * 3
    frame.with_columns(pl.Series("funding_rate", rates)).write_parquet(path)
    assert refusal(artifact(catalog_desk)).code in {
        loader.Code.DATASET_FILE_MISMATCH, loader.Code.FUNDING_DIGEST_MISMATCH}


def test_a_reordered_funding_stream_changes_the_digest():
    base = [(1_000, 0.0001), (2_000, 0.0002)]
    assert evidence.funding_digest(base) != evidence.funding_digest(list(reversed(base)))


def test_a_synthetic_run_must_declare_that_it_had_no_funding(golden_desk):
    path = edit(artifact(golden_desk),
                lambda d: d["falsification_input"]["funding_series"].update(
                    {"source": evidence.FUNDING_SUPPLIED, "n_events": 3,
                     "funding_sha256": "a" * 64}))
    assert refusal(path).code is loader.Code.FUNDING_FACT_MISMATCH


def test_a_registered_run_claiming_no_funding_is_refused(catalog_desk):
    path = edit(artifact(catalog_desk),
                lambda d: d["falsification_input"]["funding_series"].update(
                    {"source": evidence.FUNDING_NONE, "n_events": 0,
                     "funding_sha256": None, "first_ts_event_ns": None,
                     "last_ts_event_ns": None}))
    assert refusal(path).code is loader.Code.FUNDING_FACT_MISMATCH


# ══════════════════════════════════════════════════════════════════════════════
# the committed config binds the effective contract
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("field", ["taker_fee_bps", "slippage_bps", "fees_deductible"])
def test_a_cost_model_that_the_committed_config_does_not_imply_is_refused(catalog_desk,
                                                                          field):
    """The reproduction that started this round: maker_fee_bps = 999.0 verified."""
    replacement = True if field == "fees_deductible" else 999.0
    path = edit(artifact(catalog_desk),
                lambda d: d["falsification_input"]["cost_model"]
                .__setitem__(field, replacement))
    diagnostic = refusal(path)
    assert diagnostic.code is loader.Code.CONFIG_CONTRACT_MISMATCH
    assert field in diagnostic.message


def test_a_maker_fee_edited_after_sealing_is_refused(catalog_desk):
    path = edit(artifact(catalog_desk),
                lambda d: d["falsification_input"]["cost_model"]
                .__setitem__("maker_fee_bps", 999.0))
    assert refusal(path).code is loader.Code.CONFIG_CONTRACT_MISMATCH


@pytest.mark.parametrize("field", ["latency_ms", "leverage", "starting_balance"])
def test_a_run_value_the_committed_config_does_not_imply_is_refused(catalog_desk, field):
    def forge(document):
        document["falsification_input"]["run_config"][field] = 999999
        # `latency_ms` is not part of the top-level block, which carries only the three
        # fields `_metrics` writes there.
        if field in document["run_config"]:
            document["run_config"][field] = 999999
    path = edit(artifact(catalog_desk), forge)
    diagnostic = refusal(path)
    assert diagnostic.code is loader.Code.CONFIG_CONTRACT_MISMATCH
    assert field in diagnostic.message


def test_a_cost_model_version_that_disagrees_with_the_row_is_refused(catalog_desk):
    path = edit(artifact(catalog_desk),
                lambda d: d["falsification_input"]["cost_model"]
                .__setitem__("version", "india-vda-9.9.9"))
    assert refusal(path).code in {loader.Code.CONFIG_CONTRACT_MISMATCH,
                                  loader.Code.MALFORMED_EVIDENCE}


def test_the_committed_config_binding_is_derived_not_restated():
    """Producer and loader share one config-to-contract definition."""
    from supercortex.backtest import trial as trial_module

    assert callable(trial_module.model_from_config)
    assert callable(trial_module.run_values_from_config)


# ══════════════════════════════════════════════════════════════════════════════
# schema-v1 exactness, finished
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(("section", "extra"), [
    ("reconciliation", "slippage_total"), ("returns", "sortino"),
    ("price_series", "venue"), ("identity", "operator"),
])
def test_an_unknown_field_in_a_sealed_section_is_refused(catalog_desk, section, extra):
    path = edit(artifact(catalog_desk),
                lambda d: d["falsification_input"][section].__setitem__(extra, 1))
    assert refusal(path).code is loader.Code.MALFORMED_EVIDENCE


@pytest.mark.parametrize("field", ["starting_balance", "leverage", "seed"])
def test_a_missing_top_level_run_config_field_is_refused(catalog_desk, field):
    """Absence must not pass merely because the comparison loop visits what exists."""
    path = edit(artifact(catalog_desk), lambda d: d["run_config"].pop(field))
    diagnostic = refusal(path)
    assert diagnostic.code is loader.Code.MALFORMED_EVIDENCE
    assert field in diagnostic.message


def test_an_unknown_top_level_run_config_field_is_refused(catalog_desk):
    path = edit(artifact(catalog_desk), lambda d: d["run_config"].__setitem__("venue", "BINANCE"))
    assert refusal(path).code is loader.Code.MALFORMED_EVIDENCE


def test_a_registered_series_with_generator_fields_is_refused(catalog_desk):
    """Source-specific keys are exact: a registered series has no generator."""
    path = edit(artifact(catalog_desk),
                lambda d: d["falsification_input"]["price_series"]
                .__setitem__("generator", "supercortex.backtest.golden:sawtooth"))
    assert refusal(path).code is loader.Code.MALFORMED_EVIDENCE


def test_a_synthetic_series_with_dataset_fields_is_refused(golden_desk):
    path = edit(artifact(golden_desk),
                lambda d: d["falsification_input"]["price_series"]
                .__setitem__("dataset_ids", ["DATA-000002"]))
    assert refusal(path).code is loader.Code.MALFORMED_EVIDENCE


# ══════════════════════════════════════════════════════════════════════════════
# the artifact digest, against an UNREGISTERED edit
# ══════════════════════════════════════════════════════════════════════════════
# Every other tampering test also registers the forged bytes, because that is the stronger
# threat. This is the weaker one, and it is the reproduction that opened this round: edit
# the file in place and change nothing else.
@pytest.mark.parametrize(("path_to", "value"), [
    (("falsification_input", "cost_model", "maker_fee_bps"), 999.0),
    (("falsification_input", "run_config", "latency_ms"), 999999),
    (("falsification_input", "price_series", "transform", "probe", "funding_shift"), 7),
    (("trades", "n_trades"), 999),
])
def test_an_unregistered_edit_is_refused_by_the_artifact_digest(catalog_desk, path_to,
                                                                value):
    target = artifact(catalog_desk)
    document = json.loads(target.read_text(encoding="utf-8"))
    node = document
    for key in path_to[:-1]:
        node = node[key]
    node[path_to[-1]] = value
    target.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")

    diagnostic = refusal(target)
    assert diagnostic.code is loader.Code.ARTIFACT_HASH_MISMATCH
    assert diagnostic.retryable is False


def test_replacing_the_artifact_at_the_recorded_path_is_refused(catalog_desk):
    """A different document at the same registered path is still not the sealed one.

    The replacement keeps the evidence shape on purpose: the schema check runs before the
    digest, so a shapeless replacement is refused as LEGACY_ARTIFACT - also correct, but it
    would not exercise the digest.
    """
    target = artifact(catalog_desk)
    document = json.loads(target.read_text(encoding="utf-8"))
    document["falsification_input"]["trades"] = []
    target.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    assert refusal(target).code is loader.Code.ARTIFACT_HASH_MISMATCH


def test_a_shapeless_replacement_is_still_refused(catalog_desk):
    target = artifact(catalog_desk)
    target.write_text('{"trades": {}}', encoding="utf-8")
    assert refusal(target).code is loader.Code.LEGACY_ARTIFACT


def test_a_trial_sealed_without_a_digest_is_refused_cleanly(catalog_desk):
    """A row from before the digest existed. Refused, never inferred."""
    source = artifact(catalog_desk)
    target = source.parent / "metrics-trial-0800.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    document["trial_seq"] = 800
    document["falsification_input"]["identity"]["trial_seq"] = 800
    target.write_text(experiments.dump_metrics(document), encoding="utf-8")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM trial WHERE seq = 1").fetchone()
        conn.execute(
            "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
            "cost_model_version,seed,started_at,finished_at,outcome,metrics_path)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("EXP-000009", 800, row["config_hash"], row["code_commit"],
             row["data_snapshot"], row["cost_model_version"], row["seed"],
             db.now(), db.now(), "ok", target.relative_to(catalog_desk).as_posix()))
    diagnostic = refusal(target)
    assert diagnostic.code is loader.Code.ARTIFACT_NOT_AUTHENTICATED
    assert "re-run" in diagnostic.message


# ══════════════════════════════════════════════════════════════════════════════
# absolute tolerances must not scale with magnitude
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("scale", [1.0, 1e6, 1e12])
def test_the_same_absolute_drift_is_refused_at_every_magnitude(scale):
    """`math.isclose` defaults rel_tol to 1e-9, which dominates a small abs_tol at scale:
    around 1e12 an error of 100.0 slips through a nominal 1e-6 check."""
    document, block, trades = _reconciliation_case(
        our=10.0 * scale, engine=4.0 * scale, diff=6.0 * scale + 100.0)
    with pytest.raises(loader.Unverifiable) as caught:
        loader._verify_reconciliation(document, block, trades,
                                      experiment_id="E", trial_seq=1, artifact="a")
    assert caught.value.diagnostic.code is loader.Code.RECONCILIATION_MISMATCH


@pytest.mark.parametrize("scale", [1.0, 1e6, 1e12])
def test_an_exact_reconciliation_verifies_at_every_magnitude(scale):
    document, block, trades = _reconciliation_case(
        our=10.0 * scale, engine=4.0 * scale, diff=6.0 * scale)
    loader._verify_reconciliation(document, block, trades,
                                  experiment_id="E", trial_seq=1, artifact="a")


# ══════════════════════════════════════════════════════════════════════════════
# 1. reconstruction honesty: the checkout must BE the recorded commit
# ══════════════════════════════════════════════════════════════════════════════
# Comparing individual files proved the reconstruction-relevant code matched. It did not
# prove the checkout was the recorded revision, so a successful verification could still be
# a run of today's tree that happened to agree on the files anyone had thought to list.
def test_a_checkout_at_a_different_commit_is_refused(golden_desk):
    _git(golden_desk, "commit", "-q", "--allow-empty", "-m", "move HEAD")
    diagnostic = refusal(artifact(golden_desk))
    assert diagnostic.code is loader.Code.CHECKOUT_COMMIT_MISMATCH
    assert diagnostic.expected and diagnostic.observed
    assert diagnostic.expected != diagnostic.observed


def test_the_diagnostic_names_both_commits(golden_desk):
    recorded = head_of(golden_desk)
    _git(golden_desk, "commit", "-q", "--allow-empty", "-m", "move HEAD")
    moved = head_of(golden_desk)
    diagnostic = refusal(artifact(golden_desk))
    assert diagnostic.expected.startswith(recorded[:12])
    assert diagnostic.observed.startswith(moved[:12])


def test_the_checkout_check_is_a_control_not_a_constant(golden_desk):
    """The control: at the recorded commit the same artifact verifies."""
    assert head_of(golden_desk) == json.loads(
        artifact(golden_desk).read_text(encoding="utf-8")
    )["falsification_input"]["identity"]["code_commit"]
    loader.verify(artifact(golden_desk))


def test_the_loader_never_creates_a_worktree_or_checks_out(catalog_desk, monkeypatch):
    """Executing historical code is a CIO design decision, not something to do quietly."""
    issued: list[list[str]] = []
    real_run = subprocess.run

    def recording(args, *rest, **kw):
        if args and args[0] == "git":
            issued.append(list(args))
        return real_run(args, *rest, **kw)

    monkeypatch.setattr(subprocess, "run", recording)
    loader.verify(artifact(catalog_desk))
    for command in issued:
        assert not ({"checkout", "worktree", "switch", "restore", "reset"} & set(command))


# ══════════════════════════════════════════════════════════════════════════════
# 2. what the loader returns, and what it refuses to vouch for
# ══════════════════════════════════════════════════════════════════════════════
def test_the_trusted_surface_is_declared(catalog_desk):
    verified = loader.verify(artifact(catalog_desk))
    assert set(loader.TRUSTED_FIELDS) == {f.name for f in dataclasses.fields(verified)}


def test_sections_that_are_authenticated_but_not_replayed_are_named(catalog_desk):
    verified = loader.verify(artifact(catalog_desk))
    assert verified.authenticated_only == ("detail", "regimes", "verdict")
    for section in verified.authenticated_only:
        assert not hasattr(verified, section)
        assert section not in verified.headline


def test_authenticated_only_sections_are_still_covered_by_the_digest(catalog_desk):
    """They are not replayed, but they are not un-vouched-for either: the artifact digest
    covers the whole file, so editing one is still refused."""
    target = artifact(catalog_desk)
    document = json.loads(target.read_text(encoding="utf-8"))
    document["detail"]["gross_pnl"] = 999999.0
    target.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    assert refusal(target).code is loader.Code.ARTIFACT_HASH_MISMATCH


@pytest.mark.parametrize("section", ["detail", "regimes", "verdict"])
def test_editing_an_unreplayed_section_after_sealing_is_refused(catalog_desk, section):
    target = artifact(catalog_desk)
    document = json.loads(target.read_text(encoding="utf-8"))
    document[section] = {"tampered": True}
    target.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    assert refusal(target).code is loader.Code.ARTIFACT_HASH_MISMATCH


# ── controls: each fail-closed check has a passing counterpart ──────────────────
def test_control_an_untampered_catalog_trial_verifies(catalog_desk):
    verified = loader.verify(artifact(catalog_desk))
    assert verified.series_source == "registered_dataset"


def test_control_an_untampered_golden_trial_verifies(golden_desk):
    verified = loader.verify(artifact(golden_desk))
    assert verified.series_source == "synthetic_fixture"


def test_control_verification_is_repeatable(catalog_desk):
    first = loader.verify(artifact(catalog_desk))
    second = loader.verify(artifact(catalog_desk))
    assert first.series_digest == second.series_digest
    assert first.headline == second.headline


def test_control_an_unmodified_dataset_file_passes(catalog_desk):
    """Counterpart to the file-hash refusals: touching nothing verifies."""
    path = catalog_desk / "data" / "catalog" / "klines" / "BTCUSDT.um" / "1m.parquet"
    before = db.sha256_file(path)
    loader.verify(artifact(catalog_desk))
    assert db.sha256_file(path) == before
