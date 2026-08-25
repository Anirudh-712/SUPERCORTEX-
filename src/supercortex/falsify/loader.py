"""Is this artifact the sealed trial, and does its content hold together?

The loader **authenticates a sealed artifact and partially replays it**. That wording is
deliberate and narrower than "independently reconstructed": Nautilus is not re-run, and
the trades are not regenerated. They are authenticated bytes whose internal consequences
are recomputed.

What is genuinely re-derived from primary sources: the bar series and the funding stream,
rebuilt from the registered Parquet or the approved generator under the recorded probe,
and hashed. What is replayed from the sealed trades: the cost model, the headline, the
four-number ladder, the per-trade returns and the reconciliation identity. What is
authenticated rather than recomputed: the trades themselves, via an immutable digest of
the exact bytes the registry sealed.

Two possible answers, never a third: a verified `TrialEvidence`, or a typed refusal naming
the invariant that failed. Nothing here infers missing provenance, migrates a legacy
artifact, checks out another commit, touches the working tree, or writes anything at all -
verification is read-only and consumes zero trials, because a check that cost a trial would
inflate the very trial count the Deflated Sharpe Ratio depends on.

The order of proof:

 1. **schema version** - a shape this code does not understand is refused, not interpreted
 2. **registry row** - the document does not get to describe itself; the row is authority
 3. **artifact digest** - the bytes must be the bytes that were sealed
 4. **commit + reconstruction boundary** - the code and pinned packages must be the
    recorded ones, or reconstruction here is not the producer's
 5. **config** - re-hashed from the recorded commit, and the effective contract it implies
    must be the contract the evidence claims
 6. **datasets** - every referenced file stream-hashed, and both files the engine opened
    bound to a registered dataset of the right kind
 7. **series and funding** - rebuilt under the recorded transform and re-digested
 8. **replay** - trades, cost model, headline, ladder, returns and reconciliation

Step 3 is what makes the rest mean anything. Without it every internal cross-check can be
edited into agreement by whoever holds the file, and a cost model the run never used
verifies perfectly.
"""
from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np

from supercortex.backtest import evidence
from supercortex.costs.model import CostModel, TaxRegime, Trade, apply
from supercortex.registry.db import PROJECT_ROOT

#: Diagnostics are read by operators and stored in reports. Values are truncated so a
#: corrupt artifact cannot turn a refusal into a wall of text, and config contents are
#: never echoed — only their hashes.
FIELD_LIMIT = 96

SUPPORTED_SCHEMA_VERSIONS = (2,)


class Code(StrEnum):
    LEGACY_ARTIFACT = "LEGACY_ARTIFACT"
    TRIAL_NOT_FOUND = "TRIAL_NOT_FOUND"
    TRIAL_NOT_SEALED = "TRIAL_NOT_SEALED"
    IDENTITY_ROW_MISMATCH = "IDENTITY_ROW_MISMATCH"
    METRICS_IDENTITY_MISMATCH = "METRICS_IDENTITY_MISMATCH"
    ARTIFACT_PATH_MISMATCH = "ARTIFACT_PATH_MISMATCH"
    CONFIG_PATH_UNSAFE = "CONFIG_PATH_UNSAFE"
    DATASET_PATH_UNSAFE = "DATASET_PATH_UNSAFE"
    DATASET_FILE_MISSING = "DATASET_FILE_MISSING"
    DATASET_FILE_MISMATCH = "DATASET_FILE_MISMATCH"
    DATASET_SOURCE_UNVERIFIED = "DATASET_SOURCE_UNVERIFIED"
    SERIES_FACT_MISMATCH = "SERIES_FACT_MISMATCH"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    ENVIRONMENT_DRIFT = "ENVIRONMENT_DRIFT"
    ARTIFACT_NOT_AUTHENTICATED = "ARTIFACT_NOT_AUTHENTICATED"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    FUNDING_DIGEST_MISMATCH = "FUNDING_DIGEST_MISMATCH"
    FUNDING_FACT_MISMATCH = "FUNDING_FACT_MISMATCH"
    CONFIG_CONTRACT_MISMATCH = "CONFIG_CONTRACT_MISMATCH"
    CHECKOUT_COMMIT_MISMATCH = "CHECKOUT_COMMIT_MISMATCH"
    RECONSTRUCTION_CODE_DRIFT = "RECONSTRUCTION_CODE_DRIFT"
    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
    MALFORMED_EVIDENCE = "MALFORMED_EVIDENCE"
    COMMIT_NOT_FOUND = "COMMIT_NOT_FOUND"
    CONFIG_NOT_FOUND = "CONFIG_NOT_FOUND"
    CONFIG_HASH_MISMATCH = "CONFIG_HASH_MISMATCH"
    DATASET_NOT_FOUND = "DATASET_NOT_FOUND"
    DATASET_SNAPSHOT_MISMATCH = "DATASET_SNAPSHOT_MISMATCH"
    SERIES_RECONSTRUCTION_FAILED = "SERIES_RECONSTRUCTION_FAILED"
    SERIES_DIGEST_MISMATCH = "SERIES_DIGEST_MISMATCH"
    HEADLINE_REPLAY_MISMATCH = "HEADLINE_REPLAY_MISMATCH"


#: A failure is retryable when the world could change to satisfy it without the evidence
#: changing — fetch the commit, restore the config, re-register the dataset. A *mismatch*
#: is never retryable: the evidence and the world genuinely disagree, and one of them is
#: wrong. Repeating the check cannot resolve that.
#: Checking out the recorded commit would clear this one - but that is a change to the
#: operator's working tree, so it is the operator's decision, not the loader's.
RETRYABLE = frozenset({Code.COMMIT_NOT_FOUND, Code.CONFIG_NOT_FOUND,
                       Code.DATASET_NOT_FOUND, Code.DATASET_FILE_MISSING,
                       Code.CHECKOUT_COMMIT_MISMATCH})

#: Sections of the sealed document that the artifact digest authenticates but that this
#: reader does NOT replay. They are covered by registry-bound bytes, so editing one is
#: still refused - but their values are not recomputed, so they are not returned as
#: verified evidence either.
AUTHENTICATED_ONLY = ("detail", "regimes", "verdict")

#: The implementations reconstruction depends on. If any of them differs from the recorded
#: commit, the code rebuilding the series is not the code that produced it, and the commit
#: check would otherwise be decorative - see `_verify_reconstruction_code`.
RECONSTRUCTION_SOURCES = (
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

#: Third-party behaviour is part of reconstruction: Nautilus decides what a Bar is, Polars
#: decides how a frame resamples, NumPy decides how a float rounds. A different version is
#: a different reconstruction, so the installed versions are checked against the pins in
#: the committed lockfile rather than assumed.
RECONSTRUCTION_PACKAGES = ("nautilus-trader", "polars", "numpy", "scipy")

#: Exactly the probe fields `ProbeSpec.as_dict` seals, and their exact types. Nothing is
#: defaulted: the contract says the whole transform is recorded, so a missing field means
#: the evidence is malformed, not that the probe was a baseline.
PROBE_FIELDS: dict[str, type] = {
    "label": str, "bars_shift": int, "bars_scramble": bool,
    "funding_shift": int, "funding_scramble": bool,
}
RECONCILIATION_FIELDS = ("engine_realized_pnl", "our_gross_pnl", "reconciliation_diff")
RETURNS_FIELDS = ("per_trade", "sharpe_per_trade")
IDENTITY_FIELDS = ("experiment_id", "trial_seq", "config_hash", "config_path",
                   "code_commit", "data_snapshot", "dataset_ids", "seed")

#: Source-specific price-series shapes. A registered series has no generator and a
#: synthetic one has no datasets; carrying the other's fields means the document is not
#: the shape the producer writes.
SERIES_COMMON = ("source", "encoding", "series_sha256", "n_points",
                 "first_ts_event_ns", "last_ts_event_ns", "transform")
SERIES_REGISTERED = (*SERIES_COMMON, "dataset_ids", "data_snapshot", "symbol", "market")
SERIES_SYNTHETIC = (*SERIES_COMMON, "generator", "generator_parameters")

#: `_metrics` writes exactly these into the top-level run_config block.
TOP_LEVEL_RUN_CONFIG = ("starting_balance", "leverage", "seed")

#: The producer rounds every reconciliation figure to six decimals, so the comparison is
#: tight enough to see a real disagreement and loose enough to absorb that rounding.
#:
#: Every comparison that means "these serialised numbers are the same" passes
#: `rel_tol=0.0`. `math.isclose` defaults `rel_tol` to 1e-9, which DOMINATES a small
#: absolute tolerance at scale: around 1e12 an error of 100.0 slips through a nominal
#: 1e-6 check. Nonzero relative tolerance survives only where it is part of the
#: mathematical contract - the per-trade return comparison, where the quantities are
#: ratios rather than serialised money.
RECONCILIATION_TOLERANCE = 1e-6

#: Every `Trade` field, in full. `entry_taker`, `exit_taker` and `funding_pnl` have
#: dataclass defaults, so requiring only the other six let the reader invent execution and
#: funding the trial never had.
TRADE_FIELDS = ("entry_ts_ms", "exit_ts_ms", "direction", "qty", "entry_price",
                "exit_price", "entry_taker", "exit_taker", "funding_pnl")
_TRADE_TYPES: dict[str, str] = {
    "entry_ts_ms": "integer", "exit_ts_ms": "integer", "direction": "integer",
    "qty": "number", "entry_price": "number", "exit_price": "number",
    "entry_taker": "boolean", "exit_taker": "boolean", "funding_pnl": "number",
}

#: Exactly what `_metrics` writes into `run_config`, and what each must agree with.
RUN_CONFIG_FIELDS: dict[str, str] = {
    "starting_balance": "number", "leverage": "number", "seed": "integer",
    "latency_ms": "integer", "interval": "text", "symbol": "text", "market": "text",
}

#: Exactly the sections `price_series.transform` carries under schema version 1.
TRANSFORM_FIELDS = ("interval", "window", "probe")
WINDOW_FIELDS = ("start_ms", "end_ms")

#: Every field of `metrics.trades`, all of which the loader replays. Anything else in that
#: object is unverified, so its presence is refused rather than ignored.
HEADLINE_FIELDS = ("n_trades", "n_wins", "win_rate", "turnover", "profit_factor_gross",
                   "profit_factor_gross_state", "profit_factor_net",
                   "profit_factor_net_state", "break_even_profit_factor",
                   "clears_break_even", "sharpe_per_trade")


def _number(value: object, where: str, **ident: object) -> float:
    """A finite JSON number, or a typed refusal. `float("x")` raising ValueError deep in a
    comparison is not a diagnostic anybody can act on."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      f"{where} is {type(value).__name__}, expected a number", **ident)
    number = float(value)
    if not math.isfinite(number):
        raise _refuse(Code.MALFORMED_EVIDENCE, f"{where} is {number}, which is not finite",
                      **ident)
    return number


def _typed(value: object, kind: str, where: str, **ident: object) -> Any:
    """One scalar, of exactly the declared kind. `True` is never an integer here."""
    if kind == "boolean":
        if not isinstance(value, bool):
            raise _refuse(Code.MALFORMED_EVIDENCE,
                          f"{where} is {type(value).__name__}, expected a bool", **ident)
        return value
    if kind == "text":
        if not isinstance(value, str) or not value:
            raise _refuse(Code.MALFORMED_EVIDENCE,
                          f"{where} is {type(value).__name__}, expected a non-empty string",
                          **ident)
        return value
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _refuse(Code.MALFORMED_EVIDENCE,
                          f"{where} is {type(value).__name__}, expected an integer",
                          **ident)
        return value
    return _number(value, where, **ident)


def _exact_keys(mapping: dict[str, Any], expected: tuple[str, ...] | dict[str, Any],
                where: str, **ident: object) -> None:
    """The sealed contract names every field. A missing one is not a default and an extra
    one is not decoration - either way the document is not the shape that was written."""
    wanted = set(expected)
    missing = sorted(wanted - set(mapping))
    if missing:
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      f"{where} is missing {', '.join(missing)}", **ident)
    unknown = sorted(set(mapping) - wanted)
    if unknown:
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      f"{where} carries unknown field(s) {', '.join(unknown)}; schema "
                      f"version {SUPPORTED_SCHEMA_VERSIONS[-1]} does not define them",
                      **ident)


def _clip(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= FIELD_LIMIT else text[: FIELD_LIMIT - 1] + "…"


def _clip_path(value: object) -> str | None:
    """Paths are clipped from the LEFT. The filename is the half worth keeping."""
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= FIELD_LIMIT else "…" + text[-(FIELD_LIMIT - 1):]


@dataclass(frozen=True)
class Diagnostic:
    """Why a trial cannot enter falsification. Machine-readable first, prose second."""

    code: Code
    message: str
    experiment_id: str | None = None
    trial_seq: int | None = None
    expected: str | None = None
    observed: str | None = None
    artifact: str | None = None

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": str(self.code), "message": self.message,
            "experiment_id": self.experiment_id, "trial_seq": self.trial_seq,
            "expected": self.expected, "observed": self.observed,
            "artifact": self.artifact, "retryable": self.retryable,
        }


class Unverifiable(RuntimeError):
    """The trial cannot enter falsification. Carries the reason, never a partial result."""

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(f"{diagnostic.code}: {diagnostic.message}")
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class TrialEvidence:
    """Everything falsification needs, and nothing it does not.

    Deliberately absent, because they are not sealed and must be read at verdict time:
    `trials_consumed` (the registry's count now, not the count when this trial ran) and
    `sharpe_variance` (measured across the experiment's trials as they stand today).
    Also absent: sweep returns and variant-family membership, which no producer writes yet.
    """

    experiment_id: str
    trial_seq: int
    code_commit: str
    config_path: str
    config_hash: str
    dataset_ids: tuple[str, ...]
    data_snapshot: str
    series_digest: str
    series_source: str
    n_points: int
    trades: tuple[Trade, ...]
    returns: tuple[float, ...]
    sharpe_per_trade: float
    model: CostModel
    starting_equity: float
    prices: np.ndarray
    timestamps: np.ndarray
    headline: dict[str, Any] = field(default_factory=dict)
    #: Named, not returned. See `AUTHENTICATED_ONLY`.
    authenticated_only: tuple[str, ...] = AUTHENTICATED_ONLY


# ── plumbing ────────────────────────────────────────────────────────────────────
def _refuse(code: Code, message: str, **fields: object) -> Unverifiable:
    return Unverifiable(Diagnostic(
        code=code, message=message,
        experiment_id=fields.get("experiment_id"),  # type: ignore[arg-type]
        trial_seq=fields.get("trial_seq"),          # type: ignore[arg-type]
        expected=_clip(fields.get("expected")),
        observed=_clip(fields.get("observed")),
        artifact=_clip_path(fields.get("artifact")),
    ))


def _require(mapping: object, key: str, kind: type | tuple[type, ...],
             where: str, **ident: object) -> Any:
    if not isinstance(mapping, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      f"{where} is {type(mapping).__name__}, expected an object", **ident)
    if key not in mapping:
        raise _refuse(Code.MALFORMED_EVIDENCE, f"{where}.{key} is missing", **ident)
    value = mapping[key]
    if isinstance(value, bool) and kind is not bool:
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      f"{where}.{key} is a bool, expected {getattr(kind, '__name__', kind)}",
                      **ident)
    if not isinstance(value, kind):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      f"{where}.{key} is {type(value).__name__}, expected "
                      f"{getattr(kind, '__name__', kind)}", **ident)
    return value


# ── 1. schema version ───────────────────────────────────────────────────────────
def _evidence_block(document: dict[str, Any], artifact: str) -> dict[str, Any]:
    block = document.get("falsification_input")
    if block is None:
        raise _refuse(
            Code.LEGACY_ARTIFACT,
            "this trial predates the falsification evidence schema and carries no "
            "falsification_input block, so it cannot satisfy the provenance contract. "
            "It is not retrofitted and its provenance is not inferred: re-run the "
            "experiment to produce a trial that can be falsified.",
            experiment_id=document.get("experiment_id"),
            trial_seq=document.get("trial_seq"), artifact=artifact,
        )
    if not isinstance(block, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      f"falsification_input is {type(block).__name__}, expected an object",
                      artifact=artifact)

    version = block.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "falsification_input.schema_version is missing or not an integer",
                      observed=version, artifact=artifact)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise _refuse(
            Code.UNSUPPORTED_SCHEMA_VERSION,
            f"evidence schema version {version} is not supported by this reader; "
            f"a version this code does not understand cannot be checked, so it is refused "
            f"rather than interpreted",
            expected=list(SUPPORTED_SCHEMA_VERSIONS), observed=version, artifact=artifact,
        )
    return block


def _safe_relative(candidate: object, root: Path) -> Path | None:
    """A repository-relative path that genuinely stays inside `root`.

    Paths in evidence and in the registry are untrusted input. An absolute path, a `..`
    segment, or a symlink pointing outside would let a sealed document steer the reader
    at `.env`, a key file, or anything else on the machine.
    """
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    path = Path(candidate)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        return None
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        return None
    return resolved


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Read-only git. Nothing here writes to the index, the tree or the object store."""
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, check=False)


# ── 2. commit in checkout ───────────────────────────────────────────────────────
def _verify_commit(commit: str, **ident: object) -> None:
    """The recorded commit must exist here. HEAD is never substituted for it."""
    if not isinstance(commit, str) or len(commit) < 7:
        raise _refuse(Code.MALFORMED_EVIDENCE, "identity.code_commit is not a git object id",
                      observed=commit, **ident)
    probe = _git(PROJECT_ROOT, "cat-file", "-e", f"{commit}^{{commit}}")
    if probe.returncode != 0:
        raise _refuse(
            Code.COMMIT_NOT_FOUND,
            "the commit this trial was run against is not present in this checkout, so "
            "the code that produced it cannot be identified. Fetch it; do not verify "
            "against whatever happens to be checked out.",
            observed=commit, **ident,
        )


def _installed_version(package: str) -> str | None:
    """The version actually importable in this process."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _locked_versions(lockfile: bytes) -> dict[str, str]:
    """Package pins from a uv.lock, parsed without adding a TOML dependency."""
    pins: dict[str, str] = {}
    name: str | None = None
    for raw in lockfile.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line == "[[package]]":
            name = None
        elif line.startswith("name = "):
            name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("version = ") and name:
            pins[name] = line.split("=", 1)[1].strip().strip('"')
            name = None
    return pins


def _verify_checkout(commit: str, **ident: object) -> None:
    """The checkout must BE the recorded revision, not merely agree with it file by file.

    Reconstruction runs the code in this working tree. Comparing a list of files proved
    the reconstruction-relevant ones matched, which is weaker than it sounds: the list is
    a human judgement about what matters, and anything it misses runs unchecked. Requiring
    HEAD to equal the recorded commit makes the guarantee say what it means - a successful
    reconstruction used the recorded code.

    The loader does not check out the recorded commit, create a worktree, or execute
    historical code. Doing any of those would alter the operator's environment, and the
    design for it belongs in front of the CIO before it belongs in this function.
    """
    head = _git(PROJECT_ROOT, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise _refuse(Code.CHECKOUT_COMMIT_MISMATCH,
                      "this checkout has no HEAD, so it cannot be shown to be the "
                      "revision the trial was run against", **ident)
    current = head.stdout.decode("utf-8", errors="replace").strip()
    if current != commit:
        raise _refuse(
            Code.CHECKOUT_COMMIT_MISMATCH,
            "this checkout is not the revision this trial was run against, so a "
            "reconstruction here would run today's code and report it as the trial's. "
            "Check out the recorded commit and verify again; the loader will not move "
            "your working tree for you.",
            **{**ident, "expected": commit, "observed": current},
        )


def _verify_reconstruction_code(commit: str, **ident: object) -> None:
    """The implementations AND the environment that rebuild the series must be the
    recorded ones.

    Reconstruction runs the code in this checkout, not the code at `commit`: checking out
    another revision would alter the operator's working tree, which verification must
    never do. Proving the commit merely *exists* would therefore be decorative - the
    reader could rebuild with different logic and call the result verified.

    So the narrower claim is made load-bearing. Every file reconstruction depends on is
    compared byte for byte against its content at the recorded commit, and every
    third-party package whose behaviour reconstruction depends on is compared against the
    pins in that commit's lockfile. If they all agree, the reconstruction here IS the
    producer's. If any disagrees, that is refused rather than reconciled.
    """
    drifted: list[str] = []
    lockfile: bytes | None = None
    for relative in RECONSTRUCTION_SOURCES:
        recorded = _git(PROJECT_ROOT, "show", f"{commit}:{relative}")
        current = PROJECT_ROOT / relative
        if recorded.returncode != 0 or not current.exists():
            drifted.append(Path(relative).name)
            continue
        if recorded.stdout != current.read_bytes():
            drifted.append(Path(relative).name)
        elif relative == "uv.lock":
            lockfile = recorded.stdout
    if drifted:
        raise _refuse(
            Code.RECONSTRUCTION_CODE_DRIFT,
            f"the implementation that would rebuild this trial differs from the one "
            f"recorded with it ({', '.join(drifted)}), so a reconstruction here would not "
            f"be the producer's. Verify from the recorded commit.",
            **{**ident, "observed": ", ".join(drifted)},
        )

    if lockfile is None:
        raise _refuse(Code.RECONSTRUCTION_CODE_DRIFT,
                      "the lockfile recorded with this trial could not be read, so the "
                      "environment reconstruction would run in cannot be established",
                      **ident)
    pins = _locked_versions(lockfile)
    for package in RECONSTRUCTION_PACKAGES:
        pinned, installed = pins.get(package), _installed_version(package)
        if pinned is None:
            raise _refuse(Code.ENVIRONMENT_DRIFT,
                          f"{package} is not pinned in the lockfile recorded with this "
                          f"trial, so its behaviour cannot be established", **ident)
        if installed != pinned:
            raise _refuse(
                Code.ENVIRONMENT_DRIFT,
                f"the installed {package} is not the version this trial was locked "
                f"against; its behaviour is part of the reconstruction",
                **{**ident, "expected": pinned, "observed": installed},
            )


# ── 3. config drift ─────────────────────────────────────────────────────────────
def _verify_config(config_path: object, expected_hash: str, commit: str,
                   **ident: object) -> dict[str, Any]:
    """Re-hash the config **as it exists at the recorded commit**, the producer's way.

    Working-tree equality is deliberately NOT required. The trial ran against the
    committed config, and that is what `open_trial` hashed; editing the file afterwards
    to prepare the next trial does not invalidate this one. Reading the working tree at
    all would also reintroduce the traversal the path check exists to close.
    """
    from supercortex.registry import db

    # The experiment id is already in `ident`; taking it as a second parameter is what
    # collided with the splat three times over.
    experiment_id = ident["experiment_id"]
    safe = _safe_relative(config_path, PROJECT_ROOT)
    if safe is None:
        raise _refuse(Code.CONFIG_PATH_UNSAFE,
                      "the recorded config path is not a safe project-relative path",
                      **{**ident, "observed": config_path})

    with db.connect() as conn:
        row = conn.execute("SELECT slug FROM experiment WHERE id = ?",
                           (experiment_id,)).fetchone()
    if row is None:
        raise _refuse(Code.TRIAL_NOT_FOUND,
                      f"experiment {experiment_id} is not in the registry", **ident)
    expected_path = f"02-experiments/{experiment_id}-{row['slug']}/config.yaml"
    relative = safe.relative_to(PROJECT_ROOT.resolve()).as_posix()
    if relative != expected_path:
        raise _refuse(
            Code.CONFIG_PATH_UNSAFE,
            "the recorded config path is not this experiment's registered config location",
            **{**ident, "expected": expected_path, "observed": relative},
        )

    shown = _git(PROJECT_ROOT, "show", f"{commit}:{relative}")
    if shown.returncode != 0:
        raise _refuse(Code.CONFIG_NOT_FOUND,
                      "the config is not present at the commit this trial was run "
                      "against, so its parameters cannot be confirmed",
                      **{**ident, "artifact": relative})

    text = shown.stdout.decode("utf-8")
    observed = db.sha256_text(text)
    if observed != expected_hash:
        raise _refuse(
            Code.CONFIG_HASH_MISMATCH,
            "the config stored at the recorded commit does not hash to the value "
            "registered with this trial. Only hashes are reported; contents are not "
            "echoed.",
            **{**ident, "artifact": relative,
               "expected": expected_hash, "observed": observed},
        )
    try:
        import yaml

        committed = yaml.safe_load(text) or {}
    except Exception as exc:
        raise _refuse(Code.CONFIG_CONTRACT_MISMATCH,
                      f"the committed config could not be parsed: {exc}",
                      **{**ident, "artifact": relative}) from exc
    if not isinstance(committed, dict):
        raise _refuse(Code.CONFIG_CONTRACT_MISMATCH,
                      "the committed config is not a mapping", **ident)
    return committed


def _verify_config_contract(committed: dict[str, Any], model: CostModel,
                            run_values: dict[str, Any], row: Any,
                            **ident: object) -> None:
    """The effective contract in the evidence must be the one the committed config implies.

    Authenticating the config *bytes* and then never reading them left the sealed cost
    model and run values unbound: a maker fee of 999.0 verified against a config that says
    2.0. The producer's own config-to-contract helpers are used, so this derives the same
    values rather than restating its defaults.

    Strategy reference and parameters are bound transitively: they live in the same
    committed bytes whose hash is verified above, and they select the code at the verified
    commit.
    """
    import dataclasses

    from supercortex.backtest import trial as producer

    try:
        expected_model = producer.model_from_config(committed)
        expected_run = producer.run_values_from_config(committed)
    except Exception as exc:
        raise _refuse(Code.CONFIG_CONTRACT_MISMATCH,
                      f"the committed config does not describe a runnable trial: {exc}",
                      **ident) from exc

    for spec in dataclasses.fields(expected_model):
        expected = getattr(expected_model, spec.name)
        sealed = getattr(model, spec.name)
        if spec.name == "regime":
            expected, sealed = str(expected.value), str(sealed.value)
        if isinstance(expected, float) and isinstance(sealed, int | float):
            same = math.isclose(float(sealed), expected, rel_tol=0.0, abs_tol=1e-12)
        else:
            same = sealed == expected
        if not same:
            raise _refuse(
                Code.CONFIG_CONTRACT_MISMATCH,
                f"cost_model.{spec.name} is not what the committed config implies",
                **{**ident, "expected": expected, "observed": sealed},
            )
    if model.version != row["cost_model_version"]:
        raise _refuse(Code.CONFIG_CONTRACT_MISMATCH,
                      "cost_model.version does not match the registry row",
                      **{**ident, "expected": row["cost_model_version"],
                         "observed": model.version})

    for name, expected in expected_run.items():
        sealed = run_values[name]
        if isinstance(expected, float) and isinstance(sealed, int | float):
            same = math.isclose(float(sealed), expected, rel_tol=0.0, abs_tol=1e-9)
        else:
            same = sealed == expected
        if not same:
            raise _refuse(
                Code.CONFIG_CONTRACT_MISMATCH,
                f"run_config.{name} is not what the committed config implies",
                **{**ident, "expected": expected, "observed": sealed},
            )


# ── 4. dataset snapshot ─────────────────────────────────────────────────────────
def _verify_datasets(dataset_ids: list[str], expected_snapshot: str,
                     series: dict[str, Any], **ident: object) -> None:
    """Verify the dataset FILES, not merely the rows that describe them.

    Re-hashing `id:stored_sha256` proves only that the database still contains its own old
    values; it says nothing about the Parquet on disk. So every referenced dataset is
    resolved to a real file inside the project and stream-hashed. That is a byte-stream
    read, not a three-million-row reconstruction, and it is the only check that sees a
    funding file change - the price digest covers bars alone.
    """
    from supercortex.registry import db

    source = series.get("source")
    if not dataset_ids:
        if source != evidence.SOURCE_SYNTHETIC:
            raise _refuse(Code.DATASET_SOURCE_UNVERIFIED,
                          "the evidence names no datasets but the series claims to come "
                          "from registered data",
                          **{**ident, "observed": source})
        if expected_snapshot != "no-datasets":
            raise _refuse(Code.DATASET_SNAPSHOT_MISMATCH,
                          "the evidence names no datasets but carries a data snapshot",
                          **{**ident, "expected": "no-datasets",
                             "observed": expected_snapshot})
        return

    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT id, sha256, parquet_path, kind FROM dataset WHERE id IN "
            f"({','.join('?' * len(dataset_ids))}) ORDER BY id",
            list(dataset_ids),
        ).fetchall()

    missing = sorted(set(dataset_ids) - {row["id"] for row in rows})
    if missing:
        raise _refuse(Code.DATASET_NOT_FOUND,
                      "a dataset this trial read is no longer registered, so its identity "
                      "cannot be confirmed",
                      **{**ident, "observed": ", ".join(missing)})

    snapshot = db.sha256_text("|".join(f"{r['id']}:{r['sha256']}" for r in rows))
    if snapshot != expected_snapshot:
        raise _refuse(
            Code.DATASET_SNAPSHOT_MISMATCH,
            "a dataset resolves by id but no longer carries the hash recorded at sealing "
            "time. Same name, different data.",
            **{**ident, "expected": expected_snapshot, "observed": snapshot},
        )

    registered: dict[str, str] = {}
    for row in rows:
        safe = _safe_relative(row["parquet_path"], PROJECT_ROOT)
        if safe is None:
            raise _refuse(Code.DATASET_PATH_UNSAFE,
                          f"dataset {row['id']} has no safe project-relative parquet_path",
                          **{**ident, "observed": row["id"]})
        if not safe.exists():
            raise _refuse(Code.DATASET_FILE_MISSING,
                          f"the Parquet for dataset {row['id']} is not on disk, so its "
                          f"bytes cannot be verified",
                          **{**ident, "observed": row["id"],
                             "artifact": row["parquet_path"]})
        observed = db.sha256_file(safe)
        if observed != row["sha256"]:
            raise _refuse(
                Code.DATASET_FILE_MISMATCH,
                f"the Parquet for dataset {row['id']} does not hash to its registered "
                f"sha256. The file changed after registration.",
                **{**ident, "observed": row["id"], "expected": row["sha256"],
                   "artifact": row["parquet_path"]},
            )
        if safe.as_posix() in registered:
            raise _refuse(Code.DATASET_SOURCE_UNVERIFIED,
                          "two registered datasets point at the same file",
                          **{**ident, "observed": row["parquet_path"]})
        registered[safe.as_posix()] = str(row["kind"])

    _verify_series_source(series, registered, **ident)


def _verify_series_source(series: dict[str, Any], registered: dict[str, str],
                          **ident: object) -> None:
    """Bind BOTH files the engine actually opened, and require the declared set to be
    exactly those two.

    `runner.run` reads hard-coded catalog paths; the registry only *describes* datasets.
    Checking klines alone let a byte-identical funding decoy be registered while the
    engine read an unregistered file - the hash and the snapshot would both pass.
    """
    from supercortex.backtest import feed

    symbol, market = series.get("symbol"), series.get("market")
    required = {
        "klines": (feed.CATALOG / "klines" / f"{symbol}.{market}" / "1m.parquet").resolve(),
        "funding": (feed.CATALOG / "funding_rate" / f"{symbol}.{market}"
                    / "funding.parquet").resolve(),
    }
    for kind, path in required.items():
        actual_kind = registered.get(path.as_posix())
        if actual_kind is None:
            raise _refuse(
                Code.DATASET_SOURCE_UNVERIFIED,
                f"the {kind} file this trial loaded is not one of the datasets it "
                f"registered, so the bytes that were read were never verified",
                **{**ident, "observed": f"{symbol}.{market} {kind}"},
            )
        if actual_kind != kind:
            raise _refuse(
                Code.DATASET_SOURCE_UNVERIFIED,
                f"the {kind} file this trial loaded is registered as {actual_kind!r}",
                **{**ident, "expected": kind, "observed": actual_kind},
            )
    extra = sorted(set(registered) - {p.as_posix() for p in required.values()})
    if extra:
        raise _refuse(
            Code.DATASET_SOURCE_UNVERIFIED,
            "the evidence declares datasets the run did not read; it claims to name the "
            "datasets the trial read",
            **{**ident, "observed": ", ".join(Path(p).name for p in extra)},
        )


# ── 5. series digest ────────────────────────────────────────────────────────────
def _checked_transform(series: dict[str, Any], **ident: object) -> dict[str, Any]:
    """The transform exactly as sealed. Nothing defaulted, nothing coerced.

    Slice A records the whole transform, so a missing or string-valued field means the
    evidence is malformed - not that the probe was a baseline. Inferring `bars_shift = 0`
    from an absent key would let a look-ahead probe verify as its own control.
    """
    transform = series.get("transform")
    if not isinstance(transform, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "price_series.transform is missing or not an object", **ident)
    _exact_keys(transform, TRANSFORM_FIELDS, "price_series.transform", **ident)
    interval = transform.get("interval")
    if not isinstance(interval, str) or not interval:
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "price_series.transform.interval is missing or not a string", **ident)

    window = transform.get("window")
    if not isinstance(window, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "price_series.transform.window is missing or not an object", **ident)
    _exact_keys(window, WINDOW_FIELDS, "price_series.transform.window", **ident)
    for bound in WINDOW_FIELDS:
        value = window[bound]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise _refuse(Code.MALFORMED_EVIDENCE,
                          f"price_series.transform.window.{bound} is "
                          f"{type(value).__name__}, expected an integer or null", **ident)

    probe = transform.get("probe")
    if not isinstance(probe, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "price_series.transform.probe is missing or not an object", **ident)
    _exact_keys(probe, PROBE_FIELDS, "price_series.transform.probe", **ident)
    for name, kind in PROBE_FIELDS.items():
        value = probe[name]
        if kind is bool:
            if not isinstance(value, bool):
                raise _refuse(Code.MALFORMED_EVIDENCE,
                              f"price_series.transform.probe.{name} is "
                              f"{type(value).__name__}, expected a bool", **ident)
        elif isinstance(value, bool) or not isinstance(value, kind):
            raise _refuse(Code.MALFORMED_EVIDENCE,
                          f"price_series.transform.probe.{name} is "
                          f"{type(value).__name__}, expected {kind.__name__}", **ident)
    return {"interval": interval, "window": window, "probe": probe}


def _source_frame(series: dict[str, Any], transform: dict[str, Any],
                  **ident: object) -> Any:
    """The pre-transform frame, from the registered catalog or the approved generator."""
    from supercortex.backtest import feed, golden

    source = series.get("source")
    if source == evidence.SOURCE_REGISTERED:
        return feed.load_bars_frame(series["symbol"], series["market"],
                                    transform["interval"])
    if source != evidence.SOURCE_SYNTHETIC:
        raise _refuse(Code.SERIES_RECONSTRUCTION_FAILED,
                      f"series source {source!r} is not a source this reader can rebuild",
                      **{**ident, "observed": source})

    generator = series.get("generator")
    if generator not in evidence.APPROVED_GENERATORS:
        raise _refuse(Code.SERIES_RECONSTRUCTION_FAILED,
                      f"generator {generator!r} is not CIO-approved; synthetic evidence is "
                      f"restricted to {list(evidence.APPROVED_GENERATORS)}",
                      **{**ident, "observed": generator})
    parameters = series.get("generator_parameters")
    if not isinstance(parameters, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "price_series.generator_parameters is missing or not an object",
                      **ident)
    _exact_keys(parameters, evidence.SYNTHETIC_REQUIRED,
                "price_series.generator_parameters", **ident)
    for name in ("n_bars", "period", "epoch_start_ms", "bar_ms"):
        if _typed(parameters[name], "integer",
                  f"generator_parameters.{name}", **ident) <= 0:
            raise _refuse(Code.MALFORMED_EVIDENCE,
                          f"generator_parameters.{name} must be positive", **ident)
    for name in ("low", "high"):
        _number(parameters[name], f"generator_parameters.{name}", **ident)
    if parameters["high"] <= parameters["low"]:
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "generator_parameters.high must exceed low", **ident)
    # The epoch convention is part of the contract, and this generator takes it from module
    # constants: if they disagree, the frame this reader builds is not the sealed one.
    if (parameters["epoch_start_ms"], parameters["bar_ms"]) != (golden.EPOCH_START,
                                                                golden.HOUR_MS):
        raise _refuse(
            Code.SERIES_RECONSTRUCTION_FAILED,
            "the recorded epoch convention does not match the approved generator's",
            **{**ident, "expected": f"{golden.EPOCH_START}/{golden.HOUR_MS}",
               "observed": f"{parameters['epoch_start_ms']}/{parameters['bar_ms']}"},
        )
    return golden.sawtooth(n_bars=parameters["n_bars"], low=parameters["low"],
                           high=parameters["high"], period=parameters["period"])


def _rebuild_series(block: dict[str, Any], **ident: object) -> list[tuple[int, float]]:
    """Rebuild the exact bars the engine was handed, by calling what the producer called.

    Re-implementing `to_bars` here would let the reader's encoding drift from the writer's
    and produce a mismatch that means nothing. The data comes from disk or the approved
    generator, the digest comes from the artifact, and only those two are compared.
    """
    import polars as pl

    from supercortex.backtest import feed, runner

    series = block["price_series"]
    transform = _checked_transform(series, **ident)
    window, probe = transform["window"], transform["probe"]
    try:
        frame = _source_frame(series, transform, **ident)
        if window["start_ms"] is not None:
            frame = frame.filter(pl.col("ts_ms") >= window["start_ms"])
        if window["end_ms"] is not None:
            frame = frame.filter(pl.col("ts_ms") <= window["end_ms"])
        instrument = runner.fee_free_instrument()
        bars = feed.to_bars(
            frame, runner.bar_type_for(instrument, transform["interval"]),
            instrument.price_precision, instrument.size_precision,
            transform["interval"],
            shift_bars=probe["bars_shift"], scramble=probe["bars_scramble"],
            seed=block["run_config"]["seed"],
        )
    except Unverifiable:
        raise
    except Exception as exc:
        raise _refuse(Code.SERIES_RECONSTRUCTION_FAILED,
                      f"the series could not be rebuilt: {type(exc).__name__}: {exc}",
                      **ident) from exc
    return [(int(bar.ts_event), float(bar.close)) for bar in bars]


def _verify_funding(block: dict[str, Any], **ident: object) -> None:
    """Rebuild the funding stream the engine received and re-derive its digest.

    The price digest covers bars alone, so `funding_shift` and `funding_scramble` were
    validated as fields and then never used: a probe could claim any funding transform and
    nothing contradicted it.
    """
    from supercortex.backtest import feed, runner

    facts = block["funding_series"]
    _exact_keys(facts, evidence.FUNDING_FACT_FIELDS, "funding_series", **ident)
    series = block["price_series"]
    transform = series["transform"]
    probe, window = transform["probe"], transform["window"]

    if series["source"] == evidence.SOURCE_SYNTHETIC:
        if facts["source"] != evidence.FUNDING_NONE:
            raise _refuse(Code.FUNDING_FACT_MISMATCH,
                          "a synthetic fixture supplies no funding stream, but the "
                          "evidence claims one was delivered",
                          **{**ident, "expected": evidence.FUNDING_NONE,
                             "observed": facts["source"]})
        if facts["n_events"] or facts["funding_sha256"] is not None:
            raise _refuse(Code.FUNDING_FACT_MISMATCH,
                          "a synthetic fixture's funding evidence is not empty", **ident)
        return

    try:
        instrument = runner.fee_free_instrument()
        events = feed.load_funding(
            series["symbol"], series["market"], instrument.id,
            shift_intervals=probe["funding_shift"],
            scramble=probe["funding_scramble"],
            seed=block["run_config"]["seed"],
        )
        if window["start_ms"] is not None:
            events = [e for e in events
                      if e.ts_event >= window["start_ms"] * 1_000_000]
        if window["end_ms"] is not None:
            events = [e for e in events if e.ts_event <= window["end_ms"] * 1_000_000]
    except Unverifiable:
        raise
    except Exception as exc:
        raise _refuse(Code.SERIES_RECONSTRUCTION_FAILED,
                      f"the funding stream could not be rebuilt: {type(exc).__name__}: "
                      f"{exc}", **ident) from exc

    rebuilt = evidence.funding_facts(
        [(int(event.ts_event), float(event.rate)) for event in events])
    for fact in ("source", "encoding", "n_events", "first_ts_event_ns",
                 "last_ts_event_ns"):
        if facts[fact] != rebuilt[fact]:
            raise _refuse(Code.FUNDING_FACT_MISMATCH,
                          f"the sealed funding_series.{fact} does not match the "
                          f"reconstruction",
                          **{**ident, "expected": facts[fact], "observed": rebuilt[fact]})
    if facts["funding_sha256"] != rebuilt["funding_sha256"]:
        raise _refuse(
            Code.FUNDING_DIGEST_MISMATCH,
            "the funding stream rebuilt under the recorded probe does not hash to the "
            "digest sealed with this trial",
            **{**ident, "expected": facts["funding_sha256"],
               "observed": rebuilt["funding_sha256"]},
        )


def _verify_series(block: dict[str, Any], **ident: object) -> list[tuple[int, float]]:
    """Re-derive the digest, and check every sealed fact against the reconstruction."""
    pairs = _rebuild_series(block, **ident)
    series = block["price_series"]
    rebuilt = evidence.series_facts(pairs)

    for fact in ("encoding", "n_points", "first_ts_event_ns", "last_ts_event_ns"):
        if fact not in series:
            raise _refuse(Code.MALFORMED_EVIDENCE,
                          f"price_series.{fact} is missing", **ident)
        if series[fact] != rebuilt[fact]:
            raise _refuse(
                Code.SERIES_FACT_MISMATCH,
                f"the sealed price_series.{fact} does not match the reconstruction",
                **{**ident, "expected": series[fact], "observed": rebuilt[fact]},
            )

    if rebuilt["series_sha256"] != series.get("series_sha256"):
        raise _refuse(
            Code.SERIES_DIGEST_MISMATCH,
            "the series rebuilt from the recorded source does not hash to the digest "
            "sealed with this trial. The data the engine saw is not the data available "
            "now - row count and timestamps are not a substitute for this check.",
            **{**ident, "expected": series.get("series_sha256"),
               "observed": rebuilt["series_sha256"]},
        )
    return pairs


# ── 6. headline replay ──────────────────────────────────────────────────────────
def _rebuild_trades(block: dict[str, Any], **ident: object) -> tuple[Trade, ...]:
    """Every trade, with every field present. Nothing falls back to a dataclass default."""
    rows = block["trades"]
    trades: list[Trade] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise _refuse(Code.MALFORMED_EVIDENCE,
                          f"trades[{index}] is {type(row).__name__}, expected an object",
                          **ident)
        _exact_keys(row, TRADE_FIELDS, f"trades[{index}]", **ident)
        values = {name: _typed(row[name], _TRADE_TYPES[name], f"trades[{index}].{name}",
                               **ident)
                  for name in TRADE_FIELDS}
        try:
            trades.append(Trade(**values))
        except (TypeError, ValueError) as exc:
            raise _refuse(Code.MALFORMED_EVIDENCE,
                          f"trades[{index}] is not a valid Trade: {exc}", **ident) from exc
    return tuple(trades)


def _rebuild_model(block: dict[str, Any], **ident: object) -> CostModel:
    """The exact effective model. Filling an absent field from today's defaults would
    make a sealed cost model mean whatever the current code happens to think."""
    import dataclasses

    fields = block["cost_model"]
    declared = {f.name: f for f in dataclasses.fields(CostModel)}
    _exact_keys(fields, tuple(declared), "cost_model", **ident)

    payload: dict[str, Any] = {}
    for name, spec in declared.items():
        value = fields[name]
        if name == "regime":
            if not isinstance(value, str):
                raise _refuse(Code.MALFORMED_EVIDENCE,
                              f"cost_model.regime is {type(value).__name__}, expected a "
                              f"string", **ident)
            try:
                payload[name] = TaxRegime(value)
            except ValueError as exc:
                raise _refuse(Code.MALFORMED_EVIDENCE,
                              f"cost_model.regime {value!r} is not a known tax regime",
                              **ident) from exc
        elif name == "notes":
            if not isinstance(value, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
            ):
                raise _refuse(Code.MALFORMED_EVIDENCE,
                              "cost_model.notes is not a mapping of strings", **ident)
            payload[name] = dict(value)
        elif spec.type == "str":
            payload[name] = _typed(value, "text", f"cost_model.{name}", **ident)
        elif spec.type == "bool":
            payload[name] = _typed(value, "boolean", f"cost_model.{name}", **ident)
        else:
            payload[name] = _number(value, f"cost_model.{name}", **ident)

    try:
        model = CostModel(**payload)
    except (TypeError, ValueError) as exc:
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      f"cost_model cannot be reconstructed: {exc}", **ident) from exc
    if model.version != fields["version"]:
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "cost_model fields do not reproduce the recorded version",
                      **{**ident, "expected": fields["version"], "observed": model.version})
    return model


def _verify_run_config(document: dict[str, Any], block: dict[str, Any],
                       identity: dict[str, Any], **ident: object) -> dict[str, Any]:
    """The run contract, in full and agreeing with everything else that records it.

    A seed that disagrees with the registry row, or an interval that disagrees with the
    transform the series was built under, means two parts of the same document describe
    two different runs. Only one of them can be the trial.
    """
    run_config = block["run_config"]
    _exact_keys(run_config, RUN_CONFIG_FIELDS, "run_config", **ident)
    values = {name: _typed(run_config[name], kind, f"run_config.{name}", **ident)
              for name, kind in RUN_CONFIG_FIELDS.items()}
    if values["starting_balance"] <= 0:
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      f"run_config.starting_balance is {values['starting_balance']}; "
                      f"returns on it are undefined", **ident)

    if values["seed"] != identity["seed"]:
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "run_config.seed disagrees with the trial identity's seed",
                      **{**ident, "expected": identity["seed"],
                         "observed": values["seed"]})
    series = block["price_series"]
    transform = series.get("transform") or {}
    if values["interval"] != transform.get("interval"):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "run_config.interval disagrees with the transform the series was "
                      "built under",
                      **{**ident, "expected": transform.get("interval"),
                         "observed": values["interval"]})
    if series.get("source") == evidence.SOURCE_REGISTERED:
        for name in ("symbol", "market"):
            if values[name] != series.get(name):
                raise _refuse(Code.MALFORMED_EVIDENCE,
                              f"run_config.{name} disagrees with the registered price "
                              f"series",
                              **{**ident, "expected": series.get(name),
                                 "observed": values[name]})

    top_level = document.get("run_config")
    if not isinstance(top_level, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "metrics.run_config is missing or not an object", **ident)
    _exact_keys(top_level, TOP_LEVEL_RUN_CONFIG, "metrics.run_config", **ident)
    for name, value in top_level.items():
        if value != values[name]:
            raise _refuse(Code.MALFORMED_EVIDENCE,
                          f"metrics.run_config.{name} disagrees with the evidence",
                          **{**ident, "expected": values[name], "observed": value})
    return values


def _verify_reconciliation(document: dict[str, Any], block: dict[str, Any],
                           trades: tuple[Trade, ...], **ident: object) -> None:
    """The engine's realised PnL and ours must still agree, and by the recorded amount.

    Checking only that the three keys exist let every value change freely - which is the
    opposite of a reconciliation. The identity is small and exact: our gross is the sum of
    the sealed trades, and the difference is what the producer wrote down.
    """
    reconciliation = block.get("reconciliation")
    if not isinstance(reconciliation, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "falsification_input.reconciliation is missing or not an object",
                      **ident)
    values = {}
    for name in RECONCILIATION_FIELDS:
        if name not in reconciliation:
            raise _refuse(Code.MALFORMED_EVIDENCE,
                          f"falsification_input.reconciliation.{name} is missing", **ident)
        values[name] = _number(reconciliation[name], f"reconciliation.{name}", **ident)

    ours = sum(trade.gross_pnl for trade in trades)
    if not math.isclose(values["our_gross_pnl"], ours, rel_tol=0.0,
                        abs_tol=RECONCILIATION_TOLERANCE):
        raise _refuse(Code.RECONCILIATION_MISMATCH,
                      "the sealed reconciliation our_gross_pnl is not the sum of the "
                      "sealed trades' gross PnL",
                      **{**ident, "expected": round(ours, 6),
                         "observed": values["our_gross_pnl"]})
    # The producer writes `round(our_gross - engine_pnl, 6)`. The reverse is a different
    # quantity that agrees only when the two sides match - which every fixture does, which
    # is exactly why a reversed sign went unnoticed.
    difference = values["our_gross_pnl"] - values["engine_realized_pnl"]
    if not math.isclose(difference, values["reconciliation_diff"], rel_tol=0.0,
                        abs_tol=RECONCILIATION_TOLERANCE):
        raise _refuse(Code.RECONCILIATION_MISMATCH,
                      "the sealed reconciliation_diff is not our_gross_pnl minus "
                      "engine_realized_pnl",
                      **{**ident, "expected": round(difference, 6),
                         "observed": values["reconciliation_diff"]})

    engine = document.get("engine")
    if not isinstance(engine, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "metrics.engine is missing or not an object", **ident)
    for name, value in values.items():
        recorded = _number(engine.get(name), f"metrics.engine.{name}", **ident)
        if not math.isclose(recorded, value, rel_tol=0.0,
                            abs_tol=RECONCILIATION_TOLERANCE):
            raise _refuse(Code.RECONCILIATION_MISMATCH,
                          f"the evidence reconciliation.{name} disagrees with the sealed "
                          f"metrics.engine diagnostics",
                          **{**ident, "expected": recorded, "observed": value})


def _verify_headline(document: dict[str, Any], block: dict[str, Any],
                     trades: tuple[Trade, ...], model: CostModel,
                     **ident: object) -> dict[str, Any]:
    """Replay the cost model over the sealed trades and compare EVERY sealed claim.

    Nothing is conditional. A missing ladder is a refusal, not a skipped check, and no
    field is recomputed and then allowed to disagree with the value that was sealed.
    """
    from supercortex.costs.model import profit_factor_json

    headline = document.get("trades")
    if not isinstance(headline, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE, "metrics.trades is missing or not an object",
                      **ident)
    _verify_reconciliation(document, block, trades, **ident)

    replay = apply(list(trades), model)
    replayed_headline: dict[str, Any] = {
        "n_trades": replay.n_trades,
        "n_wins": replay.n_wins,
        "win_rate": round(replay.win_rate, 6),
        "turnover": round(replay.turnover, 2),
        "profit_factor_gross": profit_factor_json(replay.profit_factor_gross,
                                                  replay.profit_factor_gross_state),
        "profit_factor_gross_state": str(replay.profit_factor_gross_state),
        "profit_factor_net": profit_factor_json(replay.profit_factor_net,
                                                replay.profit_factor_net_state),
        "profit_factor_net_state": str(replay.profit_factor_net_state),
        "break_even_profit_factor": round(replay.break_even_profit_factor, 6),
        "clears_break_even": replay.clears_break_even,
    }
    if set(headline) != set(HEADLINE_FIELDS):
        raise _refuse(
            Code.HEADLINE_REPLAY_MISMATCH,
            "metrics.trades does not carry exactly the fields this reader replays, so "
            "part of it would be returned unverified",
            **{**ident, "expected": ", ".join(sorted(HEADLINE_FIELDS)),
               "observed": ", ".join(sorted(headline))},
        )
    for name, expected in replayed_headline.items():
        sealed = headline[name]
        if isinstance(expected, bool) or expected is None or isinstance(expected, str):
            same = sealed == expected
        else:
            same = (not isinstance(sealed, bool) and isinstance(sealed, int | float)
                    and math.isclose(float(sealed), float(expected), rel_tol=0.0,
                                     abs_tol=1e-5))
        if not same:
            raise _refuse(Code.HEADLINE_REPLAY_MISMATCH,
                          f"replaying the sealed trades does not reproduce the sealed "
                          f"{name}",
                          **{**ident, "expected": expected, "observed": sealed})

    ladder = document.get("four_numbers")
    if not isinstance(ladder, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "metrics.four_numbers is missing or not an object; the four-number "
                      "ladder is mandatory evidence, not an optional decoration", **ident)
    replayed = {label: round(value, 2) for label, value in replay.four_numbers()}
    if set(ladder) != set(replayed):
        raise _refuse(Code.HEADLINE_REPLAY_MISMATCH,
                      "the sealed four-number ladder does not have exactly the rungs the "
                      "replay produces",
                      **{**ident, "expected": ", ".join(sorted(replayed)),
                         "observed": ", ".join(sorted(ladder))})
    for label, value in replayed.items():
        sealed_rung = _number(ladder[label], f"four_numbers[{label!r}]", **ident)
        if not math.isclose(value, sealed_rung, rel_tol=0.0, abs_tol=0.011):
            raise _refuse(Code.HEADLINE_REPLAY_MISMATCH,
                          f"replaying the sealed trades does not reproduce the {label!r} "
                          f"rung of the four-number ladder",
                          **{**ident, "expected": sealed_rung, "observed": value})

    ordered = document.get("four_numbers_ordered")
    if not isinstance(ordered, list) or any(
        not isinstance(rung, list | tuple) or len(rung) != 2 for rung in ordered
    ):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "metrics.four_numbers_ordered is missing or not a list of pairs",
                      **ident)
    expected_order = [[label, round(value, 2)] for label, value in replay.four_numbers()]
    if [[str(a), b] for a, b in ordered] != expected_order:
        raise _refuse(Code.HEADLINE_REPLAY_MISMATCH,
                      "metrics.four_numbers_ordered does not match the replayed ladder; "
                      "it is reported separately, so it is verified separately",
                      **{**ident, "expected": ", ".join(str(label) for label, _ in expected_order),
                         "observed": ", ".join(str(r[0]) for r in ordered)})

    returns_block = block.get("returns")
    if not isinstance(returns_block, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE, "falsification_input.returns is missing",
                      **ident)
    sealed_returns = returns_block.get("per_trade")
    if not isinstance(sealed_returns, list) or any(
        isinstance(v, bool) or not isinstance(v, int | float) for v in sealed_returns
    ):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "returns.per_trade is missing or is not a list of numbers", **ident)
    equity = block["run_config"]["starting_balance"]
    replayed_returns = evidence.per_trade_returns(list(trades), model, float(equity))
    if len(replayed_returns) != len(sealed_returns) or any(
        not math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-15)
        for a, b in zip(replayed_returns, sealed_returns, strict=True)
    ):
        raise _refuse(Code.HEADLINE_REPLAY_MISMATCH,
                      "the sealed per-trade returns are not the returns these trades and "
                      "this cost model produce",
                      **{**ident, "expected": f"{len(replayed_returns)} values",
                         "observed": f"{len(sealed_returns)} values"})

    replayed_sharpe = evidence.sharpe_per_trade(replayed_returns)
    for where, sealed in (("returns.sharpe_per_trade",
                           returns_block.get("sharpe_per_trade")),
                          # read directly by `sharpe_variance_from_registry`, so verifying
                          # only the evidence block would leave the DSR input tamperable
                          ("trades.sharpe_per_trade", headline.get("sharpe_per_trade"))):
        value = _number(sealed, where, **ident)
        if not math.isclose(replayed_sharpe, value, rel_tol=1e-12, abs_tol=1e-15):
            raise _refuse(Code.HEADLINE_REPLAY_MISMATCH,
                          f"the sealed {where} is not the value these returns produce",
                          **{**ident, "expected": replayed_sharpe, "observed": value})
    # What is RETURNED is the replay, never the document: an unverified field can then
    # never reach a caller wearing the label "verified".
    replayed_headline["sharpe_per_trade"] = replayed_sharpe
    return replayed_headline


# ── the loader ──────────────────────────────────────────────────────────────────
def _registry_row(claimed_id: object, claimed_seq: object, **ident: object) -> Any:
    """The trial row this evidence claims to be. Authoritative, and the reason a copied or
    consistently edited artifact cannot verify: the document does not get to describe
    itself."""
    from supercortex.registry import db

    if not isinstance(claimed_id, str) or not claimed_id:
        raise _refuse(Code.MALFORMED_EVIDENCE, "identity.experiment_id is not a string",
                      **ident)
    if isinstance(claimed_seq, bool) or not isinstance(claimed_seq, int):
        raise _refuse(Code.MALFORMED_EVIDENCE, "identity.trial_seq is not an integer",
                      **ident)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT config_hash, code_commit, data_snapshot, seed, outcome, metrics_path,"
            " artifact_sha256, cost_model_version"
            " FROM trial WHERE experiment_id = ? AND seq = ?",
            (claimed_id, claimed_seq),
        ).fetchone()
    if row is None:
        raise _refuse(
            Code.TRIAL_NOT_FOUND,
            "no registered trial matches the experiment and sequence this evidence claims, "
            "so the document describes a trial the registry never recorded",
            **{**ident, "observed": f"{claimed_id} #{claimed_seq}"},
        )
    if row["outcome"] != "ok":
        raise _refuse(
            Code.TRIAL_NOT_SEALED,
            "the registered trial is not sealed `ok`, so its result was never accepted "
            "and it cannot enter falsification",
            **{**ident, "expected": "ok", "observed": row["outcome"]},
        )
    return row


def _verify_identity_against_row(identity: dict[str, Any], row: Any,
                                 **ident: object) -> None:
    for field_name, column in (("config_hash", "config_hash"),
                               ("code_commit", "code_commit"),
                               ("data_snapshot", "data_snapshot"),
                               ("seed", "seed")):
        sealed, recorded = identity.get(field_name), row[column]
        if field_name == "seed":
            match = isinstance(sealed, int) and not isinstance(sealed, bool) and \
                sealed == recorded
        else:
            match = sealed == recorded
        if not match:
            raise _refuse(
                Code.IDENTITY_ROW_MISMATCH,
                f"the evidence's {field_name} does not match the registry row for this "
                f"trial; the row is authoritative",
                **{**ident, "expected": recorded, "observed": sealed},
            )


def _authenticate_artifact(metrics_path: Path, row: Any, **ident: object) -> None:
    """The bytes on disk must be the bytes that were sealed.

    Without this the loader authenticates nothing: every internal cross-check can be
    edited into agreement by whoever holds the file, and a cost model the run never used
    verifies perfectly. The digest is the one immutable statement about this artifact that
    the forger does not control, because the sealed row cannot be updated.
    """
    from supercortex.registry import db

    recorded = row["artifact_sha256"]
    if not isinstance(recorded, str) or len(recorded) != 64:
        raise _refuse(
            Code.ARTIFACT_NOT_AUTHENTICATED,
            "the registry holds no artifact digest for this trial, so the document at the "
            "recorded path cannot be shown to be the one that was sealed. A trial sealed "
            "before the digest existed is not retrofitted and its provenance is not "
            "inferred: re-run the experiment.",
            **{**ident, "observed": recorded},
        )
    observed = db.sha256_file(metrics_path)
    if observed != recorded:
        raise _refuse(
            Code.ARTIFACT_HASH_MISMATCH,
            "the artifact at the registered path does not hash to the digest recorded when "
            "the trial was sealed; its bytes changed after sealing",
            **{**ident, "expected": recorded, "observed": observed},
        )


def _verify_artifact_path(metrics_path: Path, row: Any, **ident: object) -> None:
    recorded = row["metrics_path"]
    safe = _safe_relative(recorded, PROJECT_ROOT)
    if safe is None:
        raise _refuse(Code.ARTIFACT_PATH_MISMATCH,
                      "the registry row records no safe artifact path for this trial",
                      **{**ident, "observed": recorded})
    if metrics_path.resolve() != safe:
        raise _refuse(
            Code.ARTIFACT_PATH_MISMATCH,
            "this file is not the artifact the registry recorded for the trial it claims "
            "to be; a copy is not the sealed result",
            **{**ident, "expected": recorded,
               "observed": metrics_path.name},
        )


def verify(metrics_path: Path) -> TrialEvidence:
    """Verify a sealed trial, or refuse it with a diagnostic. Reads only."""
    path = Path(metrics_path)
    artifact = str(path)
    def _reject_constant(name: str) -> float:
        raise ValueError(f"{name} is not valid JSON")

    try:
        document = json.loads(path.read_text(encoding="utf-8"),
                              parse_constant=_reject_constant)
    except OSError as exc:
        raise _refuse(Code.MALFORMED_EVIDENCE, f"artifact cannot be read: {exc}",
                      artifact=artifact) from exc
    except ValueError as exc:
        raise _refuse(Code.MALFORMED_EVIDENCE, f"artifact is not valid JSON: {exc}",
                      artifact=artifact) from exc
    if not isinstance(document, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      f"artifact is a {type(document).__name__}, expected an object",
                      artifact=artifact)

    block = _evidence_block(document, artifact)
    identity = block.get("identity")
    if not isinstance(identity, dict):
        raise _refuse(Code.MALFORMED_EVIDENCE, "falsification_input.identity is missing",
                      artifact=artifact)

    ident = {
        "experiment_id": identity.get("experiment_id"),
        "trial_seq": identity.get("trial_seq"),
        "artifact": artifact,
    }
    for name in ("experiment_id", "trial_seq", "config_hash", "config_path",
                 "code_commit", "data_snapshot", "seed"):
        if name not in identity:
            raise _refuse(Code.MALFORMED_EVIDENCE, f"identity.{name} is missing", **ident)
    for name in ("price_series", "run_config", "cost_model", "returns",
                 "reconciliation", "funding_series"):
        if not isinstance(block.get(name), dict):
            raise _refuse(Code.MALFORMED_EVIDENCE,
                          f"falsification_input.{name} is missing or not an object",
                          **ident)
    if not isinstance(block.get("trades"), list):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "falsification_input.trades is missing or not a list", **ident)

    dataset_ids = identity.get("dataset_ids", [])
    if not isinstance(dataset_ids, list) or any(
        not isinstance(value, str) or not value for value in dataset_ids
    ):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "identity.dataset_ids is not a list of dataset ids", **ident)
    if len(set(dataset_ids)) != len(dataset_ids):
        raise _refuse(Code.MALFORMED_EVIDENCE,
                      "identity.dataset_ids names the same dataset more than once",
                      **ident)

    # The document does not get to describe itself: bind it to the registered trial first.
    row = _registry_row(identity["experiment_id"], identity["trial_seq"], **ident)
    _verify_identity_against_row(identity, row, **ident)
    for name in ("experiment_id", "trial_seq"):
        if name not in document:
            raise _refuse(Code.MALFORMED_EVIDENCE,
                          f"the artifact has no top-level {name} to check the evidence "
                          f"against", **ident)
        if document[name] != identity[name]:
            raise _refuse(
                Code.METRICS_IDENTITY_MISMATCH,
                f"the artifact's top-level {name} disagrees with the evidence it carries",
                **{**ident, "expected": identity[name], "observed": document[name]},
            )
    _verify_artifact_path(path, row, **ident)
    _authenticate_artifact(path, row, **ident)

    _exact_keys(identity, IDENTITY_FIELDS, "falsification_input.identity", **ident)
    _exact_keys(block["reconciliation"], RECONCILIATION_FIELDS,
                "falsification_input.reconciliation", **ident)
    _exact_keys(block["returns"], RETURNS_FIELDS, "falsification_input.returns", **ident)
    series_shape = (SERIES_REGISTERED
                    if block["price_series"].get("source") == evidence.SOURCE_REGISTERED
                    else SERIES_SYNTHETIC)
    _exact_keys(block["price_series"], series_shape, "falsification_input.price_series",
                **ident)

    _verify_commit(identity["code_commit"], **ident)
    _verify_checkout(identity["code_commit"], **ident)
    _verify_reconstruction_code(identity["code_commit"], **ident)
    committed = _verify_config(identity["config_path"], identity["config_hash"],
                               identity["code_commit"], **ident)
    _verify_datasets(list(dataset_ids), identity["data_snapshot"],
                     block["price_series"], **ident)
    # The run contract first: reconstruction reads the seed from it, so a malformed
    # run_config must be named as such rather than surfacing as a rebuild failure.
    run_values = _verify_run_config(document, block, identity, **ident)
    starting_balance = float(run_values["starting_balance"])
    pairs = _verify_series(block, **ident)
    _verify_funding(block, **ident)

    trades = _rebuild_trades(block, **ident)
    model = _rebuild_model(block, **ident)
    # The committed config is authoritative for the effective contract, so this runs once
    # both sides exist as objects rather than as raw JSON.
    _verify_config_contract(committed, model, run_values, row, **ident)
    headline = _verify_headline(document, block, trades, model, **ident)

    returns = evidence.per_trade_returns(list(trades), model, starting_balance)
    return TrialEvidence(
        experiment_id=identity["experiment_id"],
        trial_seq=int(identity["trial_seq"]),
        code_commit=identity["code_commit"],
        config_path=identity["config_path"],
        config_hash=identity["config_hash"],
        dataset_ids=tuple(dataset_ids),
        data_snapshot=identity["data_snapshot"],
        series_digest=block["price_series"]["series_sha256"],
        series_source=block["price_series"]["source"],
        n_points=len(pairs),
        trades=trades,
        returns=tuple(returns),
        sharpe_per_trade=evidence.sharpe_per_trade(returns),
        model=model,
        starting_equity=starting_balance,
        prices=np.array([close for _stamp, close in pairs], dtype=float),
        timestamps=np.array([stamp // 1_000_000 for stamp, _close in pairs], dtype=float),
        headline=dict(headline),
    )


TRUSTED_FIELDS = (
    "experiment_id", "trial_seq", "code_commit", "config_path", "config_hash",
    "dataset_ids", "data_snapshot", "series_digest", "series_source", "n_points",
    "trades", "returns", "sharpe_per_trade", "model", "starting_equity", "prices",
    "timestamps", "headline", "authenticated_only",
)
