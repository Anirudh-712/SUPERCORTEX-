"""The risk engine's guarantees, as executable claims.

Two properties matter more than the individual limits:

  1. **Unforgeable approval.** A strategy cannot mint permission to trade, because the
     signing secret lives inside the risk engine and is never exposed.
  2. **Fail-closed.** Anything the engine cannot evaluate is a rejection. An unavailable
     risk engine means no trading, never free trading.
"""
from __future__ import annotations

import pytest

from supercortex.risk.engine import (
    Action,
    ExecutionGate,
    OrderIntent,
    PortfolioState,
    RiskApproval,
    RiskEngine,
    RiskEngineError,
    RiskRejection,
    Side,
)

LIMITS = {
    "portfolio": {
        "max_gross_exposure_pct": 100, "max_leverage": 2.0, "max_daily_loss_pct": 2.0,
        "max_drawdown_pct": 15.0, "max_venue_exposure_pct": 40,
        "max_symbol_exposure_pct": 25, "max_orders_per_minute": 60,
        "min_liquidation_distance_pct": 25.0,
    },
    "kill_switch": {"daily_loss_pct": 3.0, "data_staleness_seconds": 60},
    "per_strategy": {"default": {
        "max_position_pct": 10, "max_drawdown_pct": 8, "max_order_notional_usd": 500,
    }},
}
NOW = 1_700_000_000_000


@pytest.fixture
def engine():
    return RiskEngine(dict(LIMITS), clock_ms=NOW)


@pytest.fixture
def state():
    return PortfolioState(
        equity=100_000.0,
        marks={"BTCUSDT": 50_000.0},
        venues={"BTCUSDT": "BINANCE"},
        last_data_ts_ms=NOW - 1_000,
        live_strategies=frozenset({"STR-000001"}),
    )


def intent(qty: float = 0.005, price: float = 50_000.0, side: Side = Side.BUY,
           strategy: str = "STR-000001") -> OrderIntent:
    return OrderIntent(strategy, "BTCUSDT", "BINANCE", side, qty, price, NOW)


# ── the structural guarantee ────────────────────────────────────────────────────
def test_a_clean_order_is_approved(engine, state):
    assert isinstance(engine.approve(intent(), state), RiskApproval)


def test_gate_refuses_an_order_with_no_approval(engine, state):
    gate = ExecutionGate(engine)
    with pytest.raises(PermissionError, match="no valid risk approval"):
        gate.submit(intent(), approval=None)
    assert gate.sent == []


def test_gate_refuses_a_forged_approval(engine, state):
    """The attack a rewritten strategy would actually attempt."""
    gate = ExecutionGate(engine)
    order = intent()
    forged = RiskApproval(order.fingerprint(), "deadbeef" * 8, "2026-01-01 00:00:00", ())
    with pytest.raises(PermissionError):
        gate.submit(order, forged)


def test_approval_does_not_transfer_to_a_different_order(engine, state):
    """Approve a small order, try to send a large one with the same approval."""
    small = intent(qty=0.001)
    approval = engine.approve(small, state)
    gate = ExecutionGate(engine)
    with pytest.raises(PermissionError):
        gate.submit(intent(qty=0.009), approval)
    gate.submit(small, approval)              # the order it was actually for
    assert gate.sent == [small]


def test_an_approval_from_another_engine_is_not_accepted(state):
    """Secrets are per-process, so a second engine cannot vouch for the first."""
    a, b = RiskEngine(dict(LIMITS), clock_ms=NOW), RiskEngine(dict(LIMITS), clock_ms=NOW)
    order = intent()
    approval = a.approve(order, state)
    assert isinstance(approval, RiskApproval)
    assert not b.verify(order, approval)


def test_engine_secret_is_not_reachable_from_the_approval(engine, state):
    approval = engine.approve(intent(), state)
    assert engine._secret.hex() not in repr(approval)


# ── fail-closed ─────────────────────────────────────────────────────────────────
def test_unknown_data_age_is_rejected(engine, state):
    from dataclasses import replace

    rejection = engine.approve(intent(), replace(state, last_data_ts_ms=None))
    assert isinstance(rejection, RiskRejection)
    assert rejection.check == "data_freshness"


def test_an_exception_inside_a_check_rejects(engine, state):
    class Exploding(dict):
        def get(self, *_args, **_kwargs):
            raise ValueError("boom")

    engine.limits = Exploding()
    rejection = engine.approve(intent(), state)
    assert isinstance(rejection, RiskRejection)
    assert rejection.check == "engine"
    assert "fail-closed" in rejection.reason.lower()


def test_missing_limits_file_refuses_to_start(tmp_path):
    from supercortex.risk import engine as mod

    with pytest.raises(RiskEngineError, match="Fail-closed"):
        mod.load_limits(tmp_path / "nope.yaml")


def test_digest_mismatch_refuses_to_start():
    with pytest.raises(RiskEngineError, match="digest"):
        RiskEngine({**LIMITS, "_digest": "actual"}, expected_digest="approved")


# ── the individual checks ───────────────────────────────────────────────────────
def test_kill_switch_blocks_everything(engine, state, monkeypatch, tmp_path):
    from supercortex.risk import engine as mod

    kill = tmp_path / "KILL"
    kill.write_text("")
    monkeypatch.setattr(mod, "KILL_SWITCH_PATH", kill)
    rejection = engine.approve(intent(), state)
    assert rejection.check == "kill_switch"
    assert rejection.action is Action.FLATTEN_ALL


def test_a_strategy_not_promoted_to_live_cannot_trade(engine, state):
    rejection = engine.approve(intent(strategy="STR-999999"), state)
    assert rejection.check == "strategy_live"


def test_stale_data_is_rejected(engine, state):
    from dataclasses import replace

    rejection = engine.approve(intent(), replace(state, last_data_ts_ms=NOW - 120_000))
    assert rejection.check == "data_freshness"


def test_order_rate_limit_stops_a_runaway_loop(engine, state):
    from dataclasses import replace

    rejection = engine.approve(intent(), replace(state, orders_this_minute=60))
    assert rejection.check == "order_rate"


def test_oversized_order_notional_rejected(engine, state):
    rejection = engine.approve(intent(qty=0.02), state)      # 1,000 > 500
    assert rejection.check == "order_notional"


def test_position_limit_counts_the_existing_position(engine, state):
    """0.1% orders are fine individually; the 200th is not."""
    from dataclasses import replace

    loaded = replace(state, strategy_positions={"STR-000001": {"BTCUSDT": 0.2}})
    rejection = engine.approve(intent(qty=0.005), loaded)
    assert rejection.check == "strategy_position"


def test_symbol_concentration_limit(engine, state):
    from dataclasses import replace

    big = replace(
        state,
        positions={"BTCUSDT": 0.5},
        strategy_positions={"STR-000001": {"BTCUSDT": 0.0}},
    )
    rejection = engine.approve(intent(qty=0.005), big)
    assert rejection.check == "symbol_concentration"


def test_venue_concentration_counts_other_instruments(engine, state):
    from dataclasses import replace

    crowded = replace(
        state,
        positions={"ETHUSDT": 20.0},
        marks={"BTCUSDT": 50_000.0, "ETHUSDT": 2_000.0},
        venues={"BTCUSDT": "BINANCE", "ETHUSDT": "BINANCE"},
    )
    rejection = engine.approve(intent(qty=0.005), crowded)
    assert rejection.check == "venue_concentration"


def test_leverage_limit(engine, state):
    from dataclasses import replace

    levered = replace(
        state, equity=10_000.0,
        positions={"ETHUSDT": 10.0}, marks={"BTCUSDT": 50_000.0, "ETHUSDT": 2_000.0},
        venues={"BTCUSDT": "KRAKEN", "ETHUSDT": "KRAKEN"},
    )
    rejection = engine.approve(
        OrderIntent("STR-000001", "BTCUSDT", "KRAKEN", Side.BUY, 0.005, 50_000.0, NOW),
        levered,
    )
    assert rejection.check in {"gross_exposure", "leverage", "venue_concentration",
                               "symbol_concentration", "strategy_position"}


def test_soft_daily_loss_suspends_all(engine, state):
    from dataclasses import replace

    rejection = engine.approve(intent(), replace(state, day_pnl=-2_500.0))
    assert rejection.check == "daily_loss"
    assert rejection.action is Action.SUSPEND_ALL


def test_hard_daily_loss_flattens(engine, state):
    from dataclasses import replace

    rejection = engine.approve(intent(), replace(state, day_pnl=-3_500.0))
    assert rejection.check == "daily_loss"
    assert rejection.action is Action.FLATTEN_ALL


def test_strategy_drawdown_suspends_that_strategy(engine, state):
    from dataclasses import replace

    drawn = replace(
        state,
        strategy_peak_equity={"STR-000001": 50_000.0},
        strategy_equity={"STR-000001": 45_000.0},          # 10% > 8%
    )
    rejection = engine.approve(intent(), drawn)
    assert rejection.check == "strategy_drawdown"
    assert rejection.action is Action.SUSPEND_STRATEGY


def test_portfolio_drawdown_suspends_everything(engine, state):
    from dataclasses import replace

    rejection = engine.approve(intent(), replace(state, peak_equity=125_000.0))
    assert rejection.check == "portfolio_drawdown"
    assert rejection.action is Action.SUSPEND_ALL


def test_liquidation_too_close_is_rejected(engine, state):
    from dataclasses import replace

    near = replace(state, liquidation_prices={"BTCUSDT": 45_000.0})   # 10% < 25%
    rejection = engine.approve(intent(), near)
    assert rejection.check == "liquidation_distance"


def test_checks_run_in_declared_order(engine, state):
    """Kill switch first, cheapest and most absolute. Order is part of the contract."""
    names = [name for name, _ in engine._checks()]
    assert names[0] == "kill_switch"
    assert names[1] == "strategy_live"
    assert len(names) == 14
    assert len(set(names)) == 14


def test_approval_records_every_check_it_passed(engine, state):
    approval = engine.approve(intent(), state)
    assert len(approval.checks_passed) == 14
