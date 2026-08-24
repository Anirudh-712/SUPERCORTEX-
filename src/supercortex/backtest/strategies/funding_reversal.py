"""EXP-000001 — BTC perp funding-rate reversal.

Pre-registered claim: when perpetual funding is extremely positive, longs are crowded and
paying to hold; the perp mean-reverts against them over the following days.

Mechanism: funding is a mechanical cash transfer from longs to shorts every 8 hours. At
extreme positive funding the marginal long is paying rent and is the most liquidation-prone
participant. The flow is structural, not sentiment.

Implementation notes that matter for honesty:

* The z-score uses only funding events **already received** — Nautilus delivers them in
  event-time order, so there is no way to see a future settlement.
* Decisions are taken on **bar close**, and the order fills on the **next** bar. Acting on
  the close of the bar you are deciding from is the most common lookahead in bar strategies.
* The daily decision boundary is **00:00 UTC**, not local time. This machine runs
  Asia/Kolkata (+05:30), which is not even a whole number of hours off.
"""
from __future__ import annotations

from collections import deque

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType, FundingRateUpdate
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy


class FundingReversalConfig(StrategyConfig, frozen=True):
    """Every parameter here is hashed into the trial. Nothing is passed at the call site."""

    instrument_id: InstrumentId
    bar_type: BarType
    lookback_events: int = 90        # 90 funding events ~ 30 days at 8h
    entry_z: float = 1.5             # |z| beyond which crowding is deemed extreme
    exit_z: float = 0.5              # normalisation level that closes the position
    max_hold_bars: int = 72          # hard time stop, in bars
    trade_size_pct: float = 0.20     # fraction of equity per position
    min_events_before_trading: int = 90
    allow_long: bool = True
    allow_short: bool = True


class FundingReversal(Strategy):
    def __init__(self, config: FundingReversalConfig) -> None:
        super().__init__(config)
        self._rates: deque[float] = deque(maxlen=config.lookback_events)
        self._z: float | None = None
        self._events_seen = 0
        self._bars_held = 0
        self._last_close: float | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"instrument not found: {self.config.instrument_id}")
            self.stop()
            return
        self.subscribe_bars(self.config.bar_type)
        self.subscribe_funding_rates(self.config.instrument_id)

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)

    # ── data ───────────────────────────────────────────────────────────────────
    def on_funding_rate(self, update: FundingRateUpdate) -> None:
        """Update the crowding estimate. Only settled events ever reach here."""
        self._rates.append(float(update.rate))
        self._events_seen += 1
        if len(self._rates) < self.config.min_events_before_trading:
            self._z = None
            return
        mean = sum(self._rates) / len(self._rates)
        var = sum((r - mean) ** 2 for r in self._rates) / (len(self._rates) - 1)
        sd = var**0.5
        self._z = None if sd == 0 else (self._rates[-1] - mean) / sd

    def on_bar(self, bar: Bar) -> None:
        """Act on a completed bar. The fill lands on the next one."""
        self._last_close = float(bar.close)
        net = self.portfolio.net_position(self.config.instrument_id)

        if net != 0:
            self._bars_held += 1
            if self._should_exit(net):
                self.close_all_positions(self.config.instrument_id)
                self._bars_held = 0
            return

        self._bars_held = 0
        if self._z is None:
            return

        # Fade the crowd: extreme positive funding means crowded longs, so we sell.
        if self._z >= self.config.entry_z and self.config.allow_short:
            self._enter(OrderSide.SELL)
        elif self._z <= -self.config.entry_z and self.config.allow_long:
            self._enter(OrderSide.BUY)

    # ── mechanics ──────────────────────────────────────────────────────────────
    def _should_exit(self, net_position: float) -> bool:
        if self._bars_held >= self.config.max_hold_bars:
            return True
        if self._z is None:
            return False
        # Close once crowding has normalised back inside the exit band.
        return abs(self._z) <= self.config.exit_z

    def _enter(self, side: OrderSide) -> None:
        qty = self._position_size()
        if qty is None:
            return
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=qty,
        )
        self.submit_order(order)

    def _position_size(self) -> Quantity | None:
        if self._last_close is None or self._last_close <= 0:
            return None
        account = self.portfolio.account(self.instrument.venue)
        if account is None:
            return None
        balance = account.balance_total(self.instrument.quote_currency)
        if balance is None:
            return None
        notional = float(balance.as_double()) * self.config.trade_size_pct
        raw = notional / self._last_close
        qty = self.instrument.make_qty(raw)
        return qty if qty > 0 else None
