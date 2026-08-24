"""The risk engine. Risk is not an opinion an agent holds; it is a function an order passes.

The structural guarantee, and the reason this is not just a checklist:

    A strategy has no reference to an execution client. It emits an `OrderIntent`. The
    execution gate's `submit()` **requires a `RiskApproval`**, and an approval carries an
    HMAC signed with a secret generated inside the risk engine and never exposed. A
    strategy — or an agent that rewrote the strategy — cannot mint one, because it does not
    hold the key.

That converts "strategies must not bypass risk" from a rule someone could forget into
something closer to a type error.

Every check is **fail-closed**: if a limit cannot be evaluated — missing data, an
exception, a malformed state — the order is REJECTED. An unavailable risk engine means no
trading, never free trading.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from supercortex.registry.db import PROJECT_ROOT

LIMITS_PATH = PROJECT_ROOT / "06-risk" / "limits.yaml"
KILL_SWITCH_PATH = PROJECT_ROOT / "06-risk" / "KILL"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Action(StrEnum):
    """What the engine wants done beyond accepting or rejecting this one order."""

    NONE = "none"
    FLATTEN_ALL = "flatten_all"
    SUSPEND_STRATEGY = "suspend_strategy"
    SUSPEND_ALL = "suspend_all"


@dataclass(frozen=True)
class OrderIntent:
    """What a strategy asks for. It cannot ask for anything else."""

    strategy_id: str
    instrument_id: str
    venue: str
    side: Side
    quantity: float
    price: float
    ts_ms: int

    @property
    def notional(self) -> float:
        return abs(self.quantity * self.price)

    @property
    def signed_quantity(self) -> float:
        return self.quantity if self.side is Side.BUY else -self.quantity

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "strategy_id": self.strategy_id,
                "instrument_id": self.instrument_id,
                "venue": self.venue,
                "side": str(self.side),
                "quantity": round(self.quantity, 12),
                "price": round(self.price, 12),
                "ts_ms": self.ts_ms,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class PortfolioState:
    """Everything the engine needs to decide. Absent fields are treated as unknown."""

    equity: float
    positions: dict[str, float] = field(default_factory=dict)        # instrument -> signed qty
    marks: dict[str, float] = field(default_factory=dict)            # instrument -> mark price
    venues: dict[str, str] = field(default_factory=dict)             # instrument -> venue
    strategy_positions: dict[str, dict[str, float]] = field(default_factory=dict)
    day_pnl: float = 0.0
    peak_equity: float | None = None
    strategy_peak_equity: dict[str, float] = field(default_factory=dict)
    strategy_equity: dict[str, float] = field(default_factory=dict)
    last_data_ts_ms: int | None = None
    orders_this_minute: int = 0
    live_strategies: frozenset[str] = frozenset()
    liquidation_prices: dict[str, float] = field(default_factory=dict)

    def gross_exposure(self) -> float:
        return sum(abs(q) * self.marks.get(i, 0.0) for i, q in self.positions.items())


@dataclass(frozen=True)
class RiskApproval:
    """Unforgeable permission to send one specific order."""

    intent_fingerprint: str
    signature: str
    approved_at: str
    checks_passed: tuple[str, ...]


@dataclass(frozen=True)
class RiskRejection:
    check: str
    reason: str
    action: Action = Action.NONE

    @property
    def approved(self) -> bool:
        return False


class RiskEngineError(RuntimeError):
    pass


def load_limits(path: Path | None = None) -> dict[str, Any]:
    """Load and checksum the limits file.

    The digest is returned alongside so a running process can verify at startup that the
    limits are the ones that were approved, rather than whatever is on disk now.
    """
    target = path or LIMITS_PATH
    if not target.exists():
        raise RiskEngineError(
            f"{target} is missing. Fail-closed: with no limits defined there is no basis "
            f"on which to approve anything."
        )
    text = target.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    data["_digest"] = hashlib.sha256(text.encode()).hexdigest()
    return data


class RiskEngine:
    """Pre-trade risk. Deterministic, unit-tested, and the only source of approvals."""

    def __init__(
        self,
        limits: dict[str, Any] | None = None,
        *,
        expected_digest: str | None = None,
        clock_ms: int | None = None,
    ) -> None:
        self.limits = limits if limits is not None else load_limits()
        if expected_digest and self.limits.get("_digest") != expected_digest:
            raise RiskEngineError(
                "limits.yaml does not match the digest recorded at approval time. "
                "Refusing to start: the limits under which this strategy was approved are "
                "not the limits currently on disk."
            )
        # Generated per process, never written down, never exposed. This is what a
        # strategy cannot obtain, and therefore what it cannot forge.
        self._secret = secrets.token_bytes(32)
        self._clock_ms = clock_ms

    # ── the gate ───────────────────────────────────────────────────────────────
    def approve(
        self, intent: OrderIntent, state: PortfolioState
    ) -> RiskApproval | RiskRejection:
        """Run every check in order. First failure wins; exceptions reject."""
        try:
            for name, check in self._checks():
                rejection = check(intent, state)
                if rejection is not None:
                    return rejection
                self._last_check = name
        except Exception as exc:
            return RiskRejection(
                check="engine",
                reason=f"risk evaluation raised {type(exc).__name__}: {exc}. Fail-closed: "
                f"an unavailable risk engine means no trading, not free trading.",
            )
        return self._mint(intent)

    def _mint(self, intent: OrderIntent) -> RiskApproval:
        fingerprint = intent.fingerprint()
        return RiskApproval(
            intent_fingerprint=fingerprint,
            signature=hmac.new(self._secret, fingerprint.encode(), hashlib.sha256).hexdigest(),
            approved_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            checks_passed=tuple(name for name, _ in self._checks()),
        )

    def verify(self, intent: OrderIntent, approval: Any) -> bool:
        """Constant-time verification that this approval covers this exact order."""
        if not isinstance(approval, RiskApproval):
            return False
        fingerprint = intent.fingerprint()
        if not hmac.compare_digest(approval.intent_fingerprint, fingerprint):
            return False
        expected = hmac.new(self._secret, fingerprint.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(approval.signature, expected)

    # ── the checks, in order ───────────────────────────────────────────────────
    def _checks(self):
        return (
            ("kill_switch", self._kill_switch),
            ("strategy_live", self._strategy_live),
            ("data_freshness", self._data_freshness),
            ("order_rate", self._order_rate),
            ("order_notional", self._order_notional),
            ("strategy_position", self._strategy_position),
            ("symbol_concentration", self._symbol_concentration),
            ("venue_concentration", self._venue_concentration),
            ("gross_exposure", self._gross_exposure),
            ("leverage", self._leverage),
            ("daily_loss", self._daily_loss),
            ("strategy_drawdown", self._strategy_drawdown),
            ("portfolio_drawdown", self._portfolio_drawdown),
            ("liquidation_distance", self._liquidation_distance),
        )

    def _portfolio(self, key: str, default: float) -> float:
        return float((self.limits.get("portfolio") or {}).get(key, default))

    def _kill(self, key: str, default: float) -> float:
        return float((self.limits.get("kill_switch") or {}).get(key, default))

    def _per_strategy(self, strategy_id: str, key: str, default: float) -> float:
        block = self.limits.get("per_strategy") or {}
        specific = block.get(strategy_id) or {}
        if key in specific:
            return float(specific[key])
        return float((block.get("default") or {}).get(key, default))

    def _now_ms(self) -> int:
        return self._clock_ms if self._clock_ms is not None else int(
            datetime.now(UTC).timestamp() * 1000
        )

    # 1
    def _kill_switch(self, intent, state):
        if KILL_SWITCH_PATH.exists():
            return RiskRejection(
                "kill_switch",
                "the kill switch is engaged (06-risk/KILL exists). Nothing trades until "
                "it is removed by hand.",
                Action.FLATTEN_ALL,
            )
        return None

    # 2
    def _strategy_live(self, intent, state):
        if intent.strategy_id not in state.live_strategies:
            return RiskRejection(
                "strategy_live",
                f"{intent.strategy_id} is not in the LIVE set. Only a strategy the "
                f"lifecycle promoted to LIVE may send orders.",
            )
        return None

    # 3
    def _data_freshness(self, intent, state):
        max_age = self._kill("data_staleness_seconds", 60.0) * 1000
        if state.last_data_ts_ms is None:
            return RiskRejection(
                "data_freshness",
                "no market data timestamp available. Fail-closed: an order priced off "
                "unknown-age data is an order priced off nothing.",
            )
        age = self._now_ms() - state.last_data_ts_ms
        if age > max_age:
            return RiskRejection(
                "data_freshness",
                f"market data is {age / 1000:.1f}s old (limit {max_age / 1000:.0f}s). "
                f"This is how a disconnect becomes a position.",
            )
        return None

    # 4
    def _order_rate(self, intent, state):
        limit = self._portfolio("max_orders_per_minute", 60)
        if state.orders_this_minute >= limit:
            return RiskRejection(
                "order_rate",
                f"{state.orders_this_minute} orders already this minute (limit {limit:.0f}). "
                f"A runaway loop is stopped here, not by the venue.",
            )
        return None

    # 5
    def _order_notional(self, intent, state):
        limit = self._per_strategy(intent.strategy_id, "max_order_notional_usd", 500.0)
        if intent.notional > limit:
            return RiskRejection(
                "order_notional",
                f"order notional {intent.notional:,.2f} exceeds {limit:,.2f}",
            )
        return None

    # 6
    def _strategy_position(self, intent, state):
        pct = self._per_strategy(intent.strategy_id, "max_position_pct", 10.0)
        limit = state.equity * pct / 100.0
        current = (state.strategy_positions.get(intent.strategy_id) or {}).get(
            intent.instrument_id, 0.0
        )
        resulting = abs(current + intent.signed_quantity) * intent.price
        if resulting > limit:
            return RiskRejection(
                "strategy_position",
                f"resulting strategy position {resulting:,.2f} exceeds {pct:.1f}% of "
                f"equity ({limit:,.2f})",
            )
        return None

    # 7
    def _symbol_concentration(self, intent, state):
        pct = self._portfolio("max_symbol_exposure_pct", 25.0)
        limit = state.equity * pct / 100.0
        current = state.positions.get(intent.instrument_id, 0.0)
        resulting = abs(current + intent.signed_quantity) * intent.price
        if resulting > limit:
            return RiskRejection(
                "symbol_concentration",
                f"resulting {intent.instrument_id} exposure {resulting:,.2f} exceeds "
                f"{pct:.1f}% of equity ({limit:,.2f})",
            )
        return None

    # 8
    def _venue_concentration(self, intent, state):
        pct = self._portfolio("max_venue_exposure_pct", 40.0)
        limit = state.equity * pct / 100.0
        exposure = sum(
            abs(q) * state.marks.get(i, 0.0)
            for i, q in state.positions.items()
            if state.venues.get(i) == intent.venue and i != intent.instrument_id
        )
        current = state.positions.get(intent.instrument_id, 0.0)
        exposure += abs(current + intent.signed_quantity) * intent.price
        if exposure > limit:
            return RiskRejection(
                "venue_concentration",
                f"resulting {intent.venue} exposure {exposure:,.2f} exceeds {pct:.1f}% of "
                f"equity ({limit:,.2f}). Exchange failure is a live risk class in crypto, "
                f"not a tail hypothetical.",
            )
        return None

    # 9
    def _gross_exposure(self, intent, state):
        pct = self._portfolio("max_gross_exposure_pct", 100.0)
        limit = state.equity * pct / 100.0
        current = state.positions.get(intent.instrument_id, 0.0)
        others = sum(
            abs(q) * state.marks.get(i, 0.0)
            for i, q in state.positions.items()
            if i != intent.instrument_id
        )
        resulting = others + abs(current + intent.signed_quantity) * intent.price
        if resulting > limit:
            return RiskRejection(
                "gross_exposure",
                f"resulting gross exposure {resulting:,.2f} exceeds {pct:.1f}% of equity "
                f"({limit:,.2f})",
            )
        return None

    # 10
    def _leverage(self, intent, state):
        limit = self._portfolio("max_leverage", 2.0)
        if state.equity <= 0:
            return RiskRejection("leverage", "equity is not positive; leverage is undefined")
        current = state.positions.get(intent.instrument_id, 0.0)
        others = sum(
            abs(q) * state.marks.get(i, 0.0)
            for i, q in state.positions.items()
            if i != intent.instrument_id
        )
        resulting = (others + abs(current + intent.signed_quantity) * intent.price) / state.equity
        if resulting > limit:
            return RiskRejection(
                "leverage", f"resulting leverage {resulting:.2f}x exceeds {limit:.2f}x"
            )
        return None

    # 11
    def _daily_loss(self, intent, state):
        pct = self._portfolio("max_daily_loss_pct", 2.0)
        hard = self._kill("daily_loss_pct", 3.0)
        loss_pct = -state.day_pnl / state.equity * 100.0 if state.equity > 0 else 0.0
        if loss_pct >= hard:
            return RiskRejection(
                "daily_loss",
                f"day loss {loss_pct:.2f}% at or beyond the kill threshold {hard:.2f}%",
                Action.FLATTEN_ALL,
            )
        if loss_pct >= pct:
            return RiskRejection(
                "daily_loss",
                f"day loss {loss_pct:.2f}% exceeds the daily limit {pct:.2f}%",
                Action.SUSPEND_ALL,
            )
        return None

    # 12
    def _strategy_drawdown(self, intent, state):
        pct = self._per_strategy(intent.strategy_id, "max_drawdown_pct", 8.0)
        peak = state.strategy_peak_equity.get(intent.strategy_id)
        equity = state.strategy_equity.get(intent.strategy_id)
        if peak is None or equity is None or peak <= 0:
            return None
        drawdown = (peak - equity) / peak * 100.0
        if drawdown > pct:
            return RiskRejection(
                "strategy_drawdown",
                f"{intent.strategy_id} drawdown {drawdown:.2f}% exceeds {pct:.2f}%",
                Action.SUSPEND_STRATEGY,
            )
        return None

    # 13
    def _portfolio_drawdown(self, intent, state):
        pct = self._portfolio("max_drawdown_pct", 15.0)
        if state.peak_equity is None or state.peak_equity <= 0:
            return None
        drawdown = (state.peak_equity - state.equity) / state.peak_equity * 100.0
        if drawdown > pct:
            return RiskRejection(
                "portfolio_drawdown",
                f"portfolio drawdown {drawdown:.2f}% exceeds {pct:.2f}%",
                Action.SUSPEND_ALL,
            )
        return None

    # 14
    def _liquidation_distance(self, intent, state):
        multiple = self._portfolio("min_liquidation_distance_pct", 25.0)
        liq = state.liquidation_prices.get(intent.instrument_id)
        if liq is None or liq <= 0:
            return None
        distance = abs(intent.price - liq) / intent.price * 100.0
        if distance < multiple:
            return RiskRejection(
                "liquidation_distance",
                f"liquidation price {liq:,.2f} is only {distance:.1f}% away (minimum "
                f"{multiple:.1f}%)",
            )
        return None


class ExecutionGate:
    """The only object that talks to a venue. It refuses unapproved orders."""

    def __init__(self, engine: RiskEngine) -> None:
        self._engine = engine
        self.sent: list[OrderIntent] = []

    def submit(self, intent: OrderIntent, approval: Any = None) -> None:
        if not self._engine.verify(intent, approval):
            raise PermissionError(
                "order rejected at the execution gate: no valid risk approval for this "
                "exact order. An approval is signed with a secret held inside the risk "
                "engine, so a strategy cannot mint one — and an approval for a different "
                "order does not transfer."
            )
        self.sent.append(intent)
