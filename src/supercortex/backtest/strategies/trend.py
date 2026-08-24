"""EXP-000002 — BTC trend following with discrete rebalancing.

Pre-registered claim: the sign of the trailing-window return predicts the sign of the
following weeks' return, with a payoff of few large winners and many small losers.

The design constraint that makes this different from textbook time-series momentum:

    **Position changes only on signal flip. Never on volatility drift.**

Standard TSMOM rebalances continuously toward a volatility target. Under s.115BBH every
adjustment is a taxable disposal, so continuous rebalancing is a recurring tax cost paid
for a second-order improvement in risk scaling. Turnover is therefore a first-class design
variable here, not a by-product — see [[LRN-000002]].

Honesty notes:

* The signal is computed from **completed** bars only. Nautilus delivers a bar at its
  ``ts_event``, which `feed.to_bars` stamps at the interval close, so nothing in the
  rolling window has been seen early.
* Decisions are taken at the configured UTC hour. This machine runs Asia/Kolkata (+05:30),
  which is not even a whole number of hours off a daily boundary.
* Funding is consumed as a **cash flow**, not as a signal. It reaches `on_funding_rate`
  purely so the settled amount is recorded against open positions.
"""
from __future__ import annotations

from collections import deque

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType, FundingRateUpdate
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

BARS_PER_DAY = 24


class DiscreteTrendConfig(StrategyConfig, frozen=True):
    """Every value is hashed into the trial. Only `lookback_days` is searched."""

    instrument_id: InstrumentId
    bar_type: BarType

    lookback_days: int = 90            # SEARCHED over the declared grid
    entry_threshold: float = 0.0       # act on the sign of the trailing return
    rebalance_hour_utc: int = 0        # 00:00 UTC, never local time
    min_hold_bars: int = 168           # 7 days — discourages churn the tax code punishes
    trade_size_pct: float = 0.50
    allow_long: bool = True
    allow_short: bool = True
    rebalance_on_vol_drift: bool = False


class DiscreteTrend(Strategy):
    def __init__(self, config: DiscreteTrendConfig) -> None:
        super().__init__(config)
        self._window: deque[float] = deque(maxlen=config.lookback_days * BARS_PER_DAY + 1)
        self._bars_held = 0
        self._last_close: float | None = None
        self._current_direction = 0
        self._funding_received = 0.0

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

    def on_funding_rate(self, update: FundingRateUpdate) -> None:
        """Funding is a cash flow, not a signal. Recorded, never traded on."""
        net = self.portfolio.net_position(self.config.instrument_id)
        if net != 0 and self._last_close is not None:
            # Positive rate: longs pay shorts.
            self._funding_received += -float(net) * self._last_close * float(update.rate)

    # ── the decision ───────────────────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> None:
        close = float(bar.close)
        self._window.append(close)
        self._last_close = close

        if self._current_direction != 0:
            self._bars_held += 1

        # Discrete rebalancing: one decision point per day, at a fixed UTC hour. Acting on
        # every bar would multiply taxable disposals for no gain in signal quality.
        ts_hour = (bar.ts_event // 3_600_000_000_000) % 24
        if ts_hour != self.config.rebalance_hour_utc:
            return

        signal = self._signal()
        if signal is None:
            return

        if signal == self._current_direction:
            return                                  # already positioned; do nothing

        if self._current_direction != 0 and self._bars_held < self.config.min_hold_bars:
            return                                  # honour the minimum hold

        if signal == 0:
            self._flatten()
            return

        if signal > 0 and not self.config.allow_long:
            return
        if signal < 0 and not self.config.allow_short:
            return

        if self._current_direction != 0:
            self._flatten()
        self._enter(OrderSide.BUY if signal > 0 else OrderSide.SELL, signal)

    # ── signal ─────────────────────────────────────────────────────────────────
    def _signal(self) -> int | None:
        """Sign of the trailing-window return. None until the window is full."""
        if len(self._window) < self._window.maxlen:
            return None
        first, last = self._window[0], self._window[-1]
        if first <= 0:
            return None
        trailing_return = last / first - 1.0
        if abs(trailing_return) <= self.config.entry_threshold:
            return 0
        return 1 if trailing_return > 0 else -1

    # ── mechanics ──────────────────────────────────────────────────────────────
    def _flatten(self) -> None:
        self.close_all_positions(self.config.instrument_id)
        self._current_direction = 0
        self._bars_held = 0

    def _enter(self, side: OrderSide, direction: int) -> None:
        qty = self._position_size()
        if qty is None:
            return
        self.submit_order(
            self.order_factory.market(
                instrument_id=self.config.instrument_id,
                order_side=side,
                quantity=qty,
            )
        )
        self._current_direction = direction
        self._bars_held = 0

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
        qty = self.instrument.make_qty(notional / self._last_close)
        return qty if qty > 0 else None
