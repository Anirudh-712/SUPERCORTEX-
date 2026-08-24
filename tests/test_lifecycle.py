"""The lifecycle state machine, as executable claims.

Promotion is subtraction: a candidate advances because a battery of attempts to kill it
all failed, never because it looks good. Two properties carry that, and neither had any
coverage before this file existed:

  1. **An unbuilt gate is a refusal.** If the absence of a check ever read as the success
     of one, every state above BACKTESTING would be reachable by doing nothing.
  2. **Human-gated transitions need a human.** They are exactly the points where capital
     is at stake, and no agent may perform them.

The registry is a throwaway file under tmp_path. Nothing here touches registry.sqlite, runs
a trial, or reaches a backtest.
"""
from __future__ import annotations

import pytest

from supercortex.lifecycle import machine
from supercortex.registry import db


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.sqlite"
    db.init(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO experiment(id,title,slug,status,trial_budget,created_at)"
            " VALUES ('EXP-000001','probe','probe','RESEARCH',10,?)",
            (db.now(),),
        )
        yield conn


def set_state(conn, state: str) -> None:
    conn.execute("UPDATE experiment SET status = ? WHERE id = 'EXP-000001'", (state,))


def add_ok_trial(conn, seq: int = 1) -> None:
    conn.execute(
        "INSERT INTO trial(experiment_id,seq,config_hash,code_commit,data_snapshot,"
        "cost_model_version,seed,started_at,outcome)"
        " VALUES ('EXP-000001',?,'cfg','abc','snap','v1',1,?,'ok')",
        (seq, db.now()),
    )


# ── the gate contract ───────────────────────────────────────────────────────────
def test_every_gate_returns_a_gate_result(registry):
    """One shape, no exceptions: a gate answers, and its answer carries a reason."""
    for (source, target), gate in machine.GATES.items():
        result = gate("EXP-000001")
        assert isinstance(result, machine.GateResult), f"{source} -> {target}"
        assert result.reason, f"{source} -> {target} refused or passed without saying why"


def test_an_unbuilt_gate_refuses(registry):
    """The property the whole module rests on. A gate nobody has written yet must say no."""
    for pair in (("VALIDATING", "FALSIFYING"), ("FALSIFYING", "PAPER"),
                 ("PAPER", "APPROVED"), ("APPROVED", "LIVE")):
        result = machine.GATES[pair]("EXP-000001")
        assert not result.passed, pair
        assert "not implemented yet" in result.reason


def test_every_gated_transition_has_a_gate():
    """A forward transition with no entry in GATES passes silently. These are the ones
    above BACKTESTING, where that would matter."""
    for pair in (("RESEARCH", "BACKTESTING"), ("BACKTESTING", "VALIDATING"),
                 ("VALIDATING", "FALSIFYING"), ("FALSIFYING", "PAPER"),
                 ("PAPER", "APPROVED"), ("APPROVED", "LIVE")):
        assert pair in machine.GATES, f"{pair} would advance unchecked"


# ── refusals ────────────────────────────────────────────────────────────────────
def test_an_unknown_state_is_refused(registry):
    with pytest.raises(machine.TransitionRefused, match="unknown state"):
        machine.transition("EXP-000001", "ASCENDED")


def test_a_transition_to_the_current_state_is_refused(registry):
    with pytest.raises(machine.TransitionRefused, match="already"):
        machine.transition("EXP-000001", "RESEARCH")


def test_an_illegal_transition_is_refused_and_lists_the_legal_ones(registry):
    with pytest.raises(machine.TransitionRefused, match="not a legal transition"):
        machine.transition("EXP-000001", "LIVE")


def test_promotion_stops_at_an_unbuilt_gate(registry):
    set_state(registry, "VALIDATING")
    with pytest.raises(machine.TransitionRefused, match="not implemented yet"):
        machine.transition("EXP-000001", "FALSIFYING")


def test_backtesting_to_validating_needs_a_successful_trial(registry):
    set_state(registry, "BACKTESTING")
    with pytest.raises(machine.TransitionRefused, match="no successful trial"):
        machine.transition("EXP-000001", "VALIDATING")


def test_backtesting_to_validating_passes_once_a_trial_succeeded(registry):
    set_state(registry, "BACKTESTING")
    add_ok_trial(registry)
    assert machine.transition("EXP-000001", "VALIDATING") == "BACKTESTING"


def test_human_gated_transitions_refuse_an_agent(registry):
    """No agent may move capital. SUSPENDED -> LIVE has no gate entry, so it reaches the
    human check rather than stopping earlier at an unbuilt gate."""
    expected = {
        ("PAPER", "APPROVED"), ("APPROVED", "LIVE"),
        ("DEGRADED", "LIVE"), ("SUSPENDED", "LIVE"),
    }
    assert expected == machine.HUMAN_GATED
    set_state(registry, "SUSPENDED")
    with pytest.raises(machine.TransitionRefused, match="human-gated"):
        machine.transition("EXP-000001", "LIVE")


def test_a_human_gated_transition_proceeds_with_an_approver(registry):
    set_state(registry, "SUSPENDED")
    assert machine.transition("EXP-000001", "LIVE", approved_by="cio") == "SUSPENDED"
    assert machine.current_state("EXP-000001") == "LIVE"


# ── killing must never be harder than continuing ────────────────────────────────
def test_killing_requires_a_decision_note(registry):
    with pytest.raises(machine.TransitionRefused, match="decision note"):
        machine.transition("EXP-000001", "KILLED")


def test_killing_is_reachable_from_anywhere_with_a_note(registry):
    for state in ("IDEA", "BACKTESTING", "PAPER", "LIVE"):
        set_state(registry, state)
        assert machine.transition("EXP-000001", "KILLED",
                                  decision_id="DEC-000001") == state
        set_state(registry, state)


def test_a_transition_is_recorded_in_history(registry):
    set_state(registry, "BACKTESTING")
    add_ok_trial(registry)
    machine.transition("EXP-000001", "VALIDATING")
    events = machine.history("EXP-000001")
    assert events[-1]["from_state"] == "BACKTESTING"
    assert events[-1]["to_state"] == "VALIDATING"


def test_a_refused_transition_changes_nothing(registry):
    set_state(registry, "VALIDATING")
    with pytest.raises(machine.TransitionRefused):
        machine.transition("EXP-000001", "FALSIFYING")
    assert machine.current_state("EXP-000001") == "VALIDATING"
    assert machine.history("EXP-000001") == []
