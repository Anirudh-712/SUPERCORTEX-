"""The strategy lifecycle. State changes go through here or they do not happen.

Promotion is subtraction, not addition: a candidate advances because a fixed battery of
attempts to kill it all failed, never because it looks good. Encoding that in code turns
discipline — which fails at 2 a.m. when a strategy looks amazing — into a constraint.

The machine refuses any transition whose gate it cannot actually verify. A gate that has
not been built yet is a **refusal**, not a pass. Anything else would let the absence of a
check read as the success of one.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from supercortex.registry import db, experiments

STATES = (
    "IDEA", "CANDIDATE", "RESEARCH", "BACKTESTING", "VALIDATING", "FALSIFYING",
    "PAPER", "APPROVED", "LIVE", "DEGRADED", "SUSPENDED", "RETIRED", "KILLED",
)

#: Forward transitions. KILLED is reachable from anywhere and handled separately —
#: abandoning a line of research must never be harder than continuing it.
TRANSITIONS: dict[str, set[str]] = {
    "IDEA": {"CANDIDATE"},
    "CANDIDATE": {"RESEARCH"},
    "RESEARCH": {"BACKTESTING"},
    "BACKTESTING": {"VALIDATING"},
    "VALIDATING": {"FALSIFYING"},
    "FALSIFYING": {"PAPER"},
    "PAPER": {"APPROVED"},
    "APPROVED": {"LIVE"},
    "LIVE": {"DEGRADED", "SUSPENDED"},
    "DEGRADED": {"LIVE", "SUSPENDED"},
    "SUSPENDED": {"LIVE", "RETIRED"},
    "RETIRED": set(),
    "KILLED": set(),
}

#: No agent may perform these. They are the points where capital is at stake.
HUMAN_GATED: set[tuple[str, str]] = {
    ("PAPER", "APPROVED"),
    ("APPROVED", "LIVE"),
    ("DEGRADED", "LIVE"),
    ("SUSPENDED", "LIVE"),
}


class TransitionRefused(RuntimeError):
    """A gate said no. This is the machine working, not failing."""


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str


def _gate_backtesting_to_validating(experiment_id: str) -> GateResult:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM trial WHERE experiment_id = ? AND outcome = 'ok'",
            (experiment_id,),
        ).fetchone()
    if rows["n"] == 0:
        return GateResult(False, "no successful trial has been run")
    return GateResult(True, f"{rows['n']} successful trial(s)")


def _gate_not_built(name: str) -> GateResult:
    return GateResult(
        False,
        f"the {name} gate is not implemented yet. A gate that does not exist is a "
        f"refusal, not a pass — otherwise the absence of a check reads as success.",
    )


#: A gate looks at one experiment and either lets it through or says why not. It cannot
#: return None, cannot return a bare bool, and has no way to express "skip": the only
#: outcomes are a pass with a reason and a refusal with a reason. Promotion is subtraction,
#: so the type makes "no answer" unrepresentable rather than merely discouraged.
Gate = Callable[[str], GateResult]

GATES: dict[tuple[str, str], Gate] = {
    ("RESEARCH", "BACKTESTING"): lambda e: GateResult(True, "no precondition"),
    ("BACKTESTING", "VALIDATING"): _gate_backtesting_to_validating,
    ("VALIDATING", "FALSIFYING"): lambda e: _gate_not_built("independent review"),
    ("FALSIFYING", "PAPER"): lambda e: _gate_not_built("falsification battery"),
    ("PAPER", "APPROVED"): lambda e: _gate_not_built("90-day paper trading"),
    ("APPROVED", "LIVE"): lambda e: _gate_not_built("live readiness"),
}


def current_state(experiment_id: str) -> str:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM experiment WHERE id = ?", (experiment_id,)
        ).fetchone()
    if row is None:
        raise KeyError(experiment_id)
    return str(row["status"])


def transition(
    experiment_id: str,
    to_state: str,
    *,
    decision_id: str | None = None,
    approved_by: str | None = None,
) -> str:
    """Move an experiment to a new state, or refuse and say why."""
    if to_state not in STATES:
        raise TransitionRefused(f"unknown state {to_state!r}")

    from_state = current_state(experiment_id)
    if from_state == to_state:
        raise TransitionRefused(f"{experiment_id} is already {to_state}")

    if to_state == "KILLED":
        if not decision_id:
            raise TransitionRefused(
                "killing a line of research requires a decision note. The reasoning is the "
                "point — a killed experiment is a result, and results without reasoning "
                "cannot be mined later."
            )
    else:
        allowed = TRANSITIONS.get(from_state, set())
        if to_state not in allowed:
            raise TransitionRefused(
                f"{from_state} -> {to_state} is not a legal transition. "
                f"From {from_state} you may go to: {', '.join(sorted(allowed)) or 'nowhere'}."
            )
        gate = GATES.get((from_state, to_state))
        if gate is not None:
            result = gate(experiment_id)
            if not result.passed:
                raise TransitionRefused(f"gate {from_state} -> {to_state}: {result.reason}")
        if (from_state, to_state) in HUMAN_GATED and not approved_by:
            raise TransitionRefused(
                f"{from_state} -> {to_state} is human-gated. No agent may perform it. "
                "Supply --approved-by with a signed decision note."
            )

    with db.connect() as conn:
        conn.execute(
            "UPDATE experiment SET status = ? WHERE id = ?", (to_state, experiment_id)
        )
        conn.execute(
            "INSERT INTO lifecycle_event(entity_id, from_state, to_state, gate_report_path,"
            " approved_by, at) VALUES (?,?,?,?,?,?)",
            (experiment_id, from_state, to_state, decision_id, approved_by, db.now()),
        )
    return from_state


def history(experiment_id: str) -> list[dict[str, object]]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT from_state, to_state, gate_report_path, approved_by, at"
            " FROM lifecycle_event WHERE entity_id = ? ORDER BY id",
            (experiment_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def record_decision(title: str, note_path: str, approved_by: str) -> str:
    """Allocate a DEC id and record it. Decisions are commits, not chat messages."""
    with db.connect() as conn:
        decision_id = db.allocate_id(conn, "DEC")
        conn.execute(
            "INSERT INTO decision(id, title, note_path, approved_by, at) VALUES (?,?,?,?,?)",
            (decision_id, title, note_path, approved_by, db.now()),
        )
    return decision_id


def record_learning(title: str, note_path: str, source_experiment: str | None) -> str:
    with db.connect() as conn:
        learning_id = db.allocate_id(conn, "LRN")
        conn.execute(
            "INSERT INTO learning(id, title, note_path, source_experiment, at)"
            " VALUES (?,?,?,?,?)",
            (learning_id, title, note_path, source_experiment, db.now()),
        )
    return learning_id


__all__ = [
    "GATES",
    "HUMAN_GATED",
    "STATES",
    "TRANSITIONS",
    "TransitionRefused",
    "current_state",
    "experiments",
    "history",
    "record_decision",
    "record_learning",
    "transition",
]
