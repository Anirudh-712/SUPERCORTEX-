"""Build, run and harvest a Nautilus backtest.

Everything here is deterministic given (config, code commit, data snapshot). That is the
contract the registry records, and it is what makes a trial evidence rather than an anecdote.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import polars as pl
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FillModel, LatencyModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from supercortex.backtest import feed
from supercortex.backtest.strategies.funding_reversal import (
    FundingReversal,
    FundingReversalConfig,
)
from supercortex.costs.model import Trade

MS_TO_NS = 1_000_000


@dataclass(frozen=True)
class ProbeSpec:
    """Deliberate corruption of one input channel, to interrogate the strategy.

    Sign convention throughout: **positive shift = data arrives earlier = look-ahead
    granted**; negative delays it. `scramble` permutes a series' content while keeping its
    timestamps.
    """

    bars_shift: int = 0
    bars_scramble: bool = False
    funding_shift: int = 0
    funding_scramble: bool = False
    label: str = "baseline"

    @property
    def is_baseline(self) -> bool:
        return not (self.bars_shift or self.bars_scramble
                    or self.funding_shift or self.funding_scramble)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "bars_shift": self.bars_shift,
            "bars_scramble": self.bars_scramble,
            "funding_shift": self.funding_shift,
            "funding_scramble": self.funding_scramble,
        }


@dataclass(frozen=True)
class BacktestSpec:
    """The complete, hashable description of a run."""

    symbol: str = "BTCUSDT"
    market: str = "um"
    interval: str = "1h"
    start_ms: int | None = None
    end_ms: int | None = None
    starting_balance: float = 100_000.0
    leverage: float = 1.0
    #: Order latency in milliseconds. A home connection in India is not colocated.
    latency_ms: int = 250
    seed: int = 42
    params: dict[str, Any] | None = None
    #: Falsification probe controls. See `ProbeSpec`.
    probe: ProbeSpec = field(default_factory=ProbeSpec)


def fee_free_instrument() -> CryptoPerpetual:
    """BTCUSDT perp with venue fees set to zero.

    Not because trading is free — the cost model charges more than Binance does — but
    because s.115BBH taxes the **price gain**, and whether exchange fees are deductible
    against it is a contested flag in `CostModel`. If the engine nets fees into realised
    PnL, that gross figure is gone and the flag cannot be modelled at all.

    So: Nautilus owns mechanics (sequencing, latency, whether a fill happens), the cost
    model owns economics (fees, spread, slippage, TDS, tax). Every friction is applied
    once, in one versioned place, and the two are reconciled after every run.
    """
    reference = TestInstrumentProvider.btcusdt_perp_binance()
    return CryptoPerpetual(
        instrument_id=reference.id,
        raw_symbol=reference.raw_symbol,
        base_currency=reference.base_currency,
        quote_currency=reference.quote_currency,
        settlement_currency=reference.settlement_currency,
        is_inverse=False,
        price_precision=reference.price_precision,
        size_precision=reference.size_precision,
        price_increment=Price(float(reference.price_increment), reference.price_precision),
        size_increment=Quantity(float(reference.size_increment), reference.size_precision),
        margin_init=reference.margin_init,
        margin_maint=reference.margin_maint,
        maker_fee=Decimal("0"),
        taker_fee=Decimal("0"),
        ts_event=0,
        ts_init=0,
    )


def run(spec: BacktestSpec) -> tuple[list[Trade], dict[str, Any]]:
    """Execute the backtest and return closed round trips plus engine diagnostics."""
    instrument = fee_free_instrument()
    venue = Venue("BINANCE")
    bar_type = BarType.from_str(f"{instrument.id}-1-HOUR-LAST-EXTERNAL")

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="SUPERCORTEX-001",
            logging=LoggingConfig(log_level="ERROR"),
        )
    )
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(spec.starting_balance, USDT)],
        default_leverage=Decimal(str(spec.leverage)),
        # Fills are deterministic here; economics live in the cost model. See
        # `fee_free_instrument` for why.
        fill_model=FillModel(prob_fill_on_limit=1.0, prob_slippage=0.0,
                             random_seed=spec.seed),
        latency_model=LatencyModel(
            base_latency_nanos=spec.latency_ms * MS_TO_NS,
            insert_latency_nanos=spec.latency_ms * MS_TO_NS,
        ),
        bar_execution=True,
    )
    engine.add_instrument(instrument)

    frame = feed.load_bars_frame(spec.symbol, spec.market, spec.interval)
    if spec.start_ms is not None:
        frame = frame.filter(pl.col("ts_ms") >= spec.start_ms)
    if spec.end_ms is not None:
        frame = frame.filter(pl.col("ts_ms") <= spec.end_ms)

    bars = feed.to_bars(
        frame, bar_type, instrument.price_precision, instrument.size_precision,
        spec.interval,
        shift_bars=spec.probe.bars_shift,
        scramble=spec.probe.bars_scramble,
        seed=spec.seed,
    )
    engine.add_data(bars)

    funding = feed.load_funding(
        spec.symbol, spec.market, instrument.id,
        shift_intervals=spec.probe.funding_shift,
        scramble=spec.probe.funding_scramble,
        seed=spec.seed,
    )
    if spec.start_ms is not None:
        funding = [f for f in funding if f.ts_event >= spec.start_ms * MS_TO_NS]
    if spec.end_ms is not None:
        funding = [f for f in funding if f.ts_event <= spec.end_ms * MS_TO_NS]
    engine.add_data(funding)

    params = dict(spec.params or {})
    engine.add_strategy(
        FundingReversal(
            FundingReversalConfig(
                instrument_id=instrument.id, bar_type=bar_type, **params
            )
        )
    )

    engine.run()

    positions = engine.trader.generate_positions_report()
    fills = engine.trader.generate_order_fills_report()
    trades, engine_pnl, dropped_open = _to_trades(positions)

    # Reconciliation: our gross must equal the engine's realised PnL. With fees zeroed
    # and slippage off, any divergence means we are reading the report wrong — which
    # would silently corrupt every downstream number.
    our_gross = sum(t.gross_pnl for t in trades)
    diagnostics = {
        "bars": len(bars),
        "funding_events": len(funding),
        "positions_rows": 0 if positions is None else len(positions),
        "fills": 0 if fills is None else len(fills),
        "closed_trades": len(trades),
        "open_at_end_dropped": dropped_open,
        "probe": spec.probe.as_dict(),
        "engine_realized_pnl": round(engine_pnl, 6),
        "our_gross_pnl": round(our_gross, 6),
        "reconciliation_diff": round(our_gross - engine_pnl, 6),
    }
    engine.dispose()
    return trades, diagnostics


def _ts_ms(value: Any) -> int:
    """Positions reports mix pandas Timestamps and integer nanoseconds. Handle both."""
    if hasattr(value, "value"):          # pandas Timestamp
        return int(value.value) // MS_TO_NS
    return int(value) // MS_TO_NS


def _to_trades(positions: Any) -> tuple[list[Trade], float, int]:
    """Convert Nautilus closed positions into cost-model trades.

    Only *closed* positions become trades. An open position at the end of the run has no
    exit price, and inventing one by marking to the last close would flatter the result by
    letting a losing position sit unrealised forever. It is dropped and counted.
    """
    if positions is None or len(positions) == 0:
        return [], 0.0, 0

    trades: list[Trade] = []
    engine_pnl = 0.0
    dropped = 0
    seen: set[str] = set()

    for idx, row in positions.iterrows():
        position_id = str(idx)
        if position_id in seen:          # snapshots can repeat a position
            continue
        if str(row.get("side", "")).upper() != "FLAT":
            dropped += 1
            continue
        avg_open, avg_close = row.get("avg_px_open"), row.get("avg_px_close")
        if avg_open is None or avg_close is None:
            dropped += 1
            continue
        try:
            entry_price, exit_price = float(avg_open), float(avg_close)
            qty = float(row["peak_qty"])
        except (TypeError, ValueError, KeyError):
            continue
        if qty <= 0:
            continue
        seen.add(position_id)

        direction = 1 if "BUY" in str(row.get("entry", "")).upper() else -1
        trades.append(
            Trade(
                entry_ts_ms=_ts_ms(row["ts_opened"]),
                exit_ts_ms=_ts_ms(row["ts_closed"]),
                direction=direction,
                qty=qty,
                entry_price=entry_price,
                exit_price=exit_price,
                funding_pnl=0.0,   # applied separately from the funding series
            )
        )
        realized = row.get("realized_pnl")
        if realized is not None:
            engine_pnl += float(str(realized).split()[0])

    return trades, engine_pnl, dropped
