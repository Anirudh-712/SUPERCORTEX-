"""What a sealed trial must record about itself, so it can be falsified later.

A sealed `metrics-trial-NNNN.json` carried aggregates and nothing else: `n_trades`,
`win_rate`, three profit factors. Not one individual round trip, no prices, no timestamps.
The falsification battery needs the opposite — the actual decisions and the actual series —
and none of it can be recovered from a summary. `n_trades` does not yield trades, and a
cost-model *version name* does not yield a cost model.

So the run writes it down while it still holds the objects. Everything here is *read* —
from a runtime object, from the open registry row, from the experiment's config, or from
the path the config was loaded from — and never defaulted, inferred or reconstructed. The
sources are not interchangeable and `IDENTITY_FIELDS` says which is which. A value that
cannot be read is an `EvidenceError`, never a guess. A trial
whose evidence cannot be built is sealed `error` — it still counts against the trial budget,
because it still consumed a look at the data, but it is never presented as a result.

The series digest is the load-bearing piece. It covers the bars **after** shift and
scramble — the exact objects handed to the engine — so a probe and its baseline are
provably different inputs, and data that changed underneath a trial cannot pose as the data
that produced it.
"""
from __future__ import annotations

import dataclasses
import math
import struct
from hashlib import sha256
from typing import Any

from supercortex.costs.model import CostModel, Trade

#: Bump when the shape of the block changes. A reader that does not recognise a version
#: must refuse the artifact rather than interpret unknown fields.
SCHEMA_VERSION = 2

#: The canonical series encoding, named inside every block that uses it. A digest whose
#: encoding is not recorded is a number nobody can reproduce.
SERIES_ENCODING = "scx-series-1"

#: The funding stream gets its own encoding tag. Sharing one with the price series would
#: let two different streams that happen to carry the same pairs hash identically.
FUNDING_ENCODING = "scx-funding-1"

#: Whether a run was given a funding stream at all. A synthetic fixture is not missing
#: funding by accident - it deliberately has none, and says so.
FUNDING_SUPPLIED = "supplied"
FUNDING_NONE = "none"

SOURCE_REGISTERED = "registered_dataset"
SOURCE_SYNTHETIC = "synthetic_fixture"
SOURCES = (SOURCE_REGISTERED, SOURCE_SYNTHETIC)

#: The fields that make a trial reproducible, and where each one legitimately comes from.
#: They are listed together because the block needs all of them; they are *sourced*
#: differently, and conflating that would misdescribe what a later reader is verifying.
#:
#:   * registry row — `experiment_id`, `trial_seq`, `config_hash`, `code_commit`,
#:     `data_snapshot`, `seed`. Authoritative: the row is what a reader checks against.
#:   * path-derived — `config_path`. The row stores the config's *hash*, never its
#:     location, so the path comes from where the config was actually loaded.
#:   * config/caller-sourced — `dataset_ids` (see `build`). The row stores only the
#:     `data_snapshot` hash computed over them, so the id list cannot be recovered from
#:     the registry and is recorded here to make that snapshot checkable.
#:
#: See `backtest.trial.trial_identity`, which assembles them and owns `ROW_SOURCED`.
IDENTITY_FIELDS = (
    "experiment_id", "trial_seq", "config_hash", "config_path",
    "code_commit", "data_snapshot", "seed",
)

#: A synthetic series is only recomputable if the generator and *every* effective
#: parameter are stated — including the period and the epoch convention, which are the two
#: a reader would otherwise have to assume.
SYNTHETIC_REQUIRED = ("n_bars", "low", "high", "period", "epoch_start_ms", "bar_ms")

#: Synthetic evidence exists to validate the engine, not to open a side door around law 6.
#: Exactly one generator is authorised — the CIO-approved golden fixture. Adding another is
#: a CIO decision recorded in a DEC note, not an argument a caller supplies.
APPROVED_GENERATORS = ("supercortex.backtest.golden:sawtooth",)


class EvidenceError(RuntimeError):
    """Evidence could not be built or encoded. The trial is not a result."""


def _finite(value: object, where: str) -> float:
    """Reject anything JSON would emit but nothing downstream could compare."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EvidenceError(f"{where} is {type(value).__name__}, expected a number")
    number = float(value)
    if not math.isfinite(number):
        raise EvidenceError(f"{where} is {number}, which is not finite")
    return number


def _assert_finite(payload: object, where: str) -> None:
    """Walk a nested structure and refuse any non-finite float inside it."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            _assert_finite(value, f"{where}.{key}")
    elif isinstance(payload, list | tuple):
        for index, value in enumerate(payload):
            _assert_finite(value, f"{where}[{index}]")
    elif isinstance(payload, float) and not math.isfinite(payload):
        raise EvidenceError(f"{where} is {payload}, which is not finite")


# ── the canonical series encoding ───────────────────────────────────────────────
def series_digest(pairs: object) -> str:
    """SHA-256 over the `(ts_event_ns, close)` pairs actually delivered to the engine.

    The encoding is fixed and explicit, because a digest is only evidence if a second
    reader can reproduce it byte for byte:

        sha256( b"scx-series-1\\n" + concat( struct.pack(">qd", ts_event_ns, close) ) )

    `>` pins big-endian, so the value does not depend on the host's native byte order.
    `q` is a signed 64-bit stamp and `d` is IEEE-754 binary64, so nothing passes through
    `repr`, a locale-sensitive format, a rounding step, or Polars' display layer. Order is
    the delivery order and is part of the digest: bars arrive in sequence, and two
    orderings are two different inputs.
    """
    if not isinstance(pairs, list | tuple):
        raise EvidenceError(f"series is {type(pairs).__name__}, expected a sequence")
    if not pairs:
        raise EvidenceError(
            "series is empty; a run with no bars delivered is not a series, it is a run "
            "that had no data"
        )

    body = bytearray()
    for index, pair in enumerate(pairs):
        if not isinstance(pair, list | tuple) or len(pair) != 2:
            raise EvidenceError(f"series[{index}] is not a (ts_event, close) pair")
        stamp, close = pair
        if isinstance(stamp, bool) or not isinstance(stamp, int):
            raise EvidenceError(
                f"series[{index}] timestamp is {type(stamp).__name__}, expected an "
                f"integer nanosecond stamp"
            )
        value = _finite(close, f"series[{index}] close")
        try:
            body += struct.pack(">qd", stamp, value)
        except struct.error as exc:
            raise EvidenceError(f"series[{index}] cannot be encoded: {exc}") from exc

    return sha256(SERIES_ENCODING.encode("utf-8") + b"\n" + bytes(body)).hexdigest()


def _digest(pairs: object, encoding: str, label: str) -> str:
    """SHA-256 over `(ts_event_ns, value)` pairs under a named encoding.

    Identical byte-level rules to the price digest - big-endian `>qd`, no `repr`, no
    locale, delivery order significant - with a distinct prefix so a funding stream and a
    price series can never hash alike.
    """
    if not isinstance(pairs, list | tuple):
        raise EvidenceError(f"{label} is {type(pairs).__name__}, expected a sequence")
    if not pairs:
        raise EvidenceError(f"{label} is empty")

    body = bytearray()
    for index, pair in enumerate(pairs):
        if not isinstance(pair, list | tuple) or len(pair) != 2:
            raise EvidenceError(f"{label}[{index}] is not a (ts_event, value) pair")
        stamp, value = pair
        if isinstance(stamp, bool) or not isinstance(stamp, int):
            raise EvidenceError(
                f"{label}[{index}] timestamp is {type(stamp).__name__}, expected an "
                f"integer nanosecond stamp"
            )
        number = _finite(value, f"{label}[{index}] value")
        try:
            body += struct.pack(">qd", stamp, number)
        except struct.error as exc:
            raise EvidenceError(f"{label}[{index}] cannot be encoded: {exc}") from exc
    return sha256(encoding.encode("utf-8") + b"\n" + bytes(body)).hexdigest()


def funding_digest(events: object) -> str:
    """Digest over the funding events actually delivered, after shift and scramble."""
    return _digest(events, FUNDING_ENCODING, "funding")


def funding_facts(events: list[tuple[int, float]]) -> dict[str, Any]:
    """What the run did about funding, including deliberately doing nothing."""
    if not events:
        return {"source": FUNDING_NONE, "encoding": FUNDING_ENCODING,
                "funding_sha256": None, "n_events": 0,
                "first_ts_event_ns": None, "last_ts_event_ns": None}
    return {
        "source": FUNDING_SUPPLIED,
        "encoding": FUNDING_ENCODING,
        "funding_sha256": funding_digest(events),
        "n_events": len(events),
        "first_ts_event_ns": int(events[0][0]),
        "last_ts_event_ns": int(events[-1][0]),
    }


FUNDING_FACT_FIELDS = ("source", "encoding", "funding_sha256", "n_events",
                       "first_ts_event_ns", "last_ts_event_ns")


def series_facts(pairs: list[tuple[int, float]]) -> dict[str, Any]:
    """The compact, serialisable summary of a delivered series.

    The runner holds the bars; the trial holds the provenance. Passing the whole series
    between them would put millions of floats through a JSON document that already has a
    digest covering exactly those floats.
    """
    digest = series_digest(pairs)
    return {
        "encoding": SERIES_ENCODING,
        "series_sha256": digest,
        "n_points": len(pairs),
        "first_ts_event_ns": int(pairs[0][0]),
        "last_ts_event_ns": int(pairs[-1][0]),
    }


FACT_FIELDS = ("encoding", "series_sha256", "n_points",
               "first_ts_event_ns", "last_ts_event_ns")


# ── returns ─────────────────────────────────────────────────────────────────────
def per_trade_returns(
    trades: list[Trade], model: CostModel, starting_equity: float
) -> list[float]:
    """Per-trade return on starting equity, ordered by exit.

    Deliberately identical to `falsify.orchestrator.Context.trade_returns()`. Two
    definitions of "the returns" would make the cross-trial Sharpe variance compare
    quantities that are not the same quantity.
    """
    equity = _finite(starting_equity, "starting equity")
    if equity <= 0:
        raise EvidenceError(f"starting equity is {equity}; returns are undefined")
    ordered = sorted(trades, key=lambda t: t.exit_ts_ms)
    return [model.net_of_costs(t) / equity for t in ordered]


def sharpe_per_trade(returns: list[float]) -> float:
    """The per-trade Sharpe, written into the summary as well as the evidence block.

    Both copies are replayed by the loader. Nothing reads it from a sibling artifact: an
    unauthenticated cross-trial variance is exactly the input the falsification battery
    must not have.
    """
    import numpy as np

    from supercortex.falsify import statistics as st

    return st.sharpe_ratio(np.array(returns, dtype=float))


# ── the price-series block ──────────────────────────────────────────────────────
def price_series_evidence(
    *,
    facts: dict[str, Any],
    source: str,
    interval: str,
    start_ms: int | None,
    end_ms: int | None,
    probe: dict[str, Any],
    dataset_ids: list[str] | None = None,
    data_snapshot: str | None = None,
    symbol: str | None = None,
    market: str | None = None,
    generator: str | None = None,
    generator_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Where the series came from, what was done to it, and what it hashed to.

    The reference and the transform are recorded; the series itself is not. Copying the
    market-data lake into git would make every trial artifact enormous and would duplicate
    data the `dataset` table already verifies. What is recorded is enough to re-derive the
    series and prove the re-derivation matches.
    """
    if source not in SOURCES:
        raise EvidenceError(
            f"price series source {source!r} is not one of {list(SOURCES)}"
        )
    missing_facts = [name for name in FACT_FIELDS if name not in facts]
    if missing_facts:
        raise EvidenceError(
            f"series facts are incomplete, missing: {', '.join(missing_facts)}"
        )

    block: dict[str, Any] = {
        "source": source,
        **{name: facts[name] for name in FACT_FIELDS},
        "transform": {
            "interval": interval,
            "window": {"start_ms": start_ms, "end_ms": end_ms},
            "probe": dict(probe),
        },
    }

    if source == SOURCE_REGISTERED:
        if not dataset_ids:
            raise EvidenceError(
                "a registered_dataset series must name the dataset ids it read; "
                "'no-datasets' is legal for a synthetic fixture and never for market data"
            )
        if not data_snapshot:
            raise EvidenceError("a registered_dataset series must carry its data_snapshot")
        block["dataset_ids"] = list(dataset_ids)
        block["data_snapshot"] = data_snapshot
        block["symbol"] = symbol
        block["market"] = market
        return block

    _check_generator(generator)
    parameters = dict(generator_parameters or {})
    missing = [name for name in SYNTHETIC_REQUIRED if name not in parameters]
    if missing:
        raise EvidenceError(
            f"synthetic_fixture generator parameters are incomplete, missing: "
            f"{', '.join(missing)}. Every effective parameter must be explicit, including "
            f"the period and the epoch convention."
        )
    block["generator"] = generator
    block["generator_parameters"] = parameters
    return block


def _check_generator(generator: str | None) -> None:
    """A `module.path:function` reference, and one the CIO has authorised.

    Validated rather than merely type-checked: `"not-a-reference"` is a string, and a
    string that cannot be resolved is not provenance. The allowlist is the second half —
    a well-formed reference to an arbitrary generator would let synthetic evidence be
    minted for anything.
    """
    if not generator:
        raise EvidenceError(
            "a synthetic_fixture series must name the generator that produced it; "
            "the frame alone does not say which function or which parameters"
        )
    module, separator, function = generator.partition(":")
    valid = (
        separator
        and module
        and function
        and ":" not in function
        and all(part.isidentifier() for part in module.split("."))
        and function.isidentifier()
    )
    if not valid:
        raise EvidenceError(
            f"generator reference {generator!r} is not of the form "
            f"'module.path:function'"
        )
    if generator not in APPROVED_GENERATORS:
        raise EvidenceError(
            f"generator {generator!r} is not CIO-approved. Synthetic evidence is "
            f"restricted to {list(APPROVED_GENERATORS)}; authorising another is a CIO "
            f"decision, not a caller argument."
        )


# ── the assembled block ─────────────────────────────────────────────────────────
def build(
    *,
    identity: dict[str, Any],
    trades: list[Trade],
    model: CostModel,
    run_config: dict[str, Any],
    price_series: dict[str, Any],
    funding_series: dict[str, Any],
    reconciliation: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the `falsification_input` block. Raises rather than emit a partial one."""
    missing = [name for name in IDENTITY_FIELDS if name not in identity]
    if missing:
        raise EvidenceError(
            f"trial identity is incomplete, missing: {', '.join(missing)}. The six "
            f"reproducibility inputs are read back from the registry row, config_path "
            f"from where the config was loaded, and dataset_ids from the experiment's "
            f"config — none of them is assembled from the caller's intent."
        )

    missing_facts = [n for n in FUNDING_FACT_FIELDS if n not in funding_series]
    if missing_facts:
        raise EvidenceError(
            f"funding evidence is incomplete, missing: {', '.join(missing_facts)}")

    starting_balance = run_config.get("starting_balance")
    if starting_balance is None:
        raise EvidenceError("run_config must carry starting_balance")
    returns = per_trade_returns(trades, model, float(starting_balance))

    cost_model = {
        field.name: getattr(model, field.name) for field in dataclasses.fields(model)
    }
    cost_model["regime"] = str(model.regime.value)

    block = {
        "schema_version": SCHEMA_VERSION,
        "identity": {name: identity[name] for name in IDENTITY_FIELDS}
        | ({"dataset_ids": list(identity["dataset_ids"])}
           if "dataset_ids" in identity else {}),
        "trades": [
            {field.name: getattr(trade, field.name)
             for field in dataclasses.fields(Trade)}
            for trade in sorted(trades, key=lambda t: t.exit_ts_ms)
        ],
        "cost_model": cost_model,
        "run_config": dict(run_config),
        "returns": {
            "per_trade": returns,
            "sharpe_per_trade": sharpe_per_trade(returns),
        },
        "price_series": price_series,
        "funding_series": funding_series,
        "reconciliation": dict(reconciliation),
    }
    _assert_finite(block, "falsification_input")
    return block
