"""The India cost model: what a strategy actually keeps.

Nothing off the shelf models a 30% flat tax with no loss offset plus a 1% transfer levy,
so this is one of the five components the desk builds itself.

The headline result of this module is a single number — the **break-even profit factor**.
Under s.115BBH you are taxed on every winning disposal and get nothing back for a losing
one, so a strategy can be profitable before tax and loss-making after it. The threshold is
closed-form and unforgiving:

    PF_min = 1 / (1 - effective_tax_rate)

At 30% plus 4% cess that is **1.4535**. A strategy whose gross wins are less than ~1.45x
its gross losses pays more tax than it earns — before a single rupee of fees, spread,
slippage or TDS. Sharpe ratio does not tell you this. Profit factor does.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class ProfitFactorState(StrEnum):
    """Profit factor is a ratio, and a ratio has three states, not one.

        finite      losses > 0                 -> a number
        unbounded   wins > 0, losses == 0      -> no finite value exists
        undefined   wins == 0, losses == 0     -> the ratio is 0/0

    Collapsing the last two into `inf` produced an artifact that could not be serialised
    as JSON, and collapsing them into a sentinel would produce a number nobody could
    distinguish from a real one. Both non-finite states are legitimate results — an
    all-winning run is rare and suspicious, a zero-trade run is a valid negative result —
    so they are tagged and kept, not thrown away.
    """

    FINITE = "finite"
    UNBOUNDED = "unbounded"
    UNDEFINED = "undefined"


def profit_factor_state(wins: float, losses: float) -> ProfitFactorState:
    if losses > 0:
        return ProfitFactorState.FINITE
    if wins > 0:
        return ProfitFactorState.UNBOUNDED
    return ProfitFactorState.UNDEFINED


def profit_factor_json(value: float, state: ProfitFactorState) -> float | None:
    """The value as it goes into an artifact: a number, or an explicit null."""
    return round(value, 6) if state is ProfitFactorState.FINITE else None


def format_profit_factor(value: float, state: ProfitFactorState,
                         spec: str = ".3f") -> str:
    """For humans. Names the state rather than printing `inf`."""
    if state is ProfitFactorState.FINITE:
        return format(value, spec)
    return str(state.value)


class TaxRegime(StrEnum):
    """How the tax authority might treat this activity.

    The treatment of cash-settled crypto derivatives is **unsettled in India as of
    2026-08**: published guidance directly contradicts itself on whether s.194S TDS and the
    s.115BBH 30% flat rate apply to perpetual futures, and many practitioners treat
    derivative profits as business income instead. There is no government clarification.

    Rather than bury a guess in a constant, the regime is a named, versioned input that is
    hashed into every trial. A strategy that is profitable only under the permissive
    reading is a *finding*, not a result — and the falsification engine requires survival
    under `VDA_STRICT`.

    UNVERIFIED: this is a modelling scaffold, not tax advice. Confirm with a chartered
    accountant before any capital is committed.
    """

    #: Most conservative. s.115BBH applies: 30% + cess on every winning disposal, no loss
    #: offset, no expense deduction, plus 1% s.194S TDS on disposal notional.
    VDA_STRICT = "vda_strict"

    #: 115BBH applies but 194S does not — the reading that a cash-settled derivative is not
    #: a "transfer of a VDA". Platforms offering INR-settled perps market this position.
    VDA_NO_TDS = "vda_no_tds"

    #: Derivative profits treated as business/speculative income: taxed on NET income,
    #: losses set off and carried forward, expenses deductible. The permissive reading.
    BUSINESS_INCOME = "business_income"


@dataclass(frozen=True)
class Trade:
    """One closed round trip, before any cost is applied."""

    entry_ts_ms: int
    exit_ts_ms: int
    direction: int          # +1 long, -1 short
    qty: float
    entry_price: float
    exit_price: float
    entry_taker: bool = True
    exit_taker: bool = True
    #: Funding cash flow over the life of the position. Positive = received.
    funding_pnl: float = 0.0

    def __post_init__(self) -> None:
        if self.direction not in (1, -1):
            raise ValueError(f"direction must be +1 or -1, got {self.direction}")
        if self.qty <= 0:
            raise ValueError(f"qty must be positive, got {self.qty}")
        if self.exit_ts_ms < self.entry_ts_ms:
            raise ValueError("exit precedes entry")

    @property
    def gross_pnl(self) -> float:
        """Price PnL only. No fees, no funding, no tax."""
        return self.direction * (self.exit_price - self.entry_price) * self.qty

    @property
    def entry_notional(self) -> float:
        return self.entry_price * self.qty

    @property
    def exit_notional(self) -> float:
        return self.exit_price * self.qty

    @property
    def turnover(self) -> float:
        return self.entry_notional + self.exit_notional


@dataclass(frozen=True)
class CostModel:
    """Execution frictions plus the Indian tax treatment.

    `version` is recorded in every trial row. Changing any field without bumping it makes
    two trials incomparable while looking identical, so the version is not decoration.
    """

    version: str = "india-vda-1.0.0"
    regime: TaxRegime = TaxRegime.VDA_STRICT

    # ── execution frictions, in basis points of notional ──
    taker_fee_bps: float = 4.5        # Binance USD-M taker, no VIP tier
    maker_fee_bps: float = 2.0
    slippage_bps: float = 3.0         # model at 2x whatever you measure
    half_spread_bps: float = 1.0      # paid on each leg

    # ── tax ──
    capital_gains_rate: float = 0.30  # s.115BBH
    cess_rate: float = 0.04           # health & education cess on the tax
    tds_rate: float = 0.01            # s.194S
    business_income_rate: float = 0.30  # top slab, for the BUSINESS_INCOME regime

    #: Under s.115BBH only cost of acquisition is deductible. Whether exchange fees form
    #: part of that is contested; the conservative reading is that they do not, so you are
    #: taxed on the price gain even though fees consumed part of it.
    fees_deductible: bool = False

    #: Whether funding *paid* reduces taxable income. Conservative reading: it does not.
    funding_deductible: bool = False

    #: Whether TDS withheld can actually be credited against the final liability and any
    #: excess refunded. On an FIU-registered Indian exchange that issues Form 16A this is
    #: true, and TDS is a cash-flow drag rather than a cost. On a foreign venue where the
    #: obligation falls on you and no certificate is issued, unrecovered TDS becomes a
    #: permanent loss — which is a venue-selection consequence, not a tax one.
    tds_recoverable: bool = True

    #: Annual cost of the working capital that TDS ties up until the refund lands.
    #: Refunds typically arrive after the return is filed and processed, so money withheld
    #: in April can be idle for well over a year.
    capital_carry_rate: float = 0.07
    tds_refund_lag_years: float = 1.0

    notes: dict[str, str] = field(default_factory=dict)

    # ── derived ────────────────────────────────────────────────────────────────────
    @property
    def effective_tax_rate(self) -> float:
        """Rate applied to a winning disposal, cess included."""
        if self.regime is TaxRegime.BUSINESS_INCOME:
            return self.business_income_rate * (1 + self.cess_rate)
        return self.capital_gains_rate * (1 + self.cess_rate)

    @property
    def allows_loss_offset(self) -> bool:
        return self.regime is TaxRegime.BUSINESS_INCOME

    @property
    def tds_applies(self) -> bool:
        return self.regime is TaxRegime.VDA_STRICT

    @property
    def break_even_profit_factor(self) -> float:
        """Gross-wins / gross-losses below which tax exceeds profit.

        Derivation, ignoring fees and TDS. With gross wins W, gross losses L and effective
        rate r, after-tax profit is (W - L) - rW. Setting that to zero:

            W - L = rW   ->   W(1 - r) = L   ->   PF = W/L = 1 / (1 - r)

        Under loss offset the tax is r(W - L), which is positive for any profitable
        strategy, so the floor is 1.0. Fees, spread, slippage and TDS all push the real
        threshold above whatever this returns.
        """
        if self.allows_loss_offset:
            return 1.0
        return 1.0 / (1.0 - self.effective_tax_rate)

    def fee_bps(self, is_taker: bool) -> float:
        return self.taker_fee_bps if is_taker else self.maker_fee_bps

    # ── per-trade frictions ────────────────────────────────────────────────────────
    def execution_cost(self, trade: Trade) -> float:
        """Fees, slippage and half-spread on both legs, in currency."""
        fees = (
            trade.entry_notional * self.fee_bps(trade.entry_taker)
            + trade.exit_notional * self.fee_bps(trade.exit_taker)
        ) / 1e4
        friction = trade.turnover * (self.slippage_bps + self.half_spread_bps) / 1e4
        return fees + friction

    def tds(self, trade: Trade) -> float:
        """s.194S levy. Charged on the *disposal notional*, not on profit.

        This is why turnover matters independently of edge: a strategy that round-trips
        100 times a year pays roughly 1% of notional 100 times, whether or not it made
        money. It is creditable against the final tax bill, but it leaves the account
        immediately, so it compounds against you all year.
        """
        if not self.tds_applies:
            return 0.0
        return trade.exit_notional * self.tds_rate

    def net_of_costs(self, trade: Trade) -> float:
        """PnL after execution frictions and funding, before any tax."""
        return trade.gross_pnl + trade.funding_pnl - self.execution_cost(trade)

    def taxable_base(self, trade: Trade) -> float:
        """The amount the tax rate is applied to for this trade."""
        base = trade.gross_pnl
        if self.fees_deductible:
            base -= self.execution_cost(trade)
        # Funding received is income under any reading, so it always increases the base.
        # Funding paid reduces it only where deductions are allowed — the conservative
        # reading of s.115BBH is that they are not, which means you can be taxed on price
        # gains while separately bearing the funding that consumed them.
        if trade.funding_pnl > 0 or self.funding_deductible:
            base += trade.funding_pnl
        return base


@dataclass(frozen=True)
class CostedResult:
    """The four numbers, and everything needed to argue with them."""

    regime: TaxRegime
    cost_model_version: str
    n_trades: int
    turnover: float

    gross_pnl: float          # 1. price PnL only
    execution_costs: float
    funding_pnl: float
    net_of_costs: float       # 2. after fees, spread, slippage, funding
    tds_withheld: float       # cash removed during the year
    tds_credited: float       # offset against the final bill
    tds_unrecovered: float    # permanent loss where no credit is available
    tds_carry_cost: float     # opportunity cost of capital idle until refund
    #: Mid-year cash position after the levy leaves and before any credit is applied.
    #: This is NOT a step in the four-number ladder — see `four_numbers`. It matters for
    #: margin and sizing during the year, because the money is genuinely gone until the
    #: refund is assessed.
    net_of_tds: float
    tax_total: float          # gross liability, before TDS credit
    tax_payable: float        # liability net of TDS credit
    net_after_tax: float      # 4. what you actually keep

    gross_wins: float
    gross_losses: float
    n_wins: int
    profit_factor_gross: float
    profit_factor_net: float
    break_even_profit_factor: float
    #: Defaulted so the zero-trade construction below stays positional. Zero wins and zero
    #: losses is exactly the UNDEFINED state, so the defaults are the correct answer.
    net_wins: float = 0.0
    net_losses: float = 0.0

    @property
    def profit_factor_gross_state(self) -> ProfitFactorState:
        return profit_factor_state(self.gross_wins, self.gross_losses)

    @property
    def profit_factor_net_state(self) -> ProfitFactorState:
        return profit_factor_state(self.net_wins, self.net_losses)

    @property
    def clears_break_even(self) -> bool:
        """Unbounded clears any finite floor. Undefined clears nothing — a run that made
        no trades did not beat the tax regime, it declined to play."""
        state = self.profit_factor_gross_state
        if state is ProfitFactorState.UNBOUNDED:
            return True
        if state is ProfitFactorState.UNDEFINED:
            return False
        return self.profit_factor_gross >= self.break_even_profit_factor

    @property
    def win_rate(self) -> float:
        return self.n_wins / self.n_trades if self.n_trades else 0.0

    @property
    def total_tax_burden(self) -> float:
        """Everything the tax system permanently removes."""
        return self.tax_total + self.tds_unrecovered + self.tds_carry_cost

    @property
    def realised_tax_rate(self) -> float:
        """Permanent tax burden as a share of pre-tax profit.

        Exceeds 100% when the trap bites, and is undefined-but-still-payable when the
        strategy lost money — which is the point: under s.115BBH a losing year still
        generates a tax bill, because every winning disposal is assessed on its own.
        """
        if self.net_of_costs <= 0:
            return float("inf") if self.total_tax_burden > 0 else 0.0
        return self.total_tax_burden / self.net_of_costs

    @property
    def taxed_while_losing(self) -> bool:
        """The signature of the no-offset regime: a tax bill on a losing year."""
        return self.net_of_costs <= 0 and self.tax_total > 0

    @property
    def survives_tax(self) -> bool:
        return self.net_after_tax > 0

    def four_numbers(self) -> list[tuple[str, float]]:
        """The reporting ladder every strategy result must carry. Monotonically worse.

        Note the ordering. The architecture report originally specified
        `gross -> net of costs -> net of TDS -> net of tax`, which assumed TDS was an
        additive cost. It is not: it is a creditable prepayment, so inserting it as an
        intermediate step produces a ladder whose last rung is *higher* than its
        third — the credit comes back. TDS belongs at the end, as the drag it actually
        is: the carry on idle capital plus anything that cannot be recovered.

        `net_of_tds` is still available separately as the mid-year cash position, which
        is what matters for margin and position sizing during the year.
        """
        return [
            ("gross", self.gross_pnl),
            ("net of costs", self.net_of_costs),
            ("net of tax", self.net_of_costs - self.tax_total),
            ("net of TDS drag", self.net_after_tax),
        ]


def apply(trades: list[Trade], model: CostModel) -> CostedResult:
    """Run a trade list through the cost model and produce the four numbers.

    The tax step is where the regimes diverge, and the divergence is the whole point:

    * Under the VDA regimes tax is assessed **per transaction**. Every winning disposal is
      taxed; every losing one yields nothing. Losses are not merely un-carried-forward,
      they are un-set-off *against other VDA gains in the same year*.
    * Under business income, tax is assessed on **net** income, so losses do their normal
      job of reducing the bill.
    """
    if not trades:
        return CostedResult(
            model.regime, model.version, 0, 0.0,
            0.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 0, 0.0, 0.0, model.break_even_profit_factor,
        )

    gross = sum(t.gross_pnl for t in trades)
    exec_costs = sum(model.execution_cost(t) for t in trades)
    funding = sum(t.funding_pnl for t in trades)
    tds_withheld = sum(model.tds(t) for t in trades)
    turnover = sum(t.turnover for t in trades)

    net_costs = gross + funding - exec_costs
    net_tds = net_costs - tds_withheld     # the cash position mid-year, before any credit

    bases = [model.taxable_base(t) for t in trades]
    if model.allows_loss_offset:
        # Netted: losses reduce the bill. Zero tax on a losing year.
        tax_total = max(sum(bases), 0.0) * model.effective_tax_rate
    else:
        # Per transaction: only the winners are assessed. This is the trap, and it is why
        # a losing year can still owe tax.
        tax_total = sum(b for b in bases if b > 0) * model.effective_tax_rate

    # TDS is a PREPAYMENT of tax, not an additional tax. Treating it as additive
    # double-counts it. It is credited against the liability, and any excess is refunded
    # after the return is assessed. What is permanently lost is the unrecoverable portion
    # plus the carry on capital idle until the refund lands.
    if model.tds_recoverable:
        tds_credited = min(tds_withheld, tax_total)
        tds_unrecovered = 0.0
        idle = tds_withheld
    else:
        tds_credited = 0.0
        tds_unrecovered = tds_withheld
        idle = 0.0
    tds_carry_cost = idle * model.capital_carry_rate * model.tds_refund_lag_years

    tax_payable = tax_total - tds_credited
    net_after_tax = net_costs - tax_total - tds_unrecovered - tds_carry_cost

    wins = [t for t in trades if model.net_of_costs(t) > 0]
    gross_wins = sum(t.gross_pnl for t in trades if t.gross_pnl > 0)
    gross_losses = -sum(t.gross_pnl for t in trades if t.gross_pnl < 0)
    net_wins = sum(v for t in trades if (v := model.net_of_costs(t)) > 0)
    net_losses = -sum(v for t in trades if (v := model.net_of_costs(t)) < 0)

    def ratio(numerator: float, denominator: float) -> float:
        return numerator / denominator if denominator > 0 else float("inf")

    return CostedResult(
        regime=model.regime,
        cost_model_version=model.version,
        n_trades=len(trades),
        turnover=turnover,
        gross_pnl=gross,
        execution_costs=exec_costs,
        funding_pnl=funding,
        net_of_costs=net_costs,
        tds_withheld=tds_withheld,
        tds_credited=tds_credited,
        tds_unrecovered=tds_unrecovered,
        tds_carry_cost=tds_carry_cost,
        net_of_tds=net_tds,
        tax_total=tax_total,
        tax_payable=tax_payable,
        net_after_tax=net_after_tax,
        gross_wins=gross_wins,
        gross_losses=gross_losses,
        n_wins=len(wins),
        profit_factor_gross=ratio(gross_wins, gross_losses),
        profit_factor_net=ratio(net_wins, net_losses),
        break_even_profit_factor=model.break_even_profit_factor,
        net_wins=net_wins,
        net_losses=net_losses,
    )


def compare_regimes(trades: list[Trade], base: CostModel) -> dict[TaxRegime, CostedResult]:
    """Run the same trades through every regime.

    A strategy that survives only under the permissive reading of an unsettled tax question
    is not a strategy, it is a bet on the tax question. Reporting all three makes that
    visible instead of letting the default hide it.
    """
    return {
        regime: apply(trades, replace(base, regime=regime))
        for regime in TaxRegime
    }


def funding_pnl(
    direction: int, qty: float, events: list[tuple[int, float, float]]
) -> float:
    """Cash flow from perpetual funding while a position is open.

    `events` is (ts_ms, funding_rate, mark_price). Sign convention, which is the part
    everyone gets wrong: when the rate is **positive, longs pay shorts**. So a long
    position with positive funding produces a negative cash flow.

    Returns a signed amount: positive = received.
    """
    return sum(-direction * qty * mark * rate for _ts, rate, mark in events)


def required_profit_factor(
    model: CostModel,
    n_trades: int,
    *,
    qty: float = 0.1,
    price: float = 50_000.0,
    loss_move: float = 0.01,
    win_rate: float = 0.5,
) -> float:
    """The profit factor a strategy must actually clear, for a given trading intensity.

    `break_even_profit_factor` is the closed-form floor from tax alone. It understates the
    real threshold because fees, spread, slippage and the carry on withheld TDS are charged
    per round trip regardless of how much the price moved.

    The requirement is **scale-invariant in the number of trades** — with a fixed notional
    per trade, doubling `n_trades` doubles gross PnL, costs, TDS and tax alike. The axis
    that actually matters is `loss_move`: the size of a typical adverse excursion relative
    to the ~17 bps round-trip friction. A scalper working 0.2% moves pays a fifth of its
    edge in frictions before tax; a trend follower working 5% moves pays a thirtieth.

    Solved by bisection on the winner size that drives after-tax PnL to zero.
    """
    def net_at(win_move: float) -> float:
        wins = round(n_trades * win_rate)
        trades = [
            Trade(i * 86_400_000, i * 86_400_000 + 3_600_000, 1, qty, price,
                  price * (1 + (win_move if i < wins else -loss_move)))
            for i in range(n_trades)
        ]
        return apply(trades, model).net_after_tax

    low, high = loss_move, loss_move * 200.0
    if net_at(high) <= 0:
        return float("inf")  # no winner size rescues this intensity
    for _ in range(80):
        mid = (low + high) / 2
        if net_at(mid) > 0:
            high = mid
        else:
            low = mid
    win_move = high
    wins = round(n_trades * win_rate)
    losses = n_trades - wins
    return (wins * win_move) / (losses * loss_move) if losses else float("inf")
