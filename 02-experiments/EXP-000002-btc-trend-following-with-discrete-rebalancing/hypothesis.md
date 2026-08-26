---
id: EXP-000002
type: experiment
title: BTC trend following with discrete rebalancing
created: 2026-08-24
predecessor: EXP-000001
tags: [trend, momentum, positive-skew, discrete-rebalancing]
---

# Hypothesis

## Claim

BTC perpetual futures exhibit **time-series momentum at a multi-week horizon**: the sign of
the return over a trailing window predicts the sign of the return over the following weeks,
with a payoff profile of **few large winners and many small losers**.

## Mechanism

Who is forced to trade, and why? Three flows, all mechanical rather than sentimental:

1. **Capital allocation is slow.** A decision to allocate to crypto — an ETF creation
   programme, a treasury allocation, a fund mandate change — is executed over weeks, not
   instantly, because size cannot be absorbed at once. The buyer of the first tranche is
   the same buyer of the tenth, and their remaining demand is knowable from their earlier
   demand.
2. **Leverage mechanically extends moves.** In perpetual markets a sustained move triggers
   liquidation cascades that force positions to close *in the direction of the move*. That
   is the clearest possible instance of forced flow: the liquidated party has no choice
   about the price they accept or the timing.
3. **Volatility targeting leans with trends.** Systematic participants size inversely to
   realised volatility. Volatility falls as a trend matures, so their position size grows —
   adding flow in the trend's direction without any new view being formed.

Every one of these is a *structural* reason for continuation. None requires anyone to be
irrational, which is what distinguishes it from a pattern found by searching.

## Why this shape, specifically

[[LRN-000002]] established that the Indian VDA tax code selects a strategy shape, not just a
horizon. Trend following is the profile the code rewards: **low win rate, large winners,
few taxable events**. EXP-000001 died partly because mean reversion is the opposite shape.

This produces a design constraint that would be strange in another jurisdiction:
**position changes must be discrete and infrequent**. Textbook time-series momentum
rebalances continuously to a volatility target; here, continuous rebalancing is a tax cost
paid on every adjustment. So the position changes only when the **signal flips**, never on
volatility drift. Turnover is a first-class design variable, not a by-product.

## Falsifier

Named now, before any result exists. Any one of these kills the hypothesis:

1. **No monotonic relationship** between trailing-window return and forward return, across
   the declared lookback grid. If the effect exists only at one lookback, it is a fluke of
   that lookback.
2. **Profit factor below 1.63** — the threshold at this holding horizon from [[LRN-000001]].
3. **Edge dies at 2× modelled costs.**
4. **Post-tax return negative under `VDA_STRICT`.** A result that survives only under the
   permissive reading is contingent, not passed — see [[ADR-012]].
5. **Any single calendar year contributes more than 50% of PnL.** Crypto's 2020–21 bull
   run is more than capable of manufacturing a trend result on its own.
6. **Realised turnover above roughly 50 round trips per year.** That would place it at a
   horizon where the required profit factor is materially higher, meaning it is not the
   strategy described here.

## Universe and period

**BTCUSDT-PERP.BINANCE.** Decisions taken daily at **00:00 UTC** — not IST; this machine
runs Asia/Kolkata at +05:30, which is not even a whole number of hours off.

| Variant | Period | Reason |
|---|---|---|
| **Primary** (price only) | 2020-01-01 → 2026-07-31 | full span where perp klines and funding exist |
| **Secondary** (open-interest confirmed) | 2020-09-01 → 2026-07-31 | open interest does not exist earlier |

Both windows are declared now. Changing either after seeing a result is selection bias;
narrowing because data is absent is a capacity constraint, and the distinction is only
auditable if the declaration precedes the result.

## Declared parameter grid

The grid is fixed **in advance** so the trial count is bounded and the multiple-testing
penalty is honest. Searching outside it is permitted but must be recorded as an overage,
which inflates the penalty rather than hiding it.

| Parameter | Values | Trials |
|---|---|---|
| `lookback_days` | 30, 60, 90, 120, 180 | 5 |
| Secondary: OI-confirmed variant at the winning lookback | — | 1 |
| Probes: look-ahead (bars **and** funding), cost ladder 1×/1.5×/2×/3× | — | 6 |
| Reserve for re-runs after defects | — | 8 |
| **Total budget** | | **20** |

Everything else — entry threshold, exit rule, sizing — is **fixed before the first trial**
and stated in `config.yaml`. They are not free parameters and will not be searched.

## Precondition on the falsification battery

[[LRN-000002]] recorded that a shift-probe is only informative if the shifted quantity is
the signal source. This experiment's signal is derived from **price**, so the existing
bar-shift probe is directly meaningful here — unlike in EXP-000001. The funding-shift
variant is still required before the secondary OI-confirmed variant can be falsified, since
that one consumes a second input series.
