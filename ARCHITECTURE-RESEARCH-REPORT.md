# PROJECT SUPERCORTEX — Architecture Research Report

**Prepared for:** Anirudh Tumuluru (CIO)
**Date:** 2026-08-24
**Status:** RESEARCH COMPLETE — AWAITING APPROVAL. Nothing installed. Nothing built.
**Research method:** Primary sources only — GitHub API (licence, activity, releases), official
documentation (code.claude.com, nautilustrader.io), official licence files read byte-for-byte.
Every claim marked `[V]` was verified this session. Anything I could not verify is marked
`UNVERIFIED`.

---

## SECTION 0 — THE THREE THINGS THAT CHANGE EVERYTHING

Before any architecture. I found three facts that matter more than every tool choice in this
document combined. If you read nothing else, read this section.

### 0.1 Your machine cannot hold the data you are imagining

I measured it `[V]`:

```
Apple M4 · 10 cores · 16 GB RAM · macOS 26.5.2
Disk: 228 GB total — 193 GB used — 7.2 GB FREE (97% full)
```

**Analogy:** You have designed a warehouse operation and you have a shoebox.

What fits in 7.2 GB:
- BTCUSDT 1-minute bars, 8 years: ~4M rows → **~150 MB** Parquet. Fine.
- 20 crypto symbols, 1m bars + funding + open interest, 8 years → **~4 GB**. Just barely fits.

What does NOT fit:
- Level-2 order book deltas for **one** symbol for **one** month: **10–40 GB**. Impossible.
- Full tick/trade data across 20 symbols: **hundreds of GB**. Impossible.

**Decision this forces:** Phase 1 is a **bar-and-funding desk**, not a tick desk. Order books,
microstructure, and market-making research are **deferred until an external SSD exists**. A 2 TB
external NVMe over Thunderbolt is ~₹12,000–18,000 and is the single highest-leverage purchase
you can make. Until then, any agent that proposes an order-book strategy must be rejected by the
Data Director on capacity grounds, automatically.

This is not a small caveat. Half the strategy space in your brief (microstructure, execution
alpha, market making) is closed to you until this is fixed.

### 0.2 "Crypto + Forex" is not legally one business in India

You explicitly asked me not to assume venue access. Good instinct — here is what I found.

**Forex.** Trading spot FX or CFDs with offshore brokers (OANDA, FXCM, IC Markets, MT4/MT5 brokers)
is **a FEMA violation for an Indian resident**, regardless of how well-regulated the broker is
elsewhere. The RBI maintains an "Alert List" of unauthorised forex platforms — being on it does not
mean the broker is a scam, it means *you* commit an offence by using it. Penalties under FEMA can
reach **3× the transaction value**. Legal FX for an Indian resident means **exchange-traded currency
derivatives on NSE/BSE/MSE through a SEBI-registered broker**, limited to 7 pairs (USD/INR, EUR/INR,
GBP/INR, JPY/INR, and cash-settled EUR/USD, GBP/USD, USD/JPY).
[Sources: RBI Alert List coverage; FEMA 1999 commentary — see Section 40 sources.]

Under the **Liberalised Remittance Scheme**, Indian residents may invest abroad up to **USD 250,000/yr**
but LRS does **not** permit leveraged products — no offshore futures, options, or margin FX. So the
Interactive Brokers path gives you global *equities and ETFs*, not the FX book you were imagining.

**Crypto.** Legal to trade and tax-recognised, but the tax design is hostile to systematic trading:
- **30% flat tax** on gains from Virtual Digital Assets, plus cess.
- **No set-off of losses** against other VDA gains or any other income. No carry-forward.
- **No deduction of expenses** other than cost of acquisition. Your data costs, your electricity,
  your exchange fees — not deductible.
- **1% TDS under s.194S** on transfers above the threshold.
- From **1 April 2026**, the Income Tax Act 2025 requires reporting of **every trade, conversion and
  disposal** — not net annual gains.

**Analogy:** Imagine a casino where you pay 30% on every winning hand, you get nothing back for
losing hands, and the house takes 1% every time you push chips across the table.

**What this does to strategy design — and this is the whole point:**

| Strategy profile | Round-trips/yr | TDS drag on turnover | Viable in India? |
|---|---|---|---|
| Market making / HFT | 10,000+ | catastrophic | **No** |
| Intraday momentum | 500–2,000 | ~5–20% of capital/yr | **No** |
| Multi-day swing | 50–200 | ~0.5–2% | Marginal |
| Weekly/monthly trend, carry, funding-harvest | 12–50 | ~0.1–0.5% | **Yes** |

**Decision this forces:** Supercortex must be a **low-turnover, medium-horizon research desk**.
The 30% / no-loss-offset / 1% TDS structure must be a **first-class cost model in the backtester
from day one**, not an afterthought at tax time. A strategy that is profitable pre-tax and
unprofitable post-tax must be *killed by the falsification engine automatically*.

> I am not qualified to give legal or tax advice and this is not legal advice. These are
> technical risk flags. Before a single rupee of live capital moves, engage **a chartered
> accountant with VDA experience** and, for anything FX-related, **FEMA counsel**. Section 19
> defines exactly where those gates sit.

### 0.3 An agent swarm is a p-hacking machine unless you build the registry first

This is the deepest architectural point in this report.

The Deflated Sharpe Ratio, the Probability of Backtest Overfitting, White's Reality Check — every
serious defence against false discovery requires one number: **how many strategies did you actually
try?** Not how many you kept. How many you *ran*.

A human researcher runs 50 backtests a month and can roughly remember them. **A swarm of agents can
run 50,000 and remember none.** If your registry does not capture every single run — including the
ones an agent abandoned mid-thought, including the parameter sweeps, including the "let me just
quickly check" runs — then your Deflated Sharpe Ratio is not conservative, it is *fiction*, and it
is fiction that will feel rigorous.

**Analogy:** A swarm of agents is a machine that finds four-leaf clovers. Point it at a field and it
will bring you a hundred. That does not mean the field is lucky — it means the machine is fast. The
only way to know if a clover is special is to count how many blades of grass were checked. If the
machine does not count, the clovers are worthless.

**Decision this forces:** The experiment registry is not a "nice to have for Phase 6". It is
**Phase 1 infrastructure**, and no backtest may run except through a wrapper that registers it
first. This inverts the roadmap in your brief: **you build the counting machine before you build
the searching machine.**

---

## SECTION 1 — EXECUTIVE SUMMARY

**The system in one sentence:** A single Mac Mini runs one Rust-cored trading engine
(NautilusTrader) over a folder of Parquet files, with an SQLite ledger that counts every experiment,
a Markdown vault that holds every judgment, and Claude Code subagents that do the reading, writing
and arguing — with all irreversible actions gated behind operating-system-enforced hooks and
human approval.

### What we should use

| Layer | Choice | Why in one line |
|---|---|---|
| Quant engine | **NautilusTrader** (LGPL-3.0) | Same strategy code runs backtest and live — kills the #1 source of live/backtest divergence |
| Research storage | **Parquet** via Nautilus `ParquetDataCatalog` | Already built into the engine; no second copy of the truth |
| Analytics | **DuckDB** + **Polars** | Query the same Parquet in place; zero ETL, zero daemon |
| Metadata / registry | **SQLite** | One file, ACID, no server, git-diffable dumps |
| Second brain | **Markdown + Git + Obsidian** | Human-readable and AI-readable are the same bytes |
| Orchestration | **Claude Code subagents + `launchd` + plain Python** | Depth-3 nesting maps exactly onto CIO→Director→Worker |
| Enforcement | **Claude Code `PreToolUse` hooks + `permissions.deny`** | The only agent controls that are actually enforced |
| Stats | **statsmodels, `arch`, scipy, scikit-learn** | `arch.bootstrap` ships SPA/RealityCheck/StepM/MCS `[V]` |
| Env | **uv** + native macOS + `launchd` | No Docker on a 16 GB box |
| Secrets | **macOS Keychain**, live keys never reachable by an agent | Simplest thing that is actually secure |

### What we should NOT use

- **MLflow / W&B / ClearML / Aim** — a server + artifact store + backend DB to replace one SQLite table.
- **PostgreSQL, ClickHouse, TimescaleDB, QuestDB, InfluxDB, Iceberg, Delta Lake, LanceDB** — every one
  is a daemon you must feed, back up and upgrade, solving a problem you do not have at 4 GB of data.
- **A vector database / embeddings** — `ripgrep` over 2,000 Markdown files is instant and transparent.
  Revisit at ~5,000 notes.
- **LangGraph / Temporal / Prefect / Dagster / Airflow / Ray / CrewAI / AutoGen** — you have Claude
  Code subagents and `launchd`. Adding an orchestration framework now buys nothing and costs a stack.
- **Prometheus + Grafana + Loki** — in Phase 1. A monitoring stack to watch two strategies is the
  single biggest overengineering trap in your brief.
- **vectorbt** — licence risk (Commons Clause `[V]`) plus it is a *second* engine, i.e. a second truth.
- **mlfinlab** — **not open source** `[V]`. Proprietary licence agreement, repo dormant since Oct 2023.
- **Docker / OrbStack** — on a 16 GB Mac with zero server processes, containers are pure overhead.
  (Also: OrbStack Pro is $8/user/mo for *commercial* use `[V]` — a trading business is commercial.)
- **empyrical, pyfolio, alphalens** — all effectively dead `[V]` (last releases 2019–2020).
- **400 permanent agents.** Twelve roles, spawned on demand, is the correct number.

### What we should build ourselves

Only five things. Everything else is bought or borrowed.

1. **The Experiment Registry** — the counting machine from §0.3.
2. **The Falsification Engine** — an 11-test battery whose job is to kill your ideas.
3. **The Strategy Lifecycle state machine** — promotion gates that code enforces, not discipline.
4. **The India Cost Model** — 30% / no-offset / 1% TDS / fees / funding / slippage, as a backtest cost.
5. **The Agent Governance layer** — hooks, permission tiers, and the anti-fabrication validator.

### What we should postpone

Live trading (Phase 9+, after ≥3 months paper), monitoring stack (Phase 8), cloud/VPS (Phase 9),
vector search (never, until proven necessary), order-book research (until external SSD), forex
(until §0.2 is legally resolved), and the full 12-agent hierarchy (Phase 3 — *after* you have one
validated strategy, not before).

### The honest summary

Your brief describes a system that would take a funded team 18 months. What one person plus Claude
Code can actually build and *operate* is smaller and better: **a disciplined, low-turnover,
tax-aware crypto research desk with an unusually good falsification process.** The competitive edge
here is not the agent swarm — it is that the swarm is pointed at a registry that refuses to let it
lie. Build that, and the rest is assembly.

---

## SECTION 2 — THE FINAL REFERENCE ARCHITECTURE

```
                          ┌─────────────────────────┐
                          │   YOU (CIO / operator)  │
                          │  approvals · capital ·  │
                          │  strategy kill switch   │
                          └───────────┬─────────────┘
                                      │
        ┌─────────────────────────────┴──────────────────────────────┐
        │                                                            │
        ▼  HUMAN COCKPIT                              AI WORKBENCH ▼
┌───────────────────┐                            ┌────────────────────────┐
│     OBSIDIAN      │◄── same bytes, no sync ───►│      CLAUDE CODE       │
│  Bases dashboards │                            │  subagents · skills    │
│  graph · search   │                            │  hooks · CLAUDE.md     │
└─────────┬─────────┘                            └───────────┬────────────┘
          │                                                  │
          └────────────────────┬─────────────────────────────┘
                               ▼
            ┌──────────────────────────────────────┐
            │   MARKDOWN SECOND BRAIN  (in Git)    │   ← JUDGMENT lives here
            │  hypotheses · decisions · learnings  │
            │  ADRs · post-mortems · agent notes   │
            └──────────────────┬───────────────────┘
                               │ frontmatter mirrors ▼ (one-way, generated)
            ┌──────────────────────────────────────┐
            │      registry.sqlite  (in Git)       │   ← FACTS live here
            │  EXP · STR · DEC · LRN · DATA · DEP  │
            │  every run · every trial · lineage   │
            └──────────────────┬───────────────────┘
                               │
    ┌──────────────────────────┼───────────────────────────┐
    ▼                          ▼                           ▼
┌─────────┐          ┌──────────────────┐        ┌──────────────────┐
│ RESEARCH│          │  FALSIFICATION   │        │   COST MODEL     │
│ Polars  │          │  ENGINE (custom) │        │ fees · funding   │
│ DuckDB  │          │ 11 kill-tests    │        │ slippage · spread│
│ arch    │          │ DSR · PBO · SPA  │        │ 30% tax · 1% TDS │
└────┬────┘          └────────┬─────────┘        └────────┬─────────┘
     │                        │                           │
     └────────────────────────┼───────────────────────────┘
                              ▼
            ┌──────────────────────────────────────┐
            │          NAUTILUSTRADER              │
            │  ONE engine · Rust core · ns clock   │
            │  ┌────────────┐    ┌──────────────┐  │
            │  │  BACKTEST  │ == │ PAPER / LIVE │  │  same strategy class
            │  └─────┬──────┘    └──────┬───────┘  │
            └────────┼──────────────────┼──────────┘
                     │                  │
                     ▼                  ▼
     ┌────────────────────────┐   ┌──────────────────────┐
     │ ParquetDataCatalog     │   │    RISK ENGINE       │  ← NON-BYPASSABLE
     │ (DataFusion-backed)    │   │ size · leverage · DD │
     │ bars·funding·OI·quotes │   │ kill switch · limits │
     └──────────┬─────────────┘   └──────────┬───────────┘
                │                            │  approve / REJECT
     ┌──────────▼─────────────┐              ▼
     │  RAW PARQUET LAKE      │   ┌──────────────────────┐
     │  data.binance.vision   │   │  EXECUTION ADAPTER   │
     │  ccxt gap-fill         │   │  Binance · Bybit ... │
     │  (external SSD)        │   └──────────┬───────────┘
     └────────────────────────┘              ▼
                                  ┌──────────────────────┐
                                  │  VENUE (FIU-reg.)    │
                                  │  testnet → live      │
                                  └──────────┬───────────┘
                                             ▼
                                  ┌──────────────────────┐
                                  │ RECONCILE + MONITOR  │
                                  │ fills vs expected    │
                                  └──────────┬───────────┘
                                             ▼
                                  ┌──────────────────────┐
                                  │ POST-TRADE ANALYSIS  │
                                  │ live vs backtest     │
                                  └──────────┬───────────┘
                                             │
                                             └──► back to SECOND BRAIN (LRN-xxxx)
```

### Every component, explained simply

| Component | Analogy | What it actually is |
|---|---|---|
| **Obsidian** | The office noticeboard | A viewer over the Markdown folder. Deletable without data loss. |
| **Markdown vault** | The lab notebooks | Prose: why we believed something, what we decided, what we learned. |
| **registry.sqlite** | The ledger the auditor reads | Every experiment, every trial, every metric. Machine-written only. |
| **Claude Code** | The management layer | Reads/writes the vault, runs code, spawns specialists. |
| **Hooks** | The locked doors | OS-enforced rules. The *only* agent control that actually binds. |
| **Parquet lake** | The warehouse | Immutable raw market data. Written once, never edited. |
| **ParquetDataCatalog** | The catalogued shelves | Nautilus's own typed index over Parquet. |
| **DuckDB** | The research desk | Queries the warehouse in place. Stores nothing. |
| **NautilusTrader** | The factory machinery | Turns strategy code + data into fills. Identically in test and live. |
| **Falsification engine** | The devil's advocate | Tries to prove every strategy is luck. |
| **Cost model** | The taxman in the room | Fees, funding, slippage, and India's 30%/1% TDS. |
| **Risk engine** | The security guard | Stands between strategy and exchange. Cannot be walked around. |
| **Execution adapter** | The cashier | Talks to the venue. Holds the only credentials in the system. |
| **Reconciliation** | The nightly cash count | Did we get the fills we think we got? |

### The two load-bearing invariants

**Invariant 1 — One engine, one truth.** Strategy logic is written *once*, as a Nautilus `Strategy`
subclass. It is never re-implemented in pandas for research or in another language for production.
The moment you have two implementations you have two behaviours and you will not know which one is
real until it costs you money.

**Invariant 2 — Numbers never pass through an LLM.** Agents write prose and code. Numbers are
produced by deterministic code, written to `metrics.json`, and inserted into reports by a template.
An agent that types a Sharpe ratio into a Markdown file has fabricated it. Section 16.4 makes this
mechanically enforceable.

---

## SECTION 3 — TECHNOLOGY DECISION MATRIX

All licence / stars / activity data pulled from the **GitHub REST API on 2026-08-24** `[V]`.
"Push" = last commit pushed to default branch. Complexity = ops burden for a one-person desk.

### 3.1 Quant engines

| Tool | Licence | Stars | Push | Latest rel. | Mac ARM | Crypto | FX | Backtest | Live | Complexity | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **NautilusTrader** | **LGPL-3.0** | 27,575 | 2026-08-24 | v1.231.0 (2026-08-02) | Official ARM64 wheel | ★★★ | via IB | ★★★ | ★★★ | Med | **WINNER** |
| QuantConnect LEAN | Apache-2.0 | 21,330 | 2026-08-21 | v2.4.0.1 tag (2017) | .NET | ★★ | ★★★ | ★★★ | ★★★ | High | **RUNNER-UP** |
| Freqtrade | GPL-3.0 | 53,575 | 2026-08-24 | 2026.7 | ✅ | ★★★ | ✗ | ★★ | ★★ | Low | Rejected |
| Hummingbot | Apache-2.0 | 19,584 | 2026-08-22 | v2.16.0 | ✅ | ★★★ | ✗ | ★ | ★★★ | Med | Rejected |
| vectorbt | **Apache-2.0 + Commons Clause** | 8,803 | 2026-08-02 | v1.1.0 | ✅ | data-agnostic | – | ★★★ | ✗ | Low | Rejected (licence + 2nd truth) |
| Jesse | MIT | 8,369 | 2026-08-19 | – | ✅ | ★★★ | ✗ | ★★ | ★★ | Low | Rejected |
| Qlib | MIT | 47,889 | 2026-07-23 | v0.9.7 (2025-08) | ✅ | ✗ | ✗ | ★★ | ✗ | High | Rejected (equities/ML) |
| Backtrader | GPL-3.0 | 22,942 | **2024-08-19** | none | ✅ | ★ | ★ | ★★ | ★ | Low | **Unmaintained** |
| Zipline-reloaded | Apache-2.0 | 1,927 | 2026-01-06 | 3.1.1 | ✅ | ✗ | ✗ | ★★ | ✗ | Med | Rejected (US equities) |
| bt | MIT | 2,964 | 2026-08-07 | v1.2.0 | ✅ | – | – | ★ | ✗ | Low | Rejected (allocation only) |

**WINNER: NautilusTrader. WHY:**
1. **Backtest and live are the same code path.** Every other option makes you rewrite for production.
   This single property eliminates the most expensive class of bug in systematic trading.
2. **19 stable integrations** `[V]` — Binance, Bybit, OKX, Coinbase, Kraken, Deribit, BitMEX,
   Hyperliquid, dYdX, Derive, Lighter, Interactive Brokers, plus Databento/Tardis data adapters.
3. **It already contains the data layer.** `ParquetDataCatalog` is a Rust/DataFusion-backed Parquet
   store with first-class types for **funding rates, mark prices, open interest, order book deltas** `[V]`.
   That deletes an entire category of decision from your brief.
4. **Nanosecond event clock and a real matching engine** — the difference between an honest backtest
   and a flattering one.
5. Official **macOS ARM64 wheels, Python 3.12–3.14** `[V]`. Installs with `uv pip install nautilus_trader`.

**RUNNER-UP: LEAN.** More permissive licence and a superb data/venue ecosystem, but C#-first,
heavier, and its gravity pulls you toward QuantConnect's cloud — the opposite of local-first.
Revisit only if you ever need deep equities/options coverage.

### 3.2 Data & storage

| Tool | Licence | Push | Role considered | Verdict |
|---|---|---|---|---|
| **Parquet / Arrow** | Apache-2.0 | active | Raw + canonical storage | **ADOPT** |
| **DuckDB** | MIT | 2026-08-24 | Analytics over Parquet | **ADOPT** |
| **Polars** | MIT | 2026-08-24 | DataFrame engine | **ADOPT** |
| **SQLite** | Public domain | stdlib | Registry / metadata | **ADOPT** |
| pandas | BSD-3 | 2026-08-24 | Interop only | Tolerate (Polars preferred) |
| ClickHouse | Apache-2.0 | 2026-08-24 | Tick warehouse | Reject — daemon, RAM-hungry |
| TimescaleDB | **Apache-2.0 core + Timescale Licence (TSL)** `[V]` | 2026-08-24 | Time-series DB | Reject — dual-licence + Postgres to run |
| QuestDB | Apache-2.0 | 2026-08-24 | Time-series DB | Reject — another daemon |
| InfluxDB 3 | Apache-2.0 | 2026-08-20 | Time-series DB | Reject |
| Iceberg / Delta Lake | Apache-2.0 | active | Table format | Reject — solves multi-writer problems you don't have |
| LanceDB | Apache-2.0 | 2026-08-24 | Vector store | Defer — see §7.6 |
| PostgreSQL | PostgreSQL Licence | active | Operational DB | Reject in Phase 1–8 |
| Redis 8 | **RSALv2 / SSPLv1 / AGPLv3 tri-licence** `[V]` | 2026-08-23 | Nautilus cache backend | Use **Valkey** instead |
| **Valkey** | BSD-3-Clause `[V]` | 2026-08-24 | Redis-protocol fork | **ADOPT IF NEEDED** (Phase 9) |
| Feast | Apache-2.0 | 2026-08-22 | Feature store | Reject — Parquet + DuckDB *is* your feature store |

**WINNER: Parquet + DuckDB + Polars + SQLite. WHY:** Four components, zero daemons, zero backups
beyond `cp` and `git`, and every one is a file you can inspect. At your data scale (~4 GB), DuckDB
on a Parquet folder outperforms any client/server database *and* costs nothing to operate. The
moment you add a database server you have added a process to monitor, a port to secure, a backup to
schedule and an upgrade to fear — for a query that already returns in 40 ms.

### 3.3 Research, statistics & validation

| Tool | Licence | Push | Role | Verdict |
|---|---|---|---|---|
| NumPy / SciPy | BSD-3 | active | Numerics | **ADOPT** |
| statsmodels | BSD-3 | 2026-08-24 | Econometrics, tests | **ADOPT** |
| **`arch`** (Kevin Sheppard) | Permissive custom `[V]` | 2026-08-10 | **SPA, RealityCheck, StepM, MCS** `[V]` | **ADOPT — critical** |
| scikit-learn | BSD-3 | 2026-08-24 | ML + CV primitives | **ADOPT** |
| Optuna | MIT | 2026-08-21 | Hyperparameter search (SQLite storage) | **ADOPT (Phase 6)** |
| QuantStats | Apache-2.0 | 2026-07-20 | Tearsheets | **ADOPT (reporting only)** |
| Riskfolio-Lib | BSD-3 | 2026-08-18 | Portfolio optimisation | **ADOPT (Phase 7)** |
| skfolio | BSD-3 | 2026-08-23 (v1.0.0!) | sklearn-native portfolio opt | **ADOPT (Phase 7)** — prefer over PyPortfolioOpt |
| CVXPY | Apache-2.0 | 2026-08-24 | Convex optimisation | Adopt if constraints get real |
| PyPortfolioOpt | MIT | 2026-07-07 | Portfolio opt | Runner-up to skfolio |
| SHAP | MIT | 2026-08-11 | Model explanation | Defer to Phase 6 |
| PyMC | permissive | 2026-08-17 | Bayesian | Defer — high value, high time cost |
| marimo | Apache-2.0 | 2026-08-24 | **Reproducible notebooks** (pure .py, DAG, no hidden state) | **ADOPT over Jupyter** |
| Jupyter | BSD-3 | active | Notebooks | Reject — hidden execution order destroys reproducibility |
| pandera | MIT | 2026-08-23 | Dataframe validation | **ADOPT** (lighter than Great Expectations) |
| Great Expectations | Apache-2.0 | 2026-08-21 | Data validation | Reject — too heavy for one desk |
| **mlfinlab** | **PROPRIETARY** `[V]` — "Copyright Protection Notice and Licensing Agreement", repo dormant since **2023-10-02** | — | López de Prado toolkit | **REJECT — NOT OPEN SOURCE** |
| pypbo | AGPL-3.0, 138★, no releases | 2026-07-06 | PBO/CSCV/DSR | Reject as dependency — **reimplement** (§16) |
| timeseriescv | MIT, **last push 2022-02-15** | — | Purged/embargoed CV | **Vendor the file** (MIT permits), don't depend on it |
| empyrical / pyfolio / alphalens | Apache-2.0 | **dead (2019–2024)** `[V]` | Perf analytics | **REJECT — abandoned** |

**WINNER: statsmodels + `arch` + scipy + scikit-learn. WHY:** The single most important discovery in
this section is that **`arch.bootstrap` exports `SPA`, `RealityCheck`, `StepM` and `MCS`** `[V]` —
Hansen's Superior Predictive Ability test and White's Reality Check, written by the econometrician
who literally wrote the reference implementations, under a permissive licence, still maintained.
Your brief asked for open-source implementations of exactly these. They exist and they are good.

The second most important discovery is the negative one: **mlfinlab is not open source.** It is the
most-recommended package for purged cross-validation and CPCV on the internet, and it is a
proprietary product with a dormant public repo. Anyone who tells you to `pip install mlfinlab` for
this stack is working from stale information. We implement purged/embargoed CV and CPCV ourselves —
it is roughly 150 lines and we need to understand it anyway.

### 3.4 Agents, orchestration & workflow

| Tool | Licence | Push | Verdict |
|---|---|---|---|
| **Claude Code subagents** | Product | current | **ADOPT — the orchestrator** |
| **Claude Code hooks** | Product | current | **ADOPT — the enforcement layer** |
| **`launchd`** | macOS built-in | — | **ADOPT — the scheduler** |
| Claude Code agent teams | Product, **EXPERIMENTAL, off by default** `[V]` | current | **Reject for architecture** — see §9.1 |
| MCP (Python SDK) | MIT | 2026-08-21 | **Adopt narrowly** — see §3.7 |
| Prefect | Apache-2.0 | 2026-08-23 | Defer to Phase 6+, probably never |
| Dagster | Apache-2.0 | 2026-08-22 | Reject — asset model is lovely, ops cost is not |
| Airflow | Apache-2.0 | 2026-08-24 | Reject — enterprise scheduler for a laptop |
| Temporal | MIT | 2026-08-24 | Reject — durable execution you don't need yet |
| Ray / Dask | Apache-2.0 / BSD-3 | active | Reject — 10 cores don't need a cluster scheduler |
| LangGraph | MIT | 2026-08-23 | Reject — you already have an agent runtime |
| CrewAI | MIT | 2026-08-24 | Reject — role-play abstraction over the same API |
| **AutoGen** | **CC-BY-4.0** `[V]` (a *documentation* licence applied to code) + last push **2026-04-15** | — | **Reject — licence smell + stalling** |
| PydanticAI | MIT | 2026-08-24 | Reject now; best choice *if* you ever need a non-Claude agent runtime |

**WINNER: Claude Code native + `launchd` + Python. WHY:** Verified from the official docs `[V]`:
subagents nest **3 levels deep by default** (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`), support **20
concurrent** (`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`), and their YAML frontmatter already provides
`tools`, `disallowedTools`, `model`, `permissionMode`, `maxTurns`, `memory`, `skills`, `hooks` and
`isolation: worktree`. That is a complete agent permission-and-isolation system, for free, in a
Markdown file. Three levels is *exactly* CIO → Director → Worker. You would be replacing a working
runtime with a worse one.

### 3.5 Monitoring

| Tool | Licence | Verdict |
|---|---|---|
| Prometheus | Apache-2.0 | **Adopt Phase 8** |
| Grafana | **AGPL-3.0** `[V]` | **Adopt Phase 8** — AGPL is fine for internal use (no distribution); do not embed in a product |
| VictoriaMetrics | Apache-2.0 | Runner-up to Prometheus; lighter |
| Loki | **AGPL-3.0** `[V]` | Reject — plain log files + `ripgrep` |
| **Sentry** | **FSL-1.1-Apache-2.0** `[V]` — *source-available*, converts to Apache-2.0 after 2 yrs | Reject — not OSI open source |
| OpenTelemetry | Apache-2.0 | Defer |
| **Phase 1–7 alternative** | — | **`health.md` written by `launchd` + `ntfy.sh` push to phone** |

**WINNER (Phase 1–7): a Markdown health file and a phone notification.** WHY: You need to know two
things — "is it running?" and "did something break?". A cron job that writes `00-dashboard/health.md`
and pushes to your phone answers both, in ~40 lines, with zero RAM. Prometheus+Grafana is the right
answer when you have 10 strategies and 3 venues. It is the wrong answer for Phase 1 and it will eat
2 GB of your 16.

### 3.6 Environment, security & DevOps

| Tool | Licence | Verdict |
|---|---|---|
| **uv** | Apache-2.0, 89,024★, 2026-08-23 `[V]` | **ADOPT** — 10–100× faster than pip/poetry, lockfile, Python management |
| Poetry | MIT | Runner-up. uv wins on speed and single-binary simplicity |
| **Native macOS + `launchd`** | — | **ADOPT** |
| Docker Desktop | Proprietary (paid >250 employees) | Reject Phase 1–8 |
| **OrbStack** | Proprietary — **Pro $8/user/mo required for commercial use** `[V]` | Reject |
| colima | MIT | Use *if* a container is ever needed |
| Apple `container` (macOS 26) | Apache-2.0, v1.0 Jun 2026 | Watch — no Compose yet |
| **macOS Keychain** | OS built-in | **ADOPT — primary secret store** |
| 1Password CLI + SDK | Proprietary service, MIT SDK | Adopt if you already pay for 1Password |
| **HashiCorp Vault** | **BUSL-1.1** `[V]` (NOASSERTION on GitHub) | Reject — not open source since 2023, and vast overkill |
| OpenBao | MPL-2.0 `[V]` | The open Vault fork, if you ever need one. Not now. |
| sops | MPL-2.0 | Adopt for encrypting non-live config in Git |
| gitleaks | MIT | **ADOPT — pre-commit secret scan** |
| **Claude Code Bash sandbox** | Product, macOS Seatbelt `[V]` | **ADOPT — filesystem + network isolation for agent shell commands** |

### 3.7 Where MCP belongs — and where it does not

You asked me not to use MCP because it is fashionable. I won't.

**MCP is worth it when** a capability is (a) needed by *many different agents*, (b) better expressed
as a typed tool than as a shell command, and (c) benefits from being permission-gated as a unit.

| Candidate | Use MCP? | Why |
|---|---|---|
| Reading/writing the vault | **No** | Claude Code's native Read/Write/Grep are faster and already permissioned |
| Running backtests | **No** | It's a CLI call. `uv run supercortex backtest EXP-000042` |
| Querying DuckDB | **Maybe, Phase 6** | A typed `query_catalog(sql)` tool with a read-only connection is genuinely safer than letting agents write arbitrary Python against your data |
| **The experiment registry** | **Yes, Phase 3** | Many agents, typed schema, and you want *registration to be impossible to skip* |
| Git | **No** | `gh` and `git` CLIs are better |
| Exchange APIs | **NEVER** | Credentials must never sit behind a tool an agent can call |
| Web/paper research | **No** | Native WebSearch/WebFetch suffice |
| Monitoring | **No** | Read the health file |

**Rule: MCP for read-only, high-frequency, schema-bearing operations. Direct code for everything
that touches money.** Two MCP servers total, both written by us, both Phase 3+. Zero third-party
MCP servers in the trading path — a third-party MCP server is arbitrary code with your filesystem.

---

## SECTION 4 — THE OPEN-SOURCE STACK (FINAL)

```
CORE
  Python 3.12          (Nautilus supports 3.12–3.14; pin 3.12 for wheel breadth)
  uv                   Apache-2.0   env + dependency + lockfile
  Git + GitHub         private repo
  launchd              macOS scheduler

QUANT ENGINE
  NautilusTrader       LGPL-3.0     backtest == live, 19 venue adapters

DATA
  Parquet / Arrow      Apache-2.0   raw + canonical storage
  ParquetDataCatalog   (in Nautilus) typed catalogue: bars, funding, OI, quotes, book
  DuckDB               MIT          analytics over Parquet, in place
  Polars               MIT          dataframes
  SQLite               public domain registry + metadata
  ccxt                 MIT          gap-fill + venue metadata (NOT execution)
  pandera              MIT          schema + quality contracts on every dataset

RESEARCH
  NumPy / SciPy        BSD-3
  statsmodels          BSD-3        econometrics
  arch                 permissive   SPA / RealityCheck / StepM / MCS  ← load-bearing
  scikit-learn         BSD-3
  marimo               Apache-2.0   reproducible notebooks (pure .py)
  QuantStats           Apache-2.0   tearsheets
  Optuna               MIT          Phase 6, SQLite-backed
  skfolio              BSD-3        Phase 7 portfolio construction

AI / AGENTS
  Claude Code          Max subscription
  Subagents (.claude/agents/*.md)      the org chart
  Skills (.claude/skills/*/SKILL.md)   the procedures
  Hooks (.claude/settings.json)        the enforcement
  Bash sandbox (Seatbelt)              agent shell isolation

ORCHESTRATION
  Claude Code subagents  interactive research
  launchd + Python CLI   deterministic recurring jobs
  (NO workflow engine)

RISK / EXECUTION
  Nautilus RiskEngine + custom PortfolioRiskGate
  Nautilus exec adapters: Binance, Bybit, OKX (testnet first)

MONITORING
  Phase 1-7: health.md + ntfy.sh push
  Phase 8+:  Prometheus (Apache-2.0) + Grafana (AGPL-3.0, internal use)

KNOWLEDGE
  Markdown + Git         second brain
  Obsidian + Bases       human cockpit (core plugins only)
  ripgrep                search (NOT a vector DB)

SECURITY
  macOS Keychain         live credentials
  gitleaks               pre-commit secret scan
  sops (MPL-2.0)         encrypted non-live config
  Exchange-side: withdrawal DISABLED + IP allowlist + separate research/live keys

DEVOPS
  pytest, ruff, mypy     BSD/MIT
  pre-commit             MIT
  NO Docker
```

**Total third-party runtime services: zero.** Everything is a file or a library. That is the point.

---

## SECTION 5 — WHAT WE BUILD OURSELVES

Five components. Each is custom because it encodes *your* judgment, and judgment is the only thing
that cannot be `pip install`ed.

### 5.1 The Experiment Registry (`registry.sqlite` + `supercortex` CLI)
**Why custom:** MLflow tracks *model training runs*. You need to track *scientific trials* — with a
trial counter that feeds the Deflated Sharpe Ratio, a data-snapshot hash, a code commit, and a
falsification verdict. No off-the-shelf tracker models "how many times did we look at this data".
It is ~400 lines of Python over SQLite and it is the spine of the whole desk. See §12.

### 5.2 The Falsification Engine
**Why custom:** No open-source package implements a *promotion-blocking battery*. The individual
statistics exist (arch, scipy) but the orchestration — run 11 tests, apply pre-registered
thresholds, produce a verdict that the lifecycle state machine obeys — is yours. See §16.

### 5.3 The Strategy Lifecycle state machine
**Why custom:** Promotion criteria are the expression of your risk appetite. Encoding them in code
converts discipline (which fails at 2 a.m. when a strategy looks amazing) into a constraint. See §13.

### 5.4 The India Cost Model
**Why custom:** No backtester on earth models 30% flat VDA tax with no loss offset plus 1% s.194S
TDS. This is unique to your jurisdiction and it is the difference between a strategy that looks
good and a strategy that pays. See §15.4.

### 5.5 The Agent Governance layer
**Why custom:** Hooks + permission tiers + the **anti-fabrication validator** (§16.4). The validator
alone justifies the effort: it greps every generated report for numeric tokens and fails the build
if a number does not appear in the corresponding `metrics.json`. That converts "don't hallucinate
results" from a hope into a test.

**Explicitly NOT custom, though tempting:** a backtester, a matching engine, a data downloader
framework, a charting library, a dataframe library, a task queue, a web dashboard.

---

## SECTION 6 — WHAT WE SHOULD NEVER BUILD OURSELVES

| Never build | Use instead | The trap |
|---|---|---|
| Matching / fill engine | Nautilus `BacktestEngine` | Getting fills, partial fills, funding and margin *exactly* right is years of work, and every shortcut flatters your backtest |
| Exchange connectivity | Nautilus adapters (+ ccxt for metadata) | Reconnects, rate limits, clock skew, idempotency, partial-fill recovery. This is where money dies |
| Time-series storage | Parquet + DuckDB | You will reinvent a column store, badly |
| Statistical tests | statsmodels, `arch`, scipy | Sheppard's SPA implementation is correct. Yours will not be |
| Portfolio optimisers | skfolio / Riskfolio-Lib / CVXPY | Numerical stability in constrained optimisation is a specialty |
| Package/env management | uv | Solved |
| Agent runtime | Claude Code | Solved, and better than what you'd write |
| Diffing / versioning | Git | Solved |
| A notes app | Obsidian | Solved |
| Charting | QuantStats / matplotlib / plotly | Solved |
| Auth, secrets storage | macOS Keychain | Never roll crypto or credential storage |

---

## SECTION 7 — OBSIDIAN ARCHITECTURE

### 7.1 First: challenging your assumption

You asked whether Obsidian is mandatory. **It is not, and that is its greatest strength.**

Obsidian is a *viewer*. The Markdown files are the system. If Obsidian vanished tomorrow, Claude
Code, `ripgrep`, Git and your text editor would all still work perfectly. This is the opposite of
Notion/Roam, where the app *is* the database. Keep it exactly this way:

> **The Obsidian Rule: if deleting Obsidian would lose you data, you have used it wrong.**

That single rule answers most of your plugin questions. It means: no plugin may become the only
place a fact lives.

### 7.2 Vault = repo root

The vault IS the git repo IS the Claude Code working directory. One folder, three lenses.

```
~/Quant-desk/                        ← git repo · Obsidian vault · claude cwd
│
├── CLAUDE.md                        ← <200 lines. The constitution.
├── README.md
├── .gitignore
├── pyproject.toml                   ← uv project
├── uv.lock
│
├── .claude/
│   ├── settings.json                ← hooks, permissions, sandbox  (COMMITTED)
│   ├── settings.local.json          ← personal  (GITIGNORED)
│   ├── agents/                      ← the org chart (12 .md files)
│   ├── skills/                      ← the procedures
│   ├── rules/                       ← path-scoped rules
│   └── hooks/                       ← executable guards (.sh / .py)
│
├── 00-dashboard/                    ← generated. Never hand-edited.
│   ├── health.md
│   ├── pipeline.base                ← Obsidian Bases view
│   ├── experiments.base
│   └── strategies.base
│
├── 01-research/
│   ├── ideas/           IDEA-000001-funding-reversal.md
│   ├── literature/      PAPER-000001-....md   (+ /pdf, gitignored)
│   └── notes/
│
├── 02-experiments/
│   └── EXP-000042-btc-funding-reversal/
│       ├── index.md            ← GENERATED from registry.sqlite
│       ├── hypothesis.md       ← human/agent prose  (pre-registered!)
│       ├── methodology.md      ← human/agent prose
│       ├── config.yaml         ← the ONLY source of parameters
│       ├── strategy.py         ← Nautilus Strategy subclass
│       ├── results.md          ← GENERATED from metrics.json
│       ├── robustness.md       ← GENERATED
│       ├── falsification.md    ← GENERATED — the verdict
│       ├── validation.md       ← independent reviewer agent prose
│       ├── decision.md         ← human/CIO prose
│       └── artifacts/          ← metrics.json, equity.parquet, plots  (gitignored if large)
│
├── 03-strategies/       STR-000007-....md   (lifecycle state lives here + registry)
├── 04-markets/          market structure notes: BTC perp, funding mechanics, venue quirks
├── 05-data/             DATA-000003-binance-um-1m.md  (dataset cards)
├── 06-risk/             limits.yaml (MACHINE-READ), risk-policy.md, incidents/
├── 07-portfolio/
├── 08-execution/        venue-notes, reconciliation reports
├── 09-agents/           agent charters (human-readable mirror of .claude/agents/)
├── 10-reports/          daily/weekly/monthly, post-mortems
├── 20-decisions/        DEC-000001-....md  + ADR-001-....md
├── 21-learnings/        LRN-000001-....md
├── 99-archive/
│
├── src/supercortex/                 ← THE CODE
│   ├── cli.py                       ← `supercortex` entrypoint
│   ├── registry/                    ← §12
│   ├── data/                        ← ingest, validate, catalog
│   ├── costs/                       ← §15.4 India cost model
│   ├── falsify/                     ← §16 the 11 tests
│   ├── risk/                        ← §17
│   ├── lifecycle/                   ← §13 state machine
│   └── report/                      ← templating + anti-fabrication validator
├── tests/
│
├── data/                            ← GITIGNORED. Symlink to external SSD when it exists.
│   ├── raw/                         ← immutable downloads
│   └── catalog/                     ← Nautilus ParquetDataCatalog
└── registry.sqlite                  ← COMMITTED (it's small and it's the audit trail)
```

### 7.3 File naming rules

- **IDs are immutable and zero-padded to 6 digits:** `EXP-000042`. Never renumber. Never reuse.
- Format: `{PREFIX}-{NNNNNN}-{kebab-slug}.md`. The slug may change; the ID may not.
- Prefixes: `IDEA` `EXP` `STR` `DEC` `ADR` `LRN` `TASK` `DATA` `AGT` `DEP` `PAPER` `INC` (incident).
- Folders are numbered so they sort meaningfully in both Obsidian and `ls`.
- **Generated files carry a first line `<!-- GENERATED by supercortex — do not edit -->`** and a
  pre-commit hook rejects manual edits to them.

### 7.4 Frontmatter schema

Every note. Obsidian **Properties** reads it; Obsidian **Bases** builds dashboards from it; Claude
Code greps it; and for experiments it is *generated from SQLite* so it can never drift.

```yaml
---
id: EXP-000042
type: experiment            # idea|experiment|strategy|decision|adr|learning|dataset|agent|paper|incident
title: BTC perp funding-rate reversal
status: FALSIFYING          # see §13 lifecycle states
created: 2026-08-24
updated: 2026-08-26
owner: AGT-000003           # which agent/human produced this
market: [crypto]
symbols: [BTCUSDT-PERP.BINANCE]
horizon: swing              # intraday|swing|position
hypothesis_id: IDEA-000011
strategy_id: STR-000007
datasets: [DATA-000003, DATA-000005]
code_commit: 4f9a1c2
data_snapshot: sha256:9c1f...
trials_consumed: 37         # ← feeds Deflated Sharpe Ratio. Machine-written. Never edit.
falsification_verdict: null # PASS|FAIL|null
tags: [funding, mean-reversion, perp]
---
```

### 7.5 Plugins — the shortest possible list

| Plugin | Core? | Use it? | Reasoning |
|---|---|---|---|
| **Properties** | Core | **Yes** | Frontmatter editing. Zero risk. |
| **Bases** | **Core** `[V]` | **Yes** | Native `.base` files, table/card/list/map views, visual editor. This is your dashboard layer and it is maintained by the Obsidian team. |
| Graph view / Backlinks / Outline / Search | Core | Yes | Free, no lock-in |
| Canvas | Core | Yes | Excellent for drawing strategy logic and system diagrams |
| Templates | Core | Yes | Sufficient. |
| **Dataview** | Community | **Only if Bases can't do it** | Powerful but it is a *query language embedded in your notes*. Every `dataviewjs` block is code that only Obsidian can run — a lock-in vector. Bases first. |
| Templater | Community | **No** | Core Templates + Python generators do more, transparently |
| Tasks | Community | **No** | Your task system is the registry + Claude Code's task list |
| **Obsidian Git** | Community | **No — use the real `git`** | Auto-commit plugins create noisy, meaningless commits and can commit secrets. Commit deliberately, via Claude Code or the terminal, behind `gitleaks`. |
| Local REST API | Community | **No** | An open HTTP port into your vault, for a benefit you can get from the filesystem. Security risk with no upside. |
| Charts / Excalidraw etc. | Community | **No** | Charts come from QuantStats PNGs |

**Architectural risk flagged:** Dataview and Templater are the two plugins that most often violate
The Obsidian Rule. Dataview because dashboards become un-renderable outside Obsidian; Templater
because note-creation logic ends up in JS that Claude Code cannot easily reason about. Prefer
Bases + Python generators.

### 7.6 On vector search and knowledge graphs — challenging assumption (A) and (Q)

You asked about vector DBs, embeddings and knowledge graphs. My recommendation: **no, not yet, and
possibly never.**

- At 2,000 notes, `rg -i "funding reversal" 01-research 02-experiments 21-learnings` returns in
  ~30 ms with exact provenance. An embedding search returns plausible neighbours with none.
- Claude Code's Explore subagent already does semantic-ish retrieval by *reading* files — which
  keeps citations real.
- `[[wikilinks]]` + backlinks **is** your knowledge graph, rendered natively, with zero
  infrastructure. Neo4j would add a database to visualise links you can already see.
- Embeddings introduce a stale index — the single most common source of an agent confidently citing
  a note that has since been rewritten.

**Trigger to revisit:** >5,000 notes, or when `rg` recall demonstrably fails on a real question you
asked. At that point add **LanceDB** (Apache-2.0, embedded, file-based — no server) as an *index
over* the Markdown, never as a store of it. Write the trigger down in `20-decisions/ADR-001`.

---

## SECTION 8 — CLAUDE.md ARCHITECTURE

Verified from official docs `[V]`: target **under 200 lines** per file; files load top-down
(root → cwd); subdirectory `CLAUDE.md` files load **on demand** when Claude reads files there;
`.claude/rules/*.md` with `paths:` frontmatter load **only when matching files are touched**;
`@imports` still cost full context at launch (so they do not save tokens, only organise).

**The key doctrine:** CLAUDE.md is *context, not enforcement*. The docs are explicit: "To block an
action regardless of what Claude decides, use a PreToolUse hook." So CLAUDE.md holds *culture*;
hooks hold *law*.

### 8.1 The layout

```
CLAUDE.md                                  ~120 lines — the constitution
.claude/rules/
  ├── data.md          paths: ["data/**", "src/supercortex/data/**"]
  ├── backtesting.md   paths: ["02-experiments/**", "src/supercortex/falsify/**"]
  ├── risk.md          paths: ["06-risk/**", "src/supercortex/risk/**"]
  ├── execution.md     paths: ["08-execution/**", "src/supercortex/risk/**"]
  ├── writing.md       paths: ["**/*.md"]
  └── python.md        paths: ["**/*.py"]
.claude/skills/
  ├── new-experiment/SKILL.md
  ├── run-falsification/SKILL.md
  ├── ingest-dataset/SKILL.md
  ├── promote-strategy/SKILL.md
  ├── daily-review/SKILL.md
  └── post-mortem/SKILL.md
.claude/agents/            ← 12 role definitions (§9)
```

### 8.2 What goes where

| Content | Location | Why |
|---|---|---|
| "Never write a number you did not read from a file" | **root CLAUDE.md** | Applies always |
| "Every backtest must be registered before it runs" | **root CLAUDE.md** | Applies always |
| Purged CV embargo rules | `.claude/rules/backtesting.md` | Only when touching experiments |
| Parquet partitioning conventions | `.claude/rules/data.md` | Only when touching data |
| "Risk limits live in `06-risk/limits.yaml` and only the CIO edits them" | `.claude/rules/risk.md` | Path-scoped |
| Step-by-step "create a new experiment" | **skill** | A procedure, not a fact — loads on demand |
| The 11 falsification tests | **skill** | Long reference material; costs nothing until invoked |
| Role and boundaries of the Risk Director | `.claude/agents/risk-director.md` | Becomes that agent's system prompt |
| "Anirudh prefers X" | auto memory | Claude writes it |

### 8.3 Draft root CLAUDE.md (the constitution)

```markdown
# Project Supercortex — operating rules

## What this is
A one-person, AI-native quantitative research desk. Crypto first. Local-first.
Research is autonomous; capital is not.

## The five laws
1. NEVER write a number into a document unless you read it from a file produced by code.
   No estimated Sharpe ratios. No "approximately". Cite the artifact path.
2. NEVER run a backtest that has not been registered: `supercortex exp register` first.
   Unregistered runs corrupt the trial count and invalidate every significance test.
3. NEVER touch anything under `06-risk/limits.yaml`, `.env*`, or the Keychain. Ask the CIO.
4. NEVER promote a strategy between lifecycle states by editing a file. Use
   `supercortex lifecycle promote`. The state machine decides, not you.
5. If a result looks good, your next job is to try to kill it, not to celebrate it.

## Definition of done for a research task
- hypothesis.md written BEFORE results exist (pre-registration)
- config.yaml is the only source of parameters
- code_commit and data_snapshot recorded in the registry
- falsification verdict recorded
- an independent reviewer agent has written validation.md

## Commands
- `uv run supercortex --help`         all operations
- `uv run pytest`                     tests
- `uv run ruff check . && uv run mypy src`   lint + types

## Style
- Python 3.12, type-hinted, ruff-formatted. Polars over pandas. No notebooks in git except `.py` marimo files.
- Markdown: frontmatter always. Link with [[wikilinks]]. Prose in short paragraphs.

## When unsure
Write the uncertainty into the document as `UNVERIFIED:` and continue. Never guess a number.
```

That is ~35 lines. Everything else is a rule or a skill.

---

## SECTION 9 — AGENT ORGANIZATION

### 9.1 Challenging assumption (B): teams vs subagents

I verified both mechanisms against the official docs `[V]`. The result reshapes your org chart:

| | Subagents | Agent teams |
|---|---|---|
| Status | **Stable, default-on** | **EXPERIMENTAL**, requires `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` |
| Nesting | **3 levels deep** (configurable) | **None — teammates cannot spawn teammates** |
| Concurrency | 20 default | no hard limit, but token cost is linear |
| Comms | Return a result; named siblings can message | Direct mailbox + shared task list |
| Resumption | Fine | **`/resume` does not restore in-process teammates** |
| Token cost | Lower (summarised back) | Significantly higher |
| Permissions | Per-agent frontmatter: `tools`, `disallowedTools`, `permissionMode`, `maxTurns` | **All teammates inherit the lead's permission mode; cannot be set per-teammate at spawn** |

**Verdict: build the organisation on SUBAGENTS.** Two decisive reasons:

1. **Teams cannot nest.** Your CIO → Director → Worker hierarchy is literally impossible with teams;
   only the lead may spawn. Subagents nest exactly 3 deep — which is exactly your hierarchy.
2. **Teams cannot have per-agent permissions.** For a system that touches money, "every teammate
   inherits the lead's permission mode" is disqualifying. Subagent frontmatter gives each role its
   own tool allowlist. The Risk Director *must* be unable to edit strategy code; the Strategy
   Director *must* be unable to edit risk limits. Only subagents can express that.

**Where teams still earn their keep:** the adversarial debate pattern — "spawn 5 teammates to
attack this strategy and try to disprove each other." That is genuinely better with teams because
teammates argue directly. Use it as an *occasional manual technique* in falsification review
(§16.5), never as load-bearing architecture. Keep the env var **off** by default so that named
subagents don't silently become teammates and break your orchestration.

### 9.2 The org chart

```
                        YOU (CIO — human)
                              │  approvals, capital, kill switch
                              ▼
                 ┌───────────────────────────┐
                 │  AGT-000001  ORCHESTRATOR │  depth 0 (main session)
                 └─────────────┬─────────────┘
                               │  spawns  (depth 1)
   ┌──────────┬──────────┬─────┴─────┬──────────┬──────────┬──────────┐
   ▼          ▼          ▼           ▼          ▼          ▼          ▼
RESEARCH    DATA      QUANT      FALSIFI-     RISK     EXECUTION  ENGINEERING
DIRECTOR  DIRECTOR  DIRECTOR     CATION     DIRECTOR   DIRECTOR   DIRECTOR
  002       003        004      DIRECTOR      006        007         008
                                   005
   │          │          │           │          │          │          │
   └──────────┴──────────┴───────────┴──────────┴──────────┴──────────┘
                               │  spawn (depth 2) — ephemeral workers
        literature-scout · data-fetcher · feature-builder · backtest-runner
        cost-modeller · stat-tester · code-reviewer · reconciler · doc-writer
```

Plus three cross-cutting roles at depth 1: **PORTFOLIO DIRECTOR (009)**, **MONITORING DIRECTOR (010)**,
**KNOWLEDGE DIRECTOR (011)**, and **VALIDATION REVIEWER (012)** — the last of which is deliberately
never the same agent that produced the work it reviews.

### 9.3 Agent charters

Each is a file in `.claude/agents/`. Format below is the actual frontmatter plus the doctrine.

---
**AGT-000002 — Research Director** (`research-director.md`)
- **Mission:** Turn market observations and literature into *falsifiable, pre-registered* hypotheses.
- **Inputs:** `01-research/**`, `04-markets/**`, `21-learnings/**`, web search.
- **Outputs:** `01-research/ideas/IDEA-NNNNNN-*.md`, `02-experiments/*/hypothesis.md`.
- **Tools:** `Read, Grep, Glob, Write, Edit, WebSearch, WebFetch, Agent(literature-scout)`
- **Permissions:** `permissionMode: default`. No `Bash`.
- **CANNOT DO:** run backtests; write to `02-experiments/*/results.md`; touch `src/`; cite a paper
  it has not fetched (see §11.4).
- **Memory:** `memory: project`
- **Success criteria:** hypotheses that are (a) pre-registered before data is touched, (b) stated
  with a specific falsifying observation, (c) traceable to a mechanism, not a pattern.

---
**AGT-000003 — Data Director** (`data-director.md`)
- **Mission:** Guarantee that every dataset is complete, correct, versioned and *affordable in disk*.
- **Inputs:** venue bulk archives, `05-data/**`, `DATA-*` cards.
- **Outputs:** `05-data/DATA-NNNNNN-*.md` dataset cards, Parquet in the catalog, pandera schemas.
- **Tools:** `Read, Grep, Glob, Write, Edit, Bash`
- **Permissions:** Bash restricted by sandbox network allowlist to venue data domains only.
- **CANNOT DO:** delete anything under `data/raw/`; exceed the disk budget (a hook enforces a
  free-space floor of 2 GB — §26); approve a dataset without a completeness report.
- **Success criteria:** zero silent gaps; every dataset has a hash, a row count, a date range, a
  known-issues section, and a `pandera` schema that passes.

---
**AGT-000004 — Quant Director** (`quant-director.md`)
- **Mission:** Convert a hypothesis into a Nautilus strategy + config + registered backtest.
- **Outputs:** `strategy.py`, `config.yaml`, `methodology.md`, registered runs.
- **Tools:** `Read, Grep, Glob, Write, Edit, Bash, Agent(backtest-runner, feature-builder)`
- **CANNOT DO:** write `falsification.md`; write `decision.md`; edit the cost model; edit risk
  limits; run a backtest outside `supercortex exp run` (a `PreToolUse` hook blocks direct
  `python -m nautilus` invocations).
- **Success criteria:** every run registered; parameters only ever in `config.yaml`; results
  reproducible from commit + data hash.

---
**AGT-000005 — Falsification Director** (`falsification-director.md`) ← *the most important agent*
- **Mission:** **Destroy strategies.** Assume every result is luck or leakage until it survives.
- **Inputs:** an experiment folder, its registry row, its trial count.
- **Outputs:** `falsification.md` with a machine-readable verdict block.
- **Tools:** `Read, Grep, Glob, Write, Bash`
- **CANNOT DO:** edit `strategy.py`, `config.yaml`, or any input to the experiment (read-only on
  those paths via `disallowedTools` + hook); soften a threshold; review its own team's work.
- **Success criteria — deliberately inverted:** measured on *kill rate* and on *false approvals
  caught later*. An approval rate above ~15% is itself a red flag to the CIO. This agent is
  rewarded for saying no.

---
**AGT-000006 — Risk Director** (`risk-director.md`)
- **Mission:** Own position sizing, exposure, leverage, drawdown and the kill switch.
- **Outputs:** risk review notes; proposed diffs to `06-risk/limits.yaml` (**proposals only**).
- **Tools:** `Read, Grep, Glob, Write, Bash`
- **CANNOT DO:** **write `06-risk/limits.yaml`** — hard-denied by hook. Only the human CIO commits
  changes to that file. Cannot approve live deployment.
- **Success criteria:** no realised drawdown exceeds the modelled 95% Monte Carlo band; zero limit
  breaches reaching the venue.

---
**AGT-000007 — Execution Director** (`execution-director.md`)
- **Mission:** Venue mechanics, order types, reconciliation, fill quality.
- **Tools:** `Read, Grep, Glob, Write, Edit, Bash`
- **CANNOT DO:** read any credential path; call any exchange *private* endpoint; enable live
  trading. Sandbox network allowlist excludes authenticated venue hosts entirely.
- **Success criteria:** reconciliation breaks = 0; modelled slippage within 20% of realised.

---
**AGT-000008 — Engineering Director** (`engineering-director.md`)
- **Mission:** The `src/supercortex` codebase, tests, CI, performance.
- **Tools:** all code tools + `Agent(code-reviewer)`. `isolation: worktree` for risky refactors.
- **CANNOT DO:** merge to `main` without a passing test suite (pre-commit + hook).
- **Success criteria:** coverage on `falsify/`, `risk/`, `costs/` ≥ 90%; those three are the modules
  where a bug is indistinguishable from a lie.

---
**AGT-000012 — Validation Reviewer** (`validation-reviewer.md`)
- **Mission:** Independent adversarial review of any artifact before it advances a lifecycle state.
- **Doctrine:** **Never spawned by the agent whose work it reviews.** The Orchestrator spawns it
  directly, and the registry records `reviewer_agent_id != producer_agent_id` as a promotion
  precondition.
- **CANNOT DO:** edit anything. `tools: Read, Grep, Glob, Write` with write restricted to
  `validation.md` by hook.

*(Portfolio, Monitoring and Knowledge Directors follow the same template; charters mirrored in
`09-agents/` for human reading.)*

### 9.4 The permission hierarchy — what actually enforces it

Three independent layers. Instructions are **not** one of them.

```
LAYER 1  Agent definition   .claude/agents/*.md → tools / disallowedTools / permissionMode
         (declarative, per-role)

LAYER 2  Hooks + settings   .claude/settings.json → permissions.deny + PreToolUse hooks
         (imperative, enforced by Claude Code regardless of what the model decides)

LAYER 3  The code itself    Strategy objects have no exchange client. Only the execution
         process holds credentials, and it refuses an order without a signed RiskApproval.
         (structural — holds even if layers 1 and 2 are misconfigured)
```

Worked example of Layer 2:

```json
{
  "permissions": {
    "deny": [
      "Read(./.env*)", "Read(./secrets/**)", "Read(~/.aws/**)",
      "Edit(./06-risk/limits.yaml)", "Write(./06-risk/limits.yaml)",
      "Bash(security find-generic-password:*)",
      "Bash(op read:*)",
      "Bash(rm -rf:*)"
    ]
  },
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",  "hooks": [{ "type": "command", "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard_bash.py" }] },
      { "matcher": "Write|Edit", "hooks": [{ "type": "command", "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard_write.py" }] }
    ],
    "PostToolUse": [
      { "matcher": "Write|Edit", "hooks": [{ "type": "command", "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/no_fabricated_numbers.py" }] }
    ]
  }
}
```

`guard_bash.py` returns `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",
"permissionDecisionReason":"..."}}` for: any live-trading entrypoint, any command touching the
Keychain, any direct `nautilus` backtest invocation that bypasses the registry, and any write that
would push free disk below 2 GB.

**Plus the OS layer:** enable Claude Code's Bash sandbox (`/sandbox`, macOS Seatbelt `[V]`) with a
network allowlist covering only `data.binance.vision`, `api.binance.com` (public), `arxiv.org`,
`api.openalex.org`, `pypi.org`, `github.com`. An agent literally cannot reach an execution endpoint.

---

## SECTION 10 — AGENT SPAWNING SYSTEM

### 10.1 The spawn decision

The Orchestrator spawns a worker only when **all four** hold:

1. **Separable** — the task needs no state from a sibling.
2. **Bounded** — a clear artifact ends it (`hypothesis.md` exists; `metrics.json` written).
3. **Context-heavy** — it would otherwise fill the main context with file dumps (this is the
   real reason to delegate: the fan-out searches, not the thinking).
4. **Cheap relative to value** — do not spawn 8 agents to read 3 files.

Default counts, sized to the verified 20-concurrent limit and to token cost:

| Task | Workers |
|---|---|
| New hypothesis exploration | 3 (literature, data-feasibility, mechanism) |
| Dataset ingestion | 1 |
| Backtest sweep | 1 runner (sweeps are loops in code, not agents — **critical**, see below) |
| Falsification battery | 1 director + 3 adversarial reviewers with *distinct lenses* |
| Post-mortem | 3 (data, execution, strategy) |

**The parameter-sweep rule — and this matters more than it looks.** A parameter sweep must be a
`for` loop inside Python, never a fan-out of agents. Reasons: (a) determinism, (b) the registry
increments the trial counter atomically, (c) 200 agents each running one backtest is 200 chances
to under-report a trial. **Agents decide *what* to test; code decides *how many times*.**

### 10.2 Reporting, termination and merging

- **Report:** every worker's final message is a **path**, not a narrative — e.g.
  `02-experiments/EXP-000042/artifacts/metrics.json`. The parent reads the file. This is the
  single most effective anti-hallucination control in the system: prose summaries drift, file
  paths do not.
- **Terminate:** on artifact existence + schema validation (`pandera` / JSON Schema). If the
  artifact is missing or malformed, the parent respawns once with the error, then escalates.
- **Merge:** the parent writes a synthesis note that **links** to each artifact. It may not restate
  numbers; it may only reference them. The `no_fabricated_numbers` hook enforces this.

### 10.3 Conflict resolution

When two agents disagree — and on a research desk they will:

1. **Reduce to a testable difference.** "Is the edge real?" is not resolvable. "Does Sharpe survive
   2× costs?" is.
2. **Run the test.** Code arbitrates, not rhetoric.
3. **If not testable**, escalate to the CIO with both positions written to
   `20-decisions/DEC-NNNNNN-*.md`, each stating what evidence would change its mind.
4. **Standing tie-breaker: the more pessimistic position wins by default.** The cost of wrongly
   killing a good strategy is one missed opportunity. The cost of wrongly approving a bad one is
   capital. These are not symmetric and the system should not pretend they are.

---

## SECTION 11 — RESEARCH ENGINE

### 11.1 The pipeline (yours, corrected)

Your pipeline in Section S is good. It has **four gaps**. Corrected version, additions in bold:

```
IDEA
 ↓ ── **CAPACITY & LEGALITY CHECK**  (does the data fit on disk? is the venue legal for us?)
HYPOTHESIS  (pre-registered, with a named falsifier)
 ↓
DATA REQUIREMENTS → DATA VALIDATION
 ↓ ── **TRIAL BUDGET ASSIGNED**  (you get N trials for this idea; N is recorded up front)
EXPERIMENT → BACKTEST → COST MODEL (incl. tax)
 ↓
ROBUSTNESS → FALSIFICATION → OOS → WALK-FORWARD
 ↓ ── **CAPACITY / LIQUIDITY CHECK**  (what AUM does this die at?)
PAPER TRADING (≥ 3 months)
 ↓ ── **RECONCILIATION**  (do paper fills match backtest assumptions?)
RISK REVIEW → HUMAN APPROVAL → LIVE
 ↓
MONITORING → POST-TRADE ANALYSIS → **LIVE-vs-BACKTEST DIVERGENCE TEST** → LEARNING
 ↓
SECOND BRAIN
```

The four additions and why each one is load-bearing:

1. **Capacity & legality check at the top.** Cheapest possible kill. Rejecting an order-book idea
   in 5 seconds because you have 7.2 GB free beats discovering it after three days of work.
2. **Trial budget.** Declared *before* research starts, recorded in the registry. This is what makes
   the Deflated Sharpe honest. If you blow the budget, the DSR penalty grows — automatically.
3. **Capacity/liquidity check.** A strategy that works at ₹5 lakh and dies at ₹50 lakh is a
   different asset than one that scales. Know which you have before you fall in love.
4. **Live-vs-backtest divergence test.** The most valuable feedback loop in the entire desk, and
   the one everybody skips. Formally: after 30 live/paper days, test whether realised returns are
   drawn from the backtest distribution. If not, the *model* is broken, not the market — and every
   other strategy built on the same assumptions is suspect too.

### 11.2 Idea sourcing

- **Market observation** — anomalies surfaced by scheduled `launchd` scans over the catalog.
- **Literature** — §11.3.
- **Mechanism-first** — start from "who is forced to trade, and why?" Funding payments, liquidation
  cascades, index rebalances, expiry. Mechanism-first ideas survive falsification far more often
  than pattern-first ones, because there is a *reason* for the edge.
- **Failure mining** — `21-learnings/` is an idea source. Strategies that died for fixable reasons
  are the highest-yield seam on a mature desk.

### 11.3 Literature ingestion

| Source | Access | Licence/ToS | Use |
|---|---|---|---|
| **arXiv** | Free API — **1 req / 3 s**, aggressive throttling since late 2024 `[V]` | q-fin listings, PDFs downloadable | **Primary** |
| **OpenAlex** | Free; **as of Feb 2026 requires an API key** with $1/day free credit `[V]` | CC0 metadata | **Primary metadata + citation graph** |
| Semantic Scholar | Free API, shared unauthenticated rate limit `[V]` | metadata | Secondary; request a key |
| Crossref | Free | metadata | DOI resolution |
| SSRN | **No API; scraping violates ToS** | — | Manual download only |
| Journals | Paywalled | — | Do not attempt automated access |

Pipeline: `discover (OpenAlex/arXiv) → dedupe by DOI → download PDF only where legally available →
extract text (pymupdf) → summarise → classify → PAPER-NNNNNN.md card → link to IDEA/EXP`.
PDFs live in `01-research/literature/pdf/` and are **gitignored** (disk + copyright).

### 11.4 The anti-hallucinated-citation rule

**A citation may only be written if the fetch that produced it succeeded and its artifact is on
disk.** Mechanically: the literature agent may not write a `PAPER-*.md` card unless
`01-research/literature/raw/{doi_hash}.json` exists, containing the API response. A `PostToolUse`
hook validates this on every write to `01-research/literature/`. No artifact, no citation. This
turns "don't invent references" from an instruction into a file-existence check.

---

## SECTION 12 — EXPERIMENT ENGINE

### 12.1 Why SQLite and not MLflow — challenging assumption (R)

MLflow, W&B, ClearML and Aim all model the same thing: *ML training runs* — params, metrics,
artifacts, model registry. You are not training models; you are conducting **trials against a
finite dataset**, where the count of trials is itself the most important metric. None of these
tools model "how many times did we look", none model a falsification verdict, and all of them cost
you a server, a backend DB and an artifact store.

**SQLite gives you:** ACID transactions (so the trial counter cannot lie under concurrency), a
single file you can `git`-track, `.dump` into a diffable text file for review, and zero operations.
Optuna, when you need it in Phase 6, already speaks SQLite natively.

**Revisit trigger:** if you ever run >100k trials or need a team UI. Write it in ADR-006.

### 12.2 Schema (abridged)

```sql
CREATE TABLE experiment (
  id TEXT PRIMARY KEY,              -- EXP-000042
  hypothesis_id TEXT, strategy_id TEXT,
  title TEXT NOT NULL,
  status TEXT NOT NULL,             -- lifecycle state
  trial_budget INTEGER NOT NULL,    -- declared BEFORE research begins
  created_at TEXT, owner_agent TEXT
);

-- The counting machine. One row per backtest execution. Ever.
CREATE TABLE trial (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL REFERENCES experiment(id),
  seq INTEGER NOT NULL,
  config_hash TEXT NOT NULL,        -- sha256 of resolved config
  code_commit TEXT NOT NULL,        -- git rev-parse HEAD (dirty tree => refuse to run)
  data_snapshot TEXT NOT NULL,      -- sha256 of dataset manifest
  cost_model_version TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT,                     -- ok | error | abandoned
  metrics_path TEXT,
  agent_id TEXT,
  UNIQUE(experiment_id, seq)
);

CREATE TABLE falsification (
  experiment_id TEXT, test_name TEXT, statistic REAL, threshold REAL,
  passed INTEGER NOT NULL, ran_at TEXT, artifact_path TEXT,
  PRIMARY KEY (experiment_id, test_name)
);

CREATE TABLE lifecycle_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id TEXT, from_state TEXT, to_state TEXT,
  gate_report_path TEXT, approved_by TEXT,   -- 'human:anirudh' for gated transitions
  at TEXT NOT NULL
);
CREATE TABLE dataset  (id TEXT PRIMARY KEY, sha256 TEXT, rows INTEGER, start TEXT, end TEXT, schema_path TEXT);
CREATE TABLE decision (id TEXT PRIMARY KEY, note_path TEXT, at TEXT);
CREATE TABLE learning (id TEXT PRIMARY KEY, note_path TEXT, source_exp TEXT, at TEXT);
```

**`trial` is append-only.** No `UPDATE`, no `DELETE`. A trigger enforces it. Abandoned runs are
recorded with `outcome='abandoned'` — **they still count**. That is the whole point.

### 12.3 Reproducibility contract

A trial may not start unless all five are true, checked by `supercortex exp run`:

1. Git tree is **clean** (no uncommitted changes) — else refuse.
2. `config.yaml` resolves fully; no parameter passed on the command line, ever.
3. Every referenced dataset's sha256 matches its registry row.
4. A random seed is recorded.
5. `hypothesis.md` exists and predates the first trial (checked via `git log --diff-filter=A`).

Rule 5 is pre-registration, enforced by version control. It is the difference between science and
storytelling, and it costs nothing.

### 12.4 Artifacts and versioning

| Artifact | Format | In git? |
|---|---|---|
| `config.yaml`, `strategy.py`, `hypothesis.md` | text | **Yes** |
| `metrics.json` | JSON | **Yes** (small, and it is the evidence) |
| `equity.parquet`, `trades.parquet` | Parquet | No — `data/artifacts/{exp}/`, hash in registry |
| tearsheet PNGs | PNG | No |
| `registry.sqlite` | SQLite | **Yes** — plus a `registry.sql` dump for diffable review |

---

## SECTION 13 — STRATEGY ENGINE (LIFECYCLE)

### 13.1 States

```
IDEA → CANDIDATE → RESEARCH → BACKTESTING → VALIDATING → FALSIFYING
                                                              │
                            ┌─────────────────────────────────┤
                            ▼                                 ▼
                         KILLED  ◄── (any state, any time) ── PAPER
                                                              │
                                                          APPROVED  ← HUMAN GATE
                                                              │
                                                            LIVE
                                                              │
                                    ┌─────────────────────────┼──────────────┐
                                    ▼                         ▼              ▼
                                 DEGRADED ──► SUSPENDED ──► RETIRED      (back to LIVE
                                                                          only via human)
```

I added **FALSIFYING** as a distinct state (your list folded it into VALIDATING) and **KILLED** as
a terminal state reachable from anywhere. Separating them matters: *validating* asks "is this
correct?", *falsifying* asks "is this real?" — different questions, different agent, different
evidence. And KILLED must be a first-class outcome that is celebrated, or nobody will use it.

### 13.2 Promotion gates

You asked me not to accept your example blindly. Here it is, hardened. **Every criterion is
machine-checkable and checked by `supercortex lifecycle promote`, which refuses on any failure.**

**BACKTESTING → VALIDATING**
- Reproducibility contract satisfied (§12.3), clean git tree
- All datasets pandera-valid, zero unexplained gaps
- Cost model applied at ≥ 1× including tax; a zero-cost backtest is not a result
- ≥ 200 trades OR ≥ 3 years OOS (below this, statistics are theatre)

**VALIDATING → FALSIFYING**
- `validation.md` written by an agent that is **not** the producer (`reviewer != producer`)
- Code review passed; no lookahead in feature construction (mechanical shift-test, §16.1)
- Trial count within declared budget, or an overage explicitly recorded

**FALSIFYING → PAPER** — all 11 tests in §16 PASS, plus:
- **Deflated Sharpe Ratio > 0 at 95%**, computed with the *actual* registry trial count
- PBO < 0.5 (I'd prefer < 0.35)
- Cost sensitivity: still profitable at **2×** modelled costs
- No single calendar year contributes > 50% of PnL
- Post-tax return positive under the India cost model
- Capacity estimate documented

**PAPER → APPROVED — HUMAN GATE. No agent may perform this transition.**
- ≥ **90 days** continuous paper trading (not 30 — 30 days of noise proves nothing)
- Reconciliation breaks = 0
- Realised slippage within 1.5× modelled
- Live-vs-backtest divergence test not rejected at 95%
- Risk Director sign-off; CIO written approval in `20-decisions/DEC-*.md`

**APPROVED → LIVE — HUMAN GATE.**
- Credentials provisioned with **withdrawal disabled + IP allowlist**
- Kill switch tested *in production conditions* — actually triggered, not reviewed
- Position size starts at **≤ 10%** of target for the first 30 days
- Rollback procedure written and rehearsed

**LIVE → DEGRADED (automatic):** rolling 30d Sharpe < 50% of backtest, OR drawdown > 75% of the
modelled 95% MC bound, OR 3 consecutive reconciliation breaks.
**DEGRADED → SUSPENDED (automatic):** any hard risk limit breach, or 60 days in DEGRADED.
**Anything → KILLED:** falsification failure, discovered leakage, or CIO decision.

### 13.3 The promotion principle

> **Promotion is subtraction, not addition.** A strategy is not promoted because it looks good.
> It is promoted because a fixed battery of attempts to kill it all failed. The gate is not
> "did it pass?" — it is "did we genuinely try to break it, and record the attempt?"

---

## SECTION 14 — DATA ARCHITECTURE

### 14.1 The layers

| Layer | Contents | Storage | Mutability | Analogy |
|---|---|---|---|---|
| **L0 RAW** | Exactly what the venue served — original zips/CSV | `data/raw/{venue}/{market}/{symbol}/` | **Immutable. Write-once.** | Sealed shipping crates |
| **L1 CANONICAL** | Normalised, typed, deduped, gap-flagged | Nautilus `ParquetDataCatalog` | Rebuildable from L0 | Catalogued shelves |
| **L2 FEATURE** | Derived series (funding z-score, realised vol) | Parquet, `data/features/` | Rebuildable, versioned by code commit | Prepared samples |
| **L3 RESEARCH** | Ad-hoc query results | **Nothing — DuckDB queries in place** | Ephemeral | The desk you work at |
| **L4 LIVE** | Runtime state, positions, orders | Nautilus cache (in-proc) → Valkey in Phase 9 | Volatile | The till |
| **L5 METADATA** | Datasets, experiments, trials, decisions | `registry.sqlite` | Append-mostly | The ledger |

**The rule that keeps this simple: L1 is derived from L0 by code, and L2 from L1 by code. If you
can't rebuild L1 and L2 from scratch with one command, you have a data swamp.**
`supercortex data rebuild` must always work.

### 14.2 Why there is no separate research database

Because `ParquetDataCatalog` is already a Parquet store `[V]`, and DuckDB reads Parquet natively:

```sql
-- No import. No ETL. No second copy.
SELECT symbol, date_trunc('day', ts) d, avg(funding_rate) fr
FROM read_parquet('data/catalog/data/funding_rate/*/*.parquet')
GROUP BY 1,2;
```

Nautilus writes it, DuckDB reads it, Polars processes it, git tracks the *manifest* of it. One copy
of the truth. Every additional database you were considering would have been a *second* copy, and
the second copy is always the one that is subtly wrong at 3 a.m.

### 14.3 Sources

| Data | Source | Cost | Notes |
|---|---|---|---|
| Crypto OHLCV/aggTrades/funding/OI | **data.binance.vision** bulk archives `[V]` | Free | Monthly + daily zips, SHA256 checksums provided. Primary. |
| Bybit / OKX history | venue public archives | Free | Cross-venue validation |
| Gap fill / instrument metadata | **ccxt** (MIT) | Free | Metadata + REST only. **Not** for execution — Nautilus adapters do that |
| Crypto tick/book | Tardis.dev | Paid | **Deferred until external SSD** |
| FX tick (research only) | Dukascopy free export | Free | Research only — see §0.2 on tradability |
| Macro | FRED API | Free | Phase 6 |
| Institutional-grade | Databento (Nautilus adapter exists `[V]`) | Paid | Phase 10+ if ever |

### 14.4 Quality contract

Every dataset carries a `pandera` schema plus a `DATA-NNNNNN` card asserting: row count, date range,
sha256, **gap inventory** (every missing interval, explained or flagged), monotonic timestamps, no
duplicate keys, price/volume sanity bounds, and known venue incidents (halts, symbol relistings,
the 2020 Binance futures outage, etc.). A dataset that has not passed cannot be referenced by a
trial — enforced in `supercortex exp run`.

### 14.5 The disk budget (non-negotiable given §0.1)

```
Free space floor:        2 GB   → hook denies any write below this
Phase 1 budget:          4 GB   → 20 symbols × 1m bars + funding + OI, 2018→now
Order book data:      BLOCKED   → until external SSD present
Raw archives:      auto-pruned  → after L1 build + hash verify, if space < 6 GB
```

`data/` is a symlink. On day one it points into the home directory; the day the SSD arrives you
repoint the symlink and nothing else in the system changes.

---

## SECTION 15 — BACKTESTING ARCHITECTURE: HOW WE PREVENT FALSE DISCOVERIES

**Analogy:** A backtest is a memory of a dream you had about the past. The job of this section is
to make the dream as boring and honest as possible, so that when it still looks good you can
believe a little of it.

### 15.1 The bias taxonomy and the specific defence for each

| Bias | How it actually gets in | Our defence | Enforced by |
|---|---|---|---|
| **Look-ahead** | Using a bar's close to decide a trade in that bar; resampling that peeks | Nautilus event-driven ns clock; every feature built from `ts_event`; **mechanical shift test** (§16.1) | Engine + test |
| **Leakage** | Normalising features over the whole sample; fitting a scaler before splitting | Fit all transforms **inside** the training fold only | Code review + shift test |
| **Survivorship** | Only backtesting symbols that still list | Universe built from **point-in-time listing dates**; delisted symbols retained in L1 | Data Director |
| **Selection bias** | Choosing the symbol/period after seeing results | Universe and date range declared in `hypothesis.md` before trial 1 | Pre-registration (§12.3) |
| **Data snooping / multiple testing** | Trying 500 variants, reporting the best | **Trial registry → DSR + PBO + SPA** | Registry (§12.2) |
| **p-hacking** | Moving the threshold after seeing the p-value | Thresholds in `falsify/thresholds.yaml`, git-tracked, changes visible in diff | Version control |
| **Unrealistic fills** | Assuming you get the mid, or the whole size | Nautilus matching engine; conservative fill model; ≥1 tick slippage always | Engine |
| **Unrealistic spread** | Backtesting bars without spread | Model spread from actual quote data or venue-published averages, doubled | Cost model |
| **Unrealistic funding** | Ignoring perp funding | Nautilus has a native funding-rate data type `[V]`; funding is a *real cash flow* | Engine + data |
| **Unrealistic latency** | Instant order arrival | Fixed latency model ≥ 200 ms for a home connection in India | Engine config |
| **Regime change** | 2021 bull market flattering everything | Mandatory per-year and per-vol-regime attribution; no year > 50% of PnL | Gate (§13.2) |
| **Parameter instability** | Cherry-picked peak in the parameter surface | Neighbourhood test (§16.3) | Falsification |
| **Over-optimisation** | Any of the above, compounded | Walk-forward + CPCV + DSR | Falsification |

### 15.2 Validation designs — what we use and when

| Design | When | Implementation |
|---|---|---|
| Simple train/test | Never alone | — |
| **Purged + embargoed walk-forward** | **Default** | Ours (vendor `timeseriescv` MIT `[V]`, then own it) |
| **Combinatorial Purged CV (CPCV)** | Before promotion | Ours (~150 lines) — feeds PBO |
| Monte Carlo trade resampling | Every experiment | Ours (block bootstrap) |
| Stationary/block bootstrap | Every experiment | `arch.bootstrap` |
| Permutation / randomised-entry null | Every experiment | Ours |
| **White's Reality Check / Hansen SPA** | Comparing a family of variants | **`arch.bootstrap.RealityCheck` / `SPA`** `[V]` |
| Deflated Sharpe Ratio | Before promotion | Ours, fed by the registry trial count |

**Purging and embargo, explained simply:** if your label needs 24 hours of future data to compute,
then a training example from 11 p.m. Monday and a test example from 1 a.m. Tuesday *overlap*. The
model has seen the answer. **Purging** removes training samples whose label window overlaps the test
set; **embargo** additionally drops a buffer after the test set because of serial correlation. Skip
this and your cross-validated Sharpe is inflated — often by 2× or more.

### 15.3 Nautilus configuration for honesty

- `BacktestEngine` with **realistic fill model**, never `fill_on_bar_close` optimism.
- Latency model: ≥200 ms order latency, ≥50 ms data latency.
- Bar strategies act on the **next** bar's open, never the signal bar's close.
- Funding, mark price and margin modelled explicitly for perps.
- `BarType` aggregation from trade ticks where data allows; never resample carelessly.
- Every run: fixed seed, recorded.

### 15.4 The India cost model — the one nobody else has

```python
# src/supercortex/costs/india.py  (design sketch — NOT YET WRITTEN)
@dataclass(frozen=True)
class IndiaVDACostModel:
    version: str = "1.0.0"          # recorded in every trial row
    taker_bps: float = 5.0          # venue fee
    maker_bps: float = 2.0
    slippage_bps: float = 3.0       # doubled from measured
    spread_bps: float = 2.0
    tds_rate: float = 0.01          # s.194S — on TRANSFER VALUE, not profit
    capital_gains_rate: float = 0.30
    cess: float = 0.04
    allow_loss_offset: bool = False # ← the brutal one
```

Applied in two places, and both matter:

1. **Per-trade, inside the backtest:** fees + spread + slippage + funding, and **1% TDS on the
   notional of each disposal**. This is a *cash flow*, not a year-end adjustment — it reduces
   compounding capital in real time. Model it as such.
2. **Per-financial-year, after the run:** 30% + cess on the sum of **gains only**, with losing
   trades contributing **zero** relief. So the post-tax return is
   `Σ(gains) × 0.688 − Σ|losses| − Σ(TDS)`, not `0.688 × net PnL`.

**Why this is decisive:** a strategy with a 55% win rate and symmetric win/loss sizes has a net
edge pre-tax and can be *negative* post-tax, because you are taxed on the winners and get nothing
for the losers. **A high-win-rate, small-edge strategy is a tax trap in India; a low-win-rate,
large-winner (positive-skew, trend-following) strategy is comparatively tax-efficient.** That single
sentence should shape your entire research agenda, and it falls straight out of the cost model.

Report every strategy as: **gross → net of costs → net of TDS → net of tax**. Four numbers, always.

---

## SECTION 16 — THE FALSIFICATION ENGINE

> **Charter:** This engine exists to kill your ideas. It does not evaluate strategies; it attempts
> to *murder* them. A strategy that reaches PAPER has survived a documented assassination attempt.

`uv run supercortex falsify EXP-000042` runs all eleven tests, each against a threshold set in
git-tracked `falsify/thresholds.yaml`, and writes `falsification.md` + rows in the `falsification`
table. **Any FAIL blocks promotion.** No override without a CIO-signed `DEC-*` note explaining why.

### 16.1 The eleven tests

| # | Test | Method | Kills |
|---|---|---|---|
| **1** | **Look-ahead probe** | Shift every feature forward 1 bar and re-run. Sharpe must **collapse**. If it barely moves, the strategy isn't using recent information — if it *improves*, you have leakage. | Look-ahead, leakage |
| **2** | **Future-shuffle null** | Replace future returns with a shuffled series. Any remaining "edge" is a bug. | Implementation bugs |
| **3** | **Cost sensitivity ladder** | Re-run at 1×, 1.5×, 2×, 3× costs incl. tax. **Must survive 2×.** | Fantasy execution |
| **4** | **Parameter neighbourhood** | Sample the parameter space around the chosen point. Median neighbourhood Sharpe must be **> 50% of peak**. A spike is a fluke. | Over-optimisation |
| **5** | **Purged + embargoed walk-forward** | Rolling OOS with purging and embargo. Positive in **≥ 60%** of windows. | Regime-fitting |
| **6** | **CPCV → PBO** | Combinatorially symmetric CV; PBO = P(best in-sample underperforms median OOS). **Require < 0.35.** | Backtest overfitting |
| **7** | **Deflated Sharpe Ratio** | DSR using the **registry's actual trial count**, skew and kurtosis of returns. **Require > 0 at 95%.** | Multiple testing |
| **8** | **SPA / Reality Check** | `arch.bootstrap.SPA` and `.RealityCheck` `[V]` against the variant family + benchmark. | Data snooping across variants |
| **9** | **Randomised-entry null** | Random entries matching trade count and holding period, bootstrapped 10,000×. Strategy must beat the **95th percentile**. | "It's just beta / it's just being in the market" |
| **10** | **Monte Carlo path resampling** | Block-bootstrap trade sequence → drawdown distribution. Observed MaxDD must fall **inside the 95% band**, and the 95th-percentile DD must be **survivable** under §17 limits. | Underestimated ruin risk |
| **11** | **Reproducibility re-run** | Re-execute from recorded commit + seed + data hash. Results must be **bit-identical**. | Silent non-determinism |

Plus two structural gates that are not statistics but block promotion all the same:
- **Regime attribution:** no calendar year > 50% of PnL; positive in ≥ 2 distinct vol regimes.
- **Post-tax positivity:** net-of-tax return > 0 under §15.4.

### 16.2 Why Deflated Sharpe is the linchpin

DSR asks: *given that we tried N strategies, how surprising is the best one?* Its inputs are the
observed Sharpe, the return distribution's skew/kurtosis, the sample length, and **N**.

Everything in Section 12 exists to make N true. If an agent runs 400 sweep backtests and the
registry records 12, DSR reports comfort you have not earned. **This is why the sweep-must-be-a-loop
rule (§10.1) and the append-only trial table (§12.2) are not bureaucracy — they are the load-bearing
walls under your most important statistic.**

### 16.3 Governance

- The Falsification Director **cannot** modify the experiment (hook-enforced read-only on
  `strategy.py`, `config.yaml`, `hypothesis.md`).
- Thresholds live in git. Loosening one shows up in `git diff` and requires a `DEC-*` note. You
  cannot quietly move the goalposts.
- The Falsification Director is measured on **kill rate**. If it approves more than ~15% of
  candidates, the CIO should audit *it*, not celebrate.
- **A strategy may never be falsified by the agent that built it.**

### 16.4 The anti-fabrication validator (this is the one that matters)

Your rule 16 — "Do not allow agents to fabricate research results" — cannot be enforced by asking
nicely. Here is the mechanism:

```python
# .claude/hooks/no_fabricated_numbers.py   (PostToolUse on Write|Edit)
# For any *.md written under 02-experiments/ or 10-reports/:
#   1. extract every numeric token (regex for floats, percentages, ratios)
#   2. load the sibling artifacts/metrics.json (and any JSON it references)
#   3. every extracted number must appear in that JSON (within float tolerance),
#      or be inside a ```code fence```, or be a date, or an ID like EXP-000042
#   4. otherwise: exit 2 with the offending numbers → Claude is told to fix it
```

**Effect:** an agent physically cannot write "Sharpe was about 1.8" into a results document unless
1.8 exists in a file produced by deterministic code. Prose is free; numbers are notarised.

This is the single highest-value 60 lines of code in the entire project. Build it in Phase 1.

### 16.5 Optional: the adversarial panel

For borderline candidates only, use Claude Code agent teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`)
to spawn 3–5 teammates with **distinct lenses** — data integrity, statistical validity, execution
realism, economic mechanism, tax/regulatory — and instruct them to attack each other's conclusions.
Verified `[V]`: teammates message each other directly, which is what makes a real debate possible.
The surviving objections go into `falsification.md`. Manual technique, not architecture.

---

## SECTION 17 — RISK ENGINE

> **Doctrine: risk is not an opinion an agent holds. It is a function an order must pass through.**

### 17.1 The structural guarantee

```
Strategy  ──emits──►  Order Intent
                          │
                          ▼
                 ┌──────────────────┐
                 │   RISK ENGINE    │   pure function, deterministic, unit-tested
                 │  limits.yaml     │   ← only the human CIO may commit changes
                 └────────┬─────────┘
                    approve │ reject
                          ▼
                 ┌──────────────────┐
                 │ EXECUTION CLIENT │   the ONLY object holding credentials
                 │ refuses an order │
                 │ without a signed │
                 │  RiskApproval    │
                 └────────┬─────────┘
                          ▼
                        VENUE
```

**Why a strategy cannot bypass it:** the strategy object is never given a reference to an execution
client. It emits intents into the engine's message bus. The execution client's `submit()` signature
*requires* a `RiskApproval` object that only the risk engine can mint. This is not policy — it is a
type error if you try. Even a hallucinating agent that rewrites strategy code cannot produce an
approval, because it has no key material to mint one.

### 17.2 Pre-trade checks (every order, in order, fail-closed)

1. Kill switch not engaged (a file: `06-risk/KILL` — if it exists, everything rejects)
2. Strategy is in state `LIVE` in the registry
3. Order notional ≤ per-order max
4. Resulting position ≤ per-strategy max
5. Resulting gross exposure ≤ portfolio max
6. Resulting leverage ≤ max leverage
7. Venue concentration ≤ max per venue
8. Symbol concentration ≤ max per symbol
9. Day's realised+unrealised loss > daily loss limit → **reject and flatten**
10. Strategy drawdown > strategy limit → **suspend strategy**
11. Portfolio drawdown > portfolio limit → **suspend everything**
12. Estimated liquidation price ≥ safety multiple from mark
13. Data freshness: last tick < N seconds old (stale data → reject; this catches disconnects)
14. Order rate limit (idempotency + venue limits)

**Fail-closed everywhere.** If the risk engine cannot evaluate a check — missing data, exception,
timeout — it **rejects**. An unavailable risk engine means no trading, not free trading.

### 17.3 Portfolio risk (Phase 7)

Volatility targeting; correlation-aware sizing (a 3-strategy book that is 0.9-correlated is one
strategy with 3× the fees); VaR/CVaR via historical + Monte Carlo; per-strategy risk budget;
crypto-specific: funding exposure, liquidation cascade stress, exchange counterparty limit
(**never more than X% of capital on one venue** — exchange failure is a live risk class in crypto,
not a tail hypothetical).

### 17.4 `06-risk/limits.yaml` — the most protected file in the repo

```yaml
version: 1
portfolio:
  max_gross_exposure_pct: 100
  max_leverage: 2.0
  max_daily_loss_pct: 2.0
  max_drawdown_pct: 15.0
  max_venue_exposure_pct: 40
kill_switch:
  daily_loss_pct: 3.0
  reconciliation_breaks: 3
  data_staleness_seconds: 60
per_strategy:
  default:
    max_position_pct: 10
    max_drawdown_pct: 8
    max_order_notional_usd: 500
```

Protected by: `permissions.deny` on Edit/Write `[V]`, a `PreToolUse` hook, a pre-commit hook
requiring a `DEC-*` note in the same commit, and a runtime checksum the trading process verifies at
startup against the value recorded at approval time. Four layers, because this file is where a
subtle change does the most damage.

### 17.5 Kill switches

| Trigger | Action | Who |
|---|---|---|
| `touch 06-risk/KILL` | All new orders rejected; open positions flattened | **You, from your phone via SSH** |
| Daily loss > 3% | Auto-flatten, suspend all, alert | Automatic |
| Reconciliation break ×3 | Suspend strategy, alert | Automatic |
| Data stale > 60 s | Reject new orders, alert | Automatic |
| Venue disconnect > 5 min | Cancel resting orders, alert | Automatic |

Test the kill switch **monthly, in production**, as a calendar event. An untested kill switch is a
comfort object, not a control.

---

## SECTION 18 — EXECUTION ENGINE

### 18.1 Crypto

Nautilus native adapters, all stable `[V]`: Binance, Bybit, OKX, Coinbase, Kraken, Deribit, BitMEX,
plus DEX adapters (Hyperliquid, dYdX, Derive, Lighter).

**Venue selection for you specifically:**

| Venue | Nautilus adapter | India (FIU-IND registration) | Verdict |
|---|---|---|---|
| **Binance** | ✅ stable | FIU-registered | **Primary** — best public historical archive `[V]` |
| **Bybit** | ✅ stable | FIU-registered as VDASP since Feb 2025 | **Secondary** |
| OKX | ✅ stable | verify current status | Tertiary |
| Coinbase / Kraken | ✅ stable | not India-focused | Reference data only |
| Deribit | ✅ stable | options venue | Phase 10+ |
| **Hyperliquid / dYdX** | ✅ stable | **No KYC ≠ legal clarity** | **Do not use until counsel advises** — DEX use by Indian residents is unresolved and the tax reporting burden falls entirely on you |

> **Regulatory flag:** "the exchange doesn't ask for KYC" is not a legal analysis. FIU-IND
> registration status and your s.194S TDS obligations are the questions to put to your CA. This is
> a technical flag, not legal advice.

**Order management essentials Nautilus already handles** (and which you should never rewrite):
client order IDs for idempotency, order state machine, partial fills, cancel-replace, rate limiting,
WebSocket reconnect with sequence-gap detection, and **restart recovery via position reconciliation
against the venue** — this last one is the difference between a trading system and a script.

### 18.2 Forex — the honest answer

Given §0.2, there are exactly three legitimate paths, and you must pick one before any FX work
begins:

| Path | Mechanism | Nautilus support | Reality |
|---|---|---|---|
| **A. NSE/BSE currency derivatives** | SEBI broker via **OpenAlgo** (AGPL-3.0 server + MIT client) | **One** Nautilus adapter covers 34 brokers via OpenAlgo's `CDS`/`BCD` segments | Legal. Liquid in USD/INR. **Corrected 2026-08-24 — see ADR-011.** Days, not weeks |
| **B. IBKR under LRS** | Interactive Brokers | ✅ stable adapter `[V]` | **Equities/ETFs only** — LRS does not permit leveraged FX/derivatives |
| **C. Research only** | Dukascopy free tick data | via Parquet | Build FX models, trade none. Zero legal risk. |

**Recommendation: (C) now, (A) later if FX proves worth it.** Do not build FX execution in Phase 1.
Spend the effort on crypto where you have a legal venue, a free historical archive, and a working
adapter. Revisit FX only after one crypto strategy is live and profitable.

### 18.3 Reconciliation (non-negotiable before live)

Every 60 seconds while live, and on every restart:
positions (ours vs venue) · open orders · balances · fills since last checkpoint · fees.
Any break → log, alert, and after 3 breaks → suspend. Nightly: full-day fill-by-fill reconciliation
written to `08-execution/reconciliation/YYYY-MM-DD.md`.

**This is the single most-skipped component in retail systematic trading and the most common cause
of silent capital loss.** You think you're flat; you're not.

---

## SECTION 19 — SECURITY MODEL

### 19.1 Credential tiers

| Tier | Purpose | Storage | Agent access | Exchange permissions |
|---|---|---|---|---|
| **T0** | Public data | none needed | ✅ full | — |
| **T1** | Research (read-only) | macOS Keychain | ⚠️ via CLI wrapper only | Read-only. No trade. No withdraw. |
| **T2** | Paper / testnet | macOS Keychain | ⚠️ via CLI wrapper only | Testnet only |
| **T3** | **LIVE** | macOS Keychain, **separate login item** | ❌ **NEVER** | Trade ✅ · **Withdraw ❌** · **IP allowlist ✅** |

**The T3 rule, stated precisely:** the live trading process is started **by you, manually or by a
`launchd` job you configured**, and it reads T3 credentials from the Keychain *in its own process*.
No Claude Code session, no subagent, and no agent-spawned shell ever holds a T3 credential. If an
agent needs to know whether live trading is running, it reads a status file — not a key.

### 19.2 Enforcement stack

1. **`permissions.deny`** in `.claude/settings.json`: `Read(./.env*)`, `Read(./secrets/**)`,
   `Bash(security find-generic-password:*)`, `Bash(op read:*)` `[V]`.
2. **`PreToolUse` hook** returning `permissionDecision: "deny"` `[V]` for any command matching
   credential patterns, live entrypoints, or `curl` to authenticated venue hosts.
3. **Bash sandbox** (macOS Seatbelt `[V]`): network allowlist limited to public data + package +
   research domains. An agent cannot reach `api.binance.com` private endpoints at the OS level.
4. **Exchange-side** — the layer that actually saves you: **withdrawals disabled**, IP allowlist to
   your home IP, separate API keys per tier, keys rotated quarterly, and 2FA on the account.
5. **`gitleaks`** in pre-commit + a GitHub push protection ruleset.
6. **Private repository. Always.** A public quant repo leaks both credentials and alpha.
7. **Audit log:** append-only `09-agents/audit/YYYY-MM.jsonl` written by a `PostToolUse` hook
   recording every Write/Edit/Bash by agent, with timestamps.

### 19.3 Threat model — what I actually worry about, ranked

| Threat | Likelihood | Impact | Control |
|---|---|---|---|
| **Secret committed to git** | **High** | Severe | gitleaks + private repo + push protection |
| Agent runs a destructive command | Medium | Moderate | Sandbox + deny rules + git |
| Agent fabricates a result you act on | **High** | **Severe** | §16.4 validator + numbers-from-files rule |
| Malicious/compromised dependency | Medium | Severe | `uv.lock` pinned + hashes; review new deps; minimal dep count |
| Third-party MCP server exfiltrates data | Medium | Severe | **Zero third-party MCP servers** (§3.7) |
| Exchange API key theft | Low | **Catastrophic** | Withdrawals disabled + IP allowlist |
| Mac stolen/compromised | Low | Severe | FileVault + Keychain + remote key revocation runbook |
| Prompt injection via fetched web content | **Medium** | Moderate | Treat all fetched content as untrusted data; never let a fetched page's text drive a Bash command |

Note the top-ranked pair: the two most likely severe events are **a leaked secret** and **a
fabricated number you believe**. Both are cheap to defend and both are usually undefended.

---

## SECTION 20 — MONITORING

### 20.1 Phase 1–7: the boring version that actually gets used

A `launchd` job every 5 minutes writes `00-dashboard/health.md`:

```markdown
<!-- GENERATED 2026-08-24T14:05:00+05:30 -->
# System health
| Check | Status | Detail |
|---|---|---|
| Disk free | 🟡 | 6.8 GB (floor 2 GB) |
| Data freshness | 🟢 | last bar 2026-08-24 14:04 |
| Catalog integrity | 🟢 | 23 datasets, 0 hash mismatches |
| Registry | 🟢 | 412 trials, 0 unregistered runs detected |
| Paper strategies | 🟢 | STR-000007 running, 31d, PnL +2.4% |
| Reconciliation | 🟢 | 0 breaks (24h) |
| Kill switch | 🟢 | not engaged |
```

Failures push to your phone via **ntfy.sh** (self-hostable, free). Total cost: ~50 lines, 0 MB RAM,
0 services.

### 20.2 Phase 8+: the real stack

Prometheus (Apache-2.0) + Grafana (AGPL-3.0 — fine internally, do not redistribute) + Alertmanager.
Metrics: system (CPU/RAM/disk), data (staleness, gaps, ingest lag), execution (fill rate, slippage
vs modelled, reject rate, latency p50/p99), P&L (per strategy, realised/unrealised), risk (exposure,
leverage, drawdown vs limit), agent (spawns, failures, tokens), and **strategy degradation** (rolling
Sharpe vs backtest, live-vs-backtest divergence statistic).

Logs stay as JSON lines on disk, searched with `ripgrep`. **Skip Loki.** A log aggregator for one
machine is a service to maintain in exchange for a worse `grep`.

### 20.3 Alert discipline

Three levels only: **CRITICAL** (wake me — kill switch, risk breach, live disconnect), **WARNING**
(check today — data gap, degradation, reconciliation break), **INFO** (dashboard only). If you get
more than ~3 alerts a week, the thresholds are wrong. Alert fatigue is a security vulnerability.

---

## SECTION 21 — LOCAL MAC MINI DEPLOYMENT

### 21.1 The verdict: native macOS. No Docker.

| Approach | Verdict | Reasoning |
|---|---|---|
| **Native macOS + uv + launchd** | **CHOSEN** | Zero VM overhead on 16 GB; Nautilus ships ARM64 wheels `[V]`; DuckDB/SQLite/Parquet are files; nothing here is a server |
| Docker Desktop | Rejected | A Linux VM eating 2–4 GB to run processes that run natively |
| OrbStack | Rejected | Better than Docker, but **$8/user/mo for commercial use** `[V]` and still a VM |
| colima | Fallback | Free (MIT). Use *only* if Phase 8 Grafana arrives |
| Apple `container` | Watch | Apache-2.0, v1.0 Jun 2026, but no Compose yet |
| Hybrid | **Actual answer** | Native for everything through Phase 7; one container for Grafana in Phase 8 if you want it |

**Analogy:** Docker is a shipping container. Shipping containers are brilliant when you need to move
cargo between ships, trains and trucks. You have one truck, parked in your driveway, and it is never
leaving. Just put the boxes in the truck.

### 21.2 Processes and layout

```
PROCESSES (steady state, Phase 8)
  ── com.supercortex.health        launchd, every 5 min      ~20 MB
  ── com.supercortex.ingest        launchd, hourly           ~200 MB peak
  ── com.supercortex.paper         launchd KeepAlive         ~400 MB
  ── com.supercortex.live          MANUAL START ONLY         ~400 MB
  ── com.supercortex.backup        launchd, daily 02:00      ~50 MB
  ── claude (interactive)          you                       ~300 MB
  Peak: ~1.5 GB of 16 GB. Backtests are the spike: 2–6 GB, transient.

PORTS
  Phase 1–7: NONE. Nothing listens. This is a feature.
  Phase 8:   9090 Prometheus (127.0.0.1), 3000 Grafana (127.0.0.1) — localhost only, never 0.0.0.0

DIRECTORIES
  ~/Quant-desk/            repo + vault (git)
  ~/Quant-desk/data/       SYMLINK → /Volumes/SSD/supercortex-data  (gitignored)
  ~/Library/LaunchAgents/  com.supercortex.*.plist
  ~/.claude/               agents, settings, auto memory

DATABASES
  registry.sqlite          one file, in git
  Parquet catalog          files
  (no server processes)
```

### 21.3 Example launchd unit

```xml
<!-- ~/Library/LaunchAgents/com.supercortex.health.plist -->
<plist version="1.0"><dict>
  <key>Label</key><string>com.supercortex.health</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/anirudhtumuluru/.local/bin/uv</string>
    <string>run</string><string>--project</string>
    <string>/Users/anirudhtumuluru/Quant-desk</string>
    <string>supercortex</string><string>health</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>/Users/anirudhtumuluru/Quant-desk/logs/health.log</string>
  <key>StandardErrorPath</key><string>/Users/anirudhtumuluru/Quant-desk/logs/health.err</string>
</dict></plist>
```

### 21.4 Backups (3-2-1)

| What | Where | How | Frequency |
|---|---|---|---|
| Code + vault + registry | GitHub (private) | `git push` | Every session |
| Registry snapshot | `backups/registry-YYYYMMDD.sql` | `sqlite3 .dump` | Daily |
| Whole machine | Time Machine → external disk | automatic | Continuous |
| Parquet catalog | second external disk / Backblaze B2 | `rclone sync` | Weekly |
| Keychain secrets | **written on paper, in a safe** | manual | On change |

Raw venue archives are **not** backed up — they are re-downloadable from `data.binance.vision`,
and that is what makes L0/L1 separation valuable: only L0 needs to exist, and it exists upstream.

---

## SECTION 22 — NETWORK / SYSTEM DIAGRAM

```
   INTERNET
      │
      │  ┌──────────────────────────────── OUTBOUND ONLY. No inbound ports. ─────────┐
      │  │                                                                            │
      ▼  ▼                                                                            │
┌──────────────────────────────────────────────────────────────────────────┐         │
│  HOME ROUTER (India)  — static IP required for exchange allowlist         │         │
└───────────────────────────────┬──────────────────────────────────────────┘         │
                                │                                                     │
                    ┌───────────▼────────────┐                                        │
                    │   MAC MINI (M4/16GB)   │                                        │
                    │   FileVault ON         │                                        │
                    └───────────┬────────────┘                                        │
                                │                                                     │
   ┌────────────────────────────┼─────────────────────────────┐                       │
   ▼                            ▼                             ▼                       │
┌──────────────┐   ┌────────────────────────┐   ┌──────────────────────┐              │
│ CLAUDE CODE  │   │  SUPERCORTEX PROCESSES │   │  EXTERNAL SSD (2 TB) │              │
│ + subagents  │   │  health · ingest       │   │  data/raw            │              │
│              │   │  paper  · live(manual) │   │  data/catalog        │              │
│ SANDBOXED    │   │                        │   │  Time Machine        │              │
│ Seatbelt     │   │  Keychain ── T3 keys ──┼──►│                      │              │
│ net allowlist│   │  (live process only)   │   └──────────────────────┘              │
└──────┬───────┘   └───────────┬────────────┘                                         │
       │                       │                                                      │
  allowed:                allowed:                                                    │
  data.binance.vision      api.binance.com (private, IP-allowlisted)  ────────────────┘
  api.github.com           stream.binance.com (WSS)
  pypi.org                 api.bybit.com
  arxiv.org / openalex     ntfy.sh
  api.anthropic.com
       │
   DENIED: every authenticated venue endpoint, every credential store
```

**Key property: nothing listens.** There is no inbound attack surface at all in Phase 1–7. Remote
access, if you want it, is Tailscale (WireGuard, no open ports) — never a forwarded port.

---

## SECTION 23 — DATA FLOW DIAGRAM

```
 data.binance.vision              ccxt (metadata/gaps)          Dukascopy (FX research)
        │                                │                              │
        └────────────────┬───────────────┴──────────────────────────────┘
                         ▼
              ┌────────────────────┐   sha256 + row counts + gap inventory
              │  L0 RAW (immutable)│──────────────────────────────┐
              └─────────┬──────────┘                              │
                        ▼  normalise · dedupe · type · validate   │
              ┌────────────────────┐   pandera schema PASS        │
              │  L1 ParquetCatalog │                              ▼
              └─────────┬──────────┘                     ┌──────────────────┐
                        │                                │ DATA-NNNNNN card │
          ┌─────────────┼─────────────┐                  │ registry.dataset │
          ▼             ▼             ▼                  └──────────────────┘
  ┌────────────┐ ┌────────────┐ ┌──────────────┐
  │ L2 FEATURES│ │  DuckDB    │ │  NAUTILUS    │
  │ (Parquet)  │ │ (research) │ │  BACKTEST    │◄── config.yaml + strategy.py
  └─────┬──────┘ └─────┬──────┘ └──────┬───────┘        (registered trial)
        │              │               ▼
        │              │      ┌──────────────────┐
        │              │      │ + COST MODEL     │  fees·spread·slip·funding·TDS·tax
        │              │      └──────┬───────────┘
        │              │             ▼
        │              │      ┌──────────────────┐
        │              └─────►│  metrics.json    │  ← the ONLY source of numbers
        │                     └──────┬───────────┘
        │                            ▼
        │                     ┌──────────────────┐
        │                     │ FALSIFICATION    │  11 tests → verdict
        │                     └──────┬───────────┘
        │                            │ PASS
        │                            ▼
        │                     ┌──────────────────┐      ┌──────────────┐
        │                     │  PAPER (90d)     │─────►│  RISK ENGINE │
        │                     └──────┬───────────┘      └──────┬───────┘
        │                            │                         │ approve
        │                            ▼                         ▼
        │                   ┌────────────────┐        ┌──────────────────┐
        │                   │ HUMAN APPROVAL │───────►│  LIVE @ VENUE    │
        │                   └────────────────┘        └────────┬─────────┘
        │                                                      ▼
        │                                             ┌──────────────────┐
        │                                             │  RECONCILE       │
        │                                             └────────┬─────────┘
        │                                                      ▼
        │                                             ┌──────────────────┐
        └────────────────── feeds new features ◄──────│ POST-TRADE + LRN │
                                                      │ → SECOND BRAIN   │
                                                      └──────────────────┘
```

---

## SECTION 24 — AGENT DATA FLOW

Agents communicate through **files**, not conversation. This is the single most important design
choice in the agent layer, and your instinct in brief section (C) was right.

```
 ORCHESTRATOR
      │ spawn(prompt) ────────────────────────────► RESEARCH DIRECTOR
      │                                                    │ writes
      │                                                    ▼
      │                                        01-research/ideas/IDEA-000011.md
      │                                        02-experiments/EXP-000042/hypothesis.md
      │ ◄──────── returns: "path: .../hypothesis.md"  (a PATH, never a summary of numbers)
      │
      │ spawn ──────────────────────────────────► QUANT DIRECTOR
      │                                                    │ reads hypothesis.md
      │                                                    │ writes strategy.py, config.yaml
      │                                                    │ calls: supercortex exp run
      │                                                    ▼
      │                                        registry.sqlite:  +1 trial row (ATOMIC)
      │                                        artifacts/metrics.json
      │ ◄──────── "path: .../metrics.json, trial seq 38"
      │
      │ spawn ──────────────────────────────────► FALSIFICATION DIRECTOR  (read-only on inputs)
      │                                                    │ reads metrics.json + trial count
      │                                                    ▼
      │                                        falsification.md + falsification table rows
      │ ◄──────── "verdict: FAIL — test 6 PBO=0.61 > 0.35"
      │
      │ spawn ──────────────────────────────────► VALIDATION REVIEWER (≠ producer)
      │                                                    ▼  validation.md
      │
      ▼  synthesises by LINKING, never by restating numbers
 02-experiments/EXP-000042/index.md   (generated)
 20-decisions/DEC-000019-kill-exp42.md
 21-learnings/LRN-000008-pbo-catches-funding-overfit.md
      │
      ▼
 GIT COMMIT  ← the audit trail. Every artifact, every verdict, attributable and diffable.
```

**Why artifacts beat conversation, concretely:**
- Durable: survives context compaction, session end, and your memory.
- Reviewable: `git diff` shows exactly what an agent changed.
- Attributable: frontmatter `owner` + audit log.
- Composable: the next agent reads the file, not a summary of a summary.
- **Anti-hallucination:** a file either exists or it does not.

**Message queues, event buses, JSON-RPC, MCP-as-transport — all rejected** for inter-agent
communication. They add infrastructure to replicate what a filesystem plus git already does better,
and they make the conversation invisible to you. The filesystem *is* the message bus, and Obsidian
is its UI.

---

## SECTION 25 — HUMAN APPROVAL FLOW

### 25.1 What agents may do alone

Research · literature ingestion · hypothesis generation · data download and validation · feature
engineering · writing strategy code · **registered** backtests · statistical analysis · falsification
· code review · documentation · post-mortems · paper-trading *analysis* · report generation.

That is a lot of autonomy, deliberately. The system should be able to run a full research cycle
overnight without you.

### 25.2 What requires you — and I have made this stricter than your list

| Action | Gate | Why stricter than your draft |
|---|---|---|
| Enable live trading | **Human** | — |
| Change `06-risk/limits.yaml` | **Human + `DEC-*` note in same commit** | — |
| Add capital | **Human** | — |
| Change withdrawal permissions | **Human, on the exchange website only** | Never via API, ever |
| Change credentials | **Human** | — |
| Deploy a new production strategy | **Human** | — |
| Increase leverage | **Human** | — |
| **Promote PAPER → APPROVED** | **Human** | Agents may not decide readiness for capital |
| **Loosen any falsification threshold** | **Human + `DEC-*`** | *Added.* This is the single easiest way to fake success |
| **Override a falsification FAIL** | **Human + written justification** | *Added.* Otherwise the engine is decorative |
| **Delete or edit a `trial` row** | **Forbidden to everyone** | *Added.* Append-only or the DSR is a lie |
| **Add a new venue or data vendor** | **Human** | *Added.* Legal + cost + counterparty exposure |
| **Install a new dependency** | **Human** | *Added.* Supply-chain surface |
| **Enable a third-party MCP server** | **Human** | *Added.* Arbitrary code with filesystem access |
| **Trade any FX instrument** | **Human + written FEMA position** | *Added.* See §0.2 |

### 25.3 The approval ceremony

Approvals are not chat messages. They are commits.

```
Agent prepares      →  20-decisions/DEC-000023-promote-str7-to-live.md
                       with: evidence links, falsification verdict, risk review,
                       capacity estimate, post-tax return, what would make us wrong
        ▼
You read it         →  in Obsidian, on your phone or desk, unhurried
        ▼
You decide          →  edit the note: approved_by: human:anirudh
                       approved_at: <ISO ts>
                       conditions: "start at 10% size for 30 days"
        ▼
You commit          →  git commit -S -m "DEC-000023: approve STR-000007 to LIVE"
        ▼
Machine verifies    →  supercortex lifecycle promote STR-000007 --to LIVE
                       reads the DEC note, verifies the signature/commit, checks every
                       gate in §13.2, then and only then flips the registry state
```

**Property:** the state change is impossible without an artifact, and the artifact is impossible to
backdate. Your approval leaves a fingerprint. So does your override.

---

## SECTION 26 — DISASTER RECOVERY

| Scenario | Blast radius | Recovery | Prevention |
|---|---|---|---|
| **Mac crashes / dies** | Live positions unmanaged | Positions are **at the venue**, not on the Mac. Restore repo from GitHub, catalog from SSD/B2, restart, **reconcile against venue before trading** | Never run live without exchange-side stop-losses as a backstop; Phase 9 → cheap VPS |
| **Power / internet cut (India-specific)** | Cannot manage positions | Same as above. **Always keep an exchange-side reduce-only stop on every live position** | UPS for the Mac; 4G failover; strategy horizon ≥ days so an hour offline is survivable |
| **registry.sqlite corrupted** | Loss of trial history → all significance stats invalid | Restore from `backups/registry-*.sql` + git | Daily `.dump` to git; WAL mode; never edit by hand |
| **Parquet catalog corrupted** | Research blocked | `supercortex data rebuild` from L0; if L0 gone, re-download from `data.binance.vision` | L0/L1 separation is exactly this insurance |
| **Git repo corrupted** | History loss | Clone from GitHub; every worktree is a full backup | Push every session; never rewrite published history |
| **Agent goes rogue / mass-deletes files** | Local damage | `git checkout .`; unstaged work lost only | Sandbox + deny rules + commit frequently |
| **Strategy behaves insanely** | Capital | `touch 06-risk/KILL` → flatten. Automatic triggers fire first | Position caps, daily loss limit, 10% sizing for 30 days |
| **Data silently corrupted** | **False research for months** | Hash verification catches it; rebuild L1 | Hash every dataset; re-verify weekly; cross-venue sanity checks |
| **Credentials leak** | **Catastrophic** | Revoke at exchange **first** (before anything else), then rotate, then investigate | Withdrawals disabled means a leaked key cannot drain you — it can only trade. This is why that setting matters more than any other |
| **Exchange halts / insolvency** | Capital at that venue | Nothing to do after the fact | **Venue exposure cap.** Never more than 40% of capital at one exchange. Withdraw profits regularly |
| **Disk full (likely! §0.1)** | Writes fail mid-run, possible corruption | Free space; rebuild affected datasets | **Hook denies writes below 2 GB free** — build this in Phase 1 |
| **Claude Code / API unavailable** | Research stops | Trading is Python; it keeps running | **The trading system must never depend on an LLM at runtime.** Rule 14 of your brief, architecturally enforced |

**The most important line in this table:** *positions live at the venue, not on your Mac.* Design
every failure mode around that fact, and set exchange-side stops so that the worst case of "my house
lost power" is "I stopped trading", not "I lost money I couldn't see".

---

## SECTION 27 — TESTING STRATEGY

| Level | What | Tool | Gate |
|---|---|---|---|
| **Unit** | Cost model, risk checks, purged CV, DSR/PBO math, registry invariants | pytest | **≥90% on `costs/`, `risk/`, `falsify/`** — a bug here is indistinguishable from a lie |
| **Property** | Risk engine never approves an order violating limits, for any input | hypothesis | Every PR |
| **Golden** | A known strategy on frozen data produces bit-identical metrics | pytest + committed fixture | Every PR — catches silent behaviour drift |
| **Integration** | Ingest → catalog → backtest → metrics → falsify end-to-end on a tiny dataset | pytest | Every PR |
| **Data contract** | Every dataset matches its pandera schema | pandera | Every ingest |
| **Simulation** | Backtest vs paper on the same period; results must agree within tolerance | custom | Before PAPER→APPROVED |
| **Chaos** | Kill the process mid-order; disconnect the network; feed stale data; return venue errors | custom harness | **Before live. Non-negotiable.** |
| **Reconciliation** | Injected mismatches are detected and escalate correctly | custom | Before live |
| **Security** | gitleaks; deny-rule tests (attempt a forbidden read, assert denial) | gitleaks + pytest | Every commit |
| **Kill switch** | Actually trigger it in production | manual | **Monthly calendar event** |

**The test that matters most and that nobody writes:** the **golden backtest**. Freeze a dataset,
freeze a strategy, commit the expected `metrics.json`. Any change to the engine, the cost model or
the data pipeline that alters the result now fails CI loudly. Without it, your backtest silently
changes meaning over months and you will never know which historical result to trust.

---

## SECTION 28 — ROADMAP

I have **reordered your phases**. Your draft builds the agent system (Phase 3) before data (4) and
backtesting (5). That is backwards: agents amplify a research process, so if the process does not
exist yet, agents amplify nothing — expensively. **Build the process, prove it manually once, then
hire the agents.**

| Phase | Goal | Exit criteria |
|---|---|---|
| **0 · Foundation** | Repo, uv, git, Obsidian, `.claude/` skeleton, disk hook | `uv run supercortex --version` works; disk guard denies a write below 2 GB |
| **1 · Registry + Second Brain** | `registry.sqlite`, ID system, vault structure, anti-fabrication hook, CLAUDE.md | An experiment can be registered, a trial recorded, and a fabricated number **rejected** by the hook |
| **2 · Data** | Binance bulk ingest → L0 → L1 catalog, pandera schemas, DATA cards | 10 symbols × 1m + funding + OI, 2019→now, hash-verified, `supercortex data rebuild` works |
| **3 · Backtest** | Nautilus running, one trivial strategy, cost model incl. India tax | Golden backtest test passes; four-number report (gross/net/TDS/tax) produced |
| **4 · Falsification** | All 11 tests, thresholds.yaml, verdict block | Deliberately overfit a strategy and watch the engine **kill** it. This is the acceptance test |
| **5 · Lifecycle** | State machine + promotion gates + DEC ceremony | A strategy cannot be promoted by editing a file |
| **6 · Agents** | 12 subagent definitions, hooks, permission tiers, sandbox | An agent completes a full research cycle unattended and produces a falsification verdict |
| **7 · Research engine** | Literature ingestion, idea generation, scheduled scans | 5 hypotheses generated, pre-registered, and tested end-to-end |
| **8 · Risk + Portfolio** | Risk engine, limits.yaml, kill switch, portfolio construction | Chaos tests pass; kill switch triggered successfully |
| **9 · Paper trading** | Testnet, reconciliation, monitoring, degradation detection | **90 days** continuous paper, 0 reconciliation breaks |
| **10 · Live (tiny)** | Real capital, ≤10% target size, exchange-side stops | 30 days live, realised ≈ modelled, zero incidents |
| **11 · Autonomous research** | Scheduled overnight cycles, self-directed hypothesis generation | The desk produces a reviewed research note without you starting it |

**Phases 0–5 are one person, ~4–6 weeks. Do not skip to 6.**

Each phase in detail (goal / tools / files / commands / test / exit) is in §29–§31 and the
checklist in §40.

---

## SECTION 29 — INSTALLATION PLAN

**NOT YET. This is the plan for after you approve the architecture.** No command below has been run.

```bash
# ── 0. FIRST: make space. Nothing else works at 7.2 GB free. ──────────────
#    Target: ≥ 40 GB free, or plug in an external SSD.

# ── 1. Toolchain (Homebrew already present: 6.0.17) ──────────────────────
brew install git ripgrep jq gitleaks sqlite duckdb
brew install --cask obsidian
# uv 0.11.7 already installed ✓   git 2.50.1 ✓   gh (authenticated) ✓

# ── 2. Project ───────────────────────────────────────────────────────────
cd ~/Quant-desk
uv init --python 3.12 --name supercortex
uv add nautilus_trader polars duckdb pyarrow statsmodels arch scipy \
       scikit-learn pandera pydantic pyyaml rich typer quantstats ccxt httpx
uv add --dev pytest pytest-cov ruff mypy hypothesis pre-commit marimo
uv lock

# ── 3. Verify Nautilus on Apple Silicon (single most important check) ────
uv run python -c "import nautilus_trader as n; print(n.__version__)"

# ── 4. Git + secret scanning ─────────────────────────────────────────────
git init && gh repo create Quant-desk --private --source=. --remote=origin
pre-commit install        # gitleaks + ruff + mypy + generated-file guard

# ── 5. Obsidian ──────────────────────────────────────────────────────────
#    Open ~/Quant-desk as a vault. Enable core plugins: Properties, Bases,
#    Graph, Backlinks, Templates, Canvas. Install ZERO community plugins.

# ── 6. Claude Code ───────────────────────────────────────────────────────
#    Write CLAUDE.md, .claude/settings.json (hooks + deny rules),
#    .claude/rules/*, .claude/agents/*, .claude/skills/*
claude   # then: /sandbox  → enable, set network allowlist
         #       /context  → confirm memory files loaded
         #       /doctor   → health check
```

**Decisions embedded above:** `uv` over Poetry (single binary, 10–100× faster, better lockfile);
**no Docker/OrbStack** (§21.1); Python **3.12** (broadest wheel availability within Nautilus's
3.12–3.14 support `[V]`); `duckdb` CLI installed for ad-hoc queries outside Python.

---

## SECTION 30 — FIRST 7 DAYS

| Day | Do | Exit test |
|---|---|---|
| **1** | **Free up disk** (target ≥40 GB) or order the SSD. Install Homebrew packages. `uv init`. `git init` + private GitHub repo. Create the folder tree from §7.2. | `ls` matches §7.2; `df -h` shows ≥40 GB |
| **2** | Write root `CLAUDE.md` (§8.3), `.claude/rules/*`, `.claude/settings.json` with deny rules. Enable `/sandbox` + network allowlist. Test that a denied read is actually denied. | Ask Claude to `cat .env` → **denied** |
| **3** | Build the registry: schema (§12.2), `supercortex exp register/run`, append-only trigger, ID allocator. | `supercortex exp register` creates EXP-000001; deleting a trial row **fails** |
| **4** | **Build the anti-fabrication hook** (§16.4). Then ask Claude to write a results note with a made-up Sharpe. | The hook **blocks it** |
| **5** | Install Nautilus. Ingest **one** symbol (BTCUSDT 1m, 2023–2026) from `data.binance.vision`. Write the pandera schema and the DATA card. | `supercortex data verify DATA-000001` passes; gaps inventoried |
| **6** | Run the world's dumbest strategy (SMA cross) end-to-end through `supercortex exp run`. Write the India cost model. | Four numbers produced: gross → net costs → net TDS → net tax |
| **7** | Write the golden backtest test. Commit everything. Write `ADR-001`…`ADR-010` (§35) into `20-decisions/`. Open the vault in Obsidian and build one Base. | `uv run pytest` green; Obsidian dashboard renders from frontmatter |

**End of week 1 you have:** a registry that cannot lie, a hook that blocks fabricated numbers, one
verified dataset, one reproducible backtest with honest post-tax costs, and a golden test. That is
a real quantitative research desk. It has zero agents and zero strategies, and that is correct.

---

## SECTION 31 — FIRST 30 DAYS

| Week | Focus | Deliverable |
|---|---|---|
| **1** | Foundation + registry + first backtest | §30 |
| **2** | **Falsification engine.** Implement all 11 tests. Vendor `timeseriescv`, write CPCV/PBO/DSR yourself. Wire `arch.bootstrap.SPA`. | `supercortex falsify EXP-000001` produces a full verdict. **Acceptance test: deliberately overfit a strategy on 2021 data and confirm the engine kills it.** |
| **3** | **Lifecycle + data breadth.** State machine, promotion gates, DEC ceremony. Ingest 10 symbols + funding + OI. Risk engine skeleton with limits.yaml. | A strategy cannot change state except through the CLI; 10 datasets verified |
| **4** | **Agents.** Write the 12 subagent definitions, the hooks, permission tiers. Then run **one real research cycle end to end** with agents doing the work. | An agent-produced experiment with a real falsification verdict — most likely a FAIL, which is a success |

**Explicitly NOT in the first 30 days:** live trading, real money, Prometheus/Grafana, portfolio
optimisation, forex, order books, MCP servers, vector search, cloud.

**Realistic expectation:** at day 30 you will probably have **zero strategies that survive
falsification**. That is the correct and healthy outcome. Most ideas are noise. A desk that produces
a validated strategy in month one has a broken falsification engine, not a good researcher.

---

## SECTION 32 — COST

### Software

| Tier | Contents | Cost |
|---|---|---|
| **$0** | Everything in §4. Python, Nautilus, DuckDB, Polars, SQLite, Parquet, statsmodels, arch, Obsidian (personal), Claude Code (your existing Max sub), Git, GitHub private, uv, ripgrep, gitleaks, ntfy | **₹0** |
| **<$20/mo** | + Obsidian Commercial licence if you incorporate (`[V]`: required for orgs with 2+ employees; solo use is free) + a domain | ~$0–10 |
| **<$50/mo** | + Backblaze B2 backup (~$6/TB) + Tailscale (free tier fine) | ~$10 |
| **<$100/mo** | + small VPS for redundant execution (Phase 9), $12–40 | ~$50 |
| **Production** | + paid data (Tardis ~$100–500/mo; Databento usage-based) | $150–600 |

### Hardware — the only spend I actively recommend

| Item | Cost | Priority |
|---|---|---|
| **2 TB external NVMe SSD (Thunderbolt)** | ₹12,000–18,000 | **BUY NOW.** §0.1. Highest-leverage purchase available |
| UPS (small) | ₹5,000–8,000 | Before live trading |
| Second SSD for backup | ₹8,000 | Before live trading |

### Data / brokers / venues

| Item | Cost |
|---|---|
| Binance/Bybit/OKX historical bulk archives | **Free** `[V]` |
| Dukascopy FX tick | Free |
| FRED macro | Free |
| arXiv / OpenAlex | Free (OpenAlex key gives $1/day credit `[V]`) |
| Exchange trading fees | 2–10 bps/side — **model at 2×** |
| **India VDA tax** | **30% + cess on gains, no loss offset, 1% TDS** — by far your largest cost |

### The honest total

**Phase 0–8: ₹0/month in software + ~₹15,000 one-off for the SSD.**
Your real costs are the Claude Max subscription you already pay, your time, and — dominating
everything — the Indian tax regime. **Model the tax before you spend a rupee on data.**

---

## SECTION 33 — BUILD VS BUY (FINAL TABLE)

| Component | Decision | Choice | Rationale |
|---|---|---|---|
| Trading/backtest engine | **BUY (OSS)** | NautilusTrader | Years of correctness work; backtest==live |
| Exchange connectivity | **BUY (OSS)** | Nautilus adapters | Reconnects/idempotency/recovery are where money dies |
| Market data storage | **BUY (OSS)** | Parquet + ParquetDataCatalog | Already inside the engine |
| Analytics | **BUY (OSS)** | DuckDB + Polars | Nothing to beat here |
| Statistics | **BUY (OSS)** | statsmodels, `arch`, scipy | `arch` has SPA/RealityCheck `[V]` |
| Portfolio optimisation | **BUY (OSS)** | skfolio / Riskfolio-Lib | Numerical stability is a specialty |
| Agent runtime | **BUY (product)** | Claude Code subagents | Depth-3 + per-agent permissions, free |
| Notes/knowledge UI | **BUY (product)** | Obsidian | It's a viewer; zero lock-in |
| Version control | **BUY** | Git + GitHub | Obviously |
| Scheduler | **BUY (OS)** | launchd | Already installed |
| Secrets | **BUY (OS)** | macOS Keychain | Never roll your own |
| **Experiment registry** | **BUILD** | SQLite + CLI | Nobody models trial counts for DSR |
| **Falsification engine** | **BUILD** | 11-test battery | Nobody ships a promotion-blocking battery |
| **India cost model** | **BUILD** | 30%/no-offset/1% TDS | Unique to your jurisdiction |
| **Lifecycle state machine** | **BUILD** | Promotion gates | Encodes *your* risk appetite |
| **Agent governance** | **BUILD** | Hooks + anti-fabrication validator | The control that makes agents trustworthy |
| Purged CV / CPCV / PBO / DSR | **BUILD** (vendor MIT `timeseriescv` as a start) | — | mlfinlab is proprietary `[V]`; pypbo is AGPL + dormant |
| Monitoring | **BUY, LATER** | Prometheus + Grafana (Phase 8) | Markdown + ntfy until then |
| Workflow orchestration | **NEITHER** | launchd + Python | You don't have this problem |
| Vector search | **NEITHER** | ripgrep | Revisit at 5,000 notes |
| Feature store | **NEITHER** | Parquet + DuckDB | That *is* a feature store |

---

## SECTION 34 — FINAL RECOMMENDATION: THE STACK

No ambiguity.

```
MACHINE     Mac Mini M4, 16 GB · macOS 26 · + 2 TB external NVMe (BUY THIS)
RUNTIME     Native macOS. launchd. NO Docker.
LANGUAGE    Python 3.12
PACKAGING   uv
ENGINE      NautilusTrader (LGPL-3.0)
DATA        Parquet + Nautilus ParquetDataCatalog
ANALYTICS   DuckDB + Polars
METADATA    SQLite (registry.sqlite)
VALIDATION  pandera
STATS       NumPy · SciPy · statsmodels · arch · scikit-learn
PORTFOLIO   skfolio (Phase 7)
REPORTING   QuantStats + Jinja templates
NOTEBOOKS   marimo (never Jupyter)
BRAIN       Markdown + Git + Obsidian (core plugins only: Properties, Bases, Graph, Canvas)
SEARCH      ripgrep. No vector DB.
AGENTS      Claude Code subagents (12 roles, depth 3) + skills + PreToolUse hooks + Seatbelt sandbox
SCHEDULER   launchd
SECRETS     macOS Keychain (T3 live keys never reachable by an agent)
SCANNING    gitleaks (pre-commit)
MONITORING  health.md + ntfy.sh  →  Prometheus + Grafana at Phase 8
VENUES      Binance (primary) → Bybit (secondary). Testnet until day 90+.
FOREX       RESEARCH ONLY (Dukascopy data) until FEMA position is resolved with counsel.
CLOUD       None until Phase 9. Then one small VPS for redundant execution.
```

**Build ourselves:** registry · falsification engine · India cost model · lifecycle state machine ·
agent governance + anti-fabrication validator. **That is all.**

---

## SECTION 35 — ARCHITECTURE DECISION RECORDS

Each becomes a file in `20-decisions/`.

**ADR-001 — Second Brain: Markdown + Git + Obsidian; SQLite for facts; no vector DB.**
*Context:* need human and AI usability with transparency. *Decision:* Markdown holds judgment,
SQLite holds facts, Obsidian is a deletable viewer, `ripgrep` is search. *Consequences:* total
transparency, zero lock-in, no semantic recall. *Revisit:* >5,000 notes → add LanceDB as an index.

**ADR-002 — Quant Engine: NautilusTrader.** *Decision:* single engine for backtest and live.
*Consequences:* LGPL-3.0 (no obligation for internal, non-distributed use — get counsel before
distributing); Rust build complexity if we ever fork. *Rejected:* LEAN (C#, cloud gravity),
vectorbt (Commons Clause + second truth), Backtrader (unmaintained `[V]`).

**ADR-003 — Data: Parquet + DuckDB + SQLite. No database server.**
*Consequences:* zero ops, ~4 GB ceiling until SSD. *Revisit:* >500 GB or multi-machine access →
consider ClickHouse.

**ADR-004 — Agent Framework: Claude Code subagents, not agent teams, not LangGraph.**
*Decision:* subagents for hierarchy (depth 3 `[V]`), teams only as a manual adversarial technique.
*Consequences:* per-role permissions possible; tied to Claude Code. *Revisit:* if per-agent
permission control ever lands in teams.

**ADR-005 — Orchestration: launchd + Python CLI. No workflow engine.**
*Revisit:* >20 interdependent scheduled jobs → Prefect.

**ADR-006 — Experiment Tracking: custom SQLite registry, not MLflow.**
*Rationale:* the trial counter feeding DSR is not a feature of any tracker. *Revisit:* >100k trials.

**ADR-007 — Risk: custom engine in the order path, limits in a human-only YAML file.**
*Consequences:* strategies structurally cannot bypass risk.

**ADR-008 — Execution: Nautilus adapters. Crypto only in Phase 1. FX research-only.**
*Rationale:* §0.2. *Revisit:* after FEMA counsel.

**ADR-009 — Monitoring: Markdown + ntfy now, Prometheus + Grafana at Phase 8.**
*Rationale:* a monitoring stack for two strategies costs more RAM than it saves risk.

**ADR-010 — Security: 4-tier credentials, T3 unreachable by agents, withdrawals disabled.**
*Rationale:* the exchange-side control is the one that actually bounds worst-case loss.

---

## SECTION 36 — RED TEAM: ATTACKING THIS ARCHITECTURE

I designed it. Now I will try to break it.

### 36.1 The ten ways this fails

**1. The registry is bypassed and every statistic becomes fiction. [HIGHEST RISK]**
One `python backtest.py` run outside the CLI and the trial count is wrong forever — silently, with
no error, and every DSR from then on is falsely reassuring.
→ *Mitigation:* `PreToolUse` hook denying direct engine invocation; a `health` check that greps
session transcripts for unregistered runs; make `supercortex exp run` **easier** than the bypass.
Convenience is the only durable enforcement.

**2. India's tax regime makes the whole thing uneconomic.**
You could build all of this beautifully and discover that no strategy survives 30% + 1% TDS.
→ *Mitigation:* **Build the cost model in week 1, before the falsification engine.** Test three
canonical strategies (trend, carry, funding-harvest) against it in month 1. If nothing survives,
you have learned that in 30 days instead of 12 months. **This is the cheapest possible failure and
you should actively seek it.**

**3. Overengineering — the report itself is the threat.**
This document describes 12 agents, 11 tests, 6 data layers and a state machine. A solo operator can
build maybe 30% of it before losing momentum.
→ *Mitigation:* §37. Phases 0–5 are the product; everything after is optional. **If you build only
the registry, the cost model and the falsification engine, you will already be doing better science
than most retail quants.**

**4. Agents fabricate results that look rigorous.**
The most dangerous failure mode is not a wrong number — it is a *plausible* number in a
well-formatted table with a citation.
→ *Mitigation:* §16.4 anti-fabrication validator + agents return paths, not summaries + §11.4
citation-requires-artifact. These are the three highest-value controls in the report.

**5. Single point of failure: one Mac, one house, one Indian power grid.**
→ *Mitigation:* exchange-side stops on every live position; strategy horizons ≥ days; a VPS at
Phase 9. **Do not run intraday live from home.**

**6. Disk exhaustion corrupts data mid-write.**
7.2 GB free is one careless download from failure.
→ *Mitigation:* 2 GB floor hook + SSD + auto-prune of L0 after L1 verification.

**7. Complexity of the agent layer exceeds its value.**
Twelve agents that each need prompt maintenance can consume more time than they save.
→ *Mitigation:* **Start with three** — Research, Quant, Falsification. Add a role only when you can
name the task you keep doing manually that it would take over.

**8. LGPL-3.0 on Nautilus creates a problem later.**
For internal, non-distributed use: no obligations trigger. If you ever ship software, fork Nautilus,
or offer a hosted product, this becomes a real question.
→ *Mitigation:* never modify Nautilus in-tree (subclass and compose instead); record this in
ADR-002; get counsel before any distribution.

**9. Claude Code changes underneath you.**
Subagent depth, teams, hooks — all evolving. An architecture welded to today's flags will crack.
→ *Mitigation:* the trading system has **zero runtime LLM dependency**. If Claude Code vanished,
`supercortex` keeps running. The agents are a research accelerator, never a component.

**10. Backtest deception via the cost model itself.**
The cost model is code you wrote. If it is optimistic, every downstream test inherits the optimism
and the falsification engine will confidently approve garbage.
→ *Mitigation:* ≥90% unit test coverage; **calibrate against realised paper-trading fills** and
treat divergence as a defect in the model, not noise; always report at 2× costs.

### 36.2 What I would cut if you gave me one hour

Delete: MCP servers · vector search · Prometheus/Grafana · Docker · portfolio optimisation ·
9 of the 12 agents · forex · order books · feature store · workflow engine · Optuna · SHAP.

**Keep: registry · cost model · falsification engine · Nautilus · Parquet/DuckDB/SQLite ·
Markdown+Git · three agents · hooks.**

That is the real system. The rest is scaffolding you may or may not ever need.

### 36.3 The uncomfortable question

Your brief asks for an autonomous quant research desk. But the binding constraint on your returns
is not research throughput — it is (a) the Indian tax regime, (b) 7.2 GB of disk, and (c) the fact
that low-turnover crypto strategies have a small, crowded opportunity set.

**An agent swarm makes you faster at generating hypotheses. It does not make the hypotheses better,
and it multiplies your multiple-testing burden.** The highest-return activity available to you is
not building 12 agents — it is building the falsification engine and pointing it at three simple,
economically-motivated strategies. If you never build the swarm, you lose very little. If you build
the swarm without the registry, you will lose money while feeling rigorous.

---

## SECTION 37 — MINIMAL VIABLE QUANT DESK

Nine components. Buildable in two weeks. **This is what you should actually build first.**

```
1. Mac Mini + external SSD
2. Claude Code (you have it)
3. Obsidian (viewer only)
4. Git + private GitHub repo
5. Python 3.12 + uv
6. NautilusTrader                 ← the one engine
7. Parquet + DuckDB + SQLite      ← the one storage layer
8. data.binance.vision            ← the one data source
9. Binance testnet                ← the one venue
```

Plus exactly four pieces of your own code:

```
supercortex/registry     ← count every trial
supercortex/costs        ← India cost model (30% / no offset / 1% TDS)
supercortex/falsify      ← the 11 tests
.claude/hooks/           ← the anti-fabrication validator + disk guard
```

**Everything else in this report is optional.** Agents, teams, monitoring stacks, portfolio
optimisation, MCP, forex, order books — all optional. If you build the nine plus the four and never
build anything else, you have a functioning, honest quantitative research desk.

---

## SECTION 38 — THE IDEAL QUANT DESK (18–24 months out)

- **Data:** 2–4 TB across 5 venues; tick and L2 order book; macro; on-chain; point-in-time universe.
- **Research:** 10–20 hypotheses/month generated and pre-registered autonomously overnight; literature
  ingestion running continuously; failure-mining producing as many ideas as fresh observation.
- **Falsification:** the same 11 tests, plus regime-conditional CPCV and a strategy-similarity check
  that flags "you've tested this idea before under a different name" — the multiple-testing problem
  you cannot see.
- **Portfolio:** 5–15 low-correlation strategies, vol-targeted, correlation-aware sizing,
  counterparty caps across 3 venues.
- **Execution:** primary on the VPS, Mac Mini as hot standby, exchange-side stops always on,
  reconciliation every 30 s.
- **Monitoring:** Prometheus + Grafana + phone alerts + a weekly auto-generated research review.
- **Agents:** 12 roles, spawned on demand, running overnight cycles, producing artifacts you review
  each morning over coffee.
- **Governance:** every number traceable to an artifact; every promotion traceable to a signed
  decision; every threshold change visible in `git log`.
- **Capital:** sized so that the 95th-percentile Monte Carlo drawdown is an inconvenience, not an event.

**The thing that makes it "ideal" is not the size. It is that after two years you can answer "why do
you believe this strategy works?" with a chain of artifacts instead of a feeling.**

---

## SECTION 39 — ARCHITECTURE SCORE

| Dimension | Score | Reasoning |
|---|---|---|
| **Simplicity** | **8/10** | Zero servers, zero containers, four core storage primitives. Loses 2 for the agent layer's inherent conceptual overhead |
| **Power** | **7/10** | Nautilus is genuinely institutional-grade. Capped by 16 GB RAM and no tick data until the SSD |
| **Reliability** | **6/10** | Excellent software choices, but one machine on Indian home power/internet. Rises to 8 with the Phase 9 VPS |
| **Research capability** | **9/10** | The falsification engine + honest trial counting is better than most retail *and* some professional setups |
| **Agent capability** | **8/10** | Depth-3 hierarchy with per-role permissions and OS-enforced hooks is a genuinely strong design. Loses 2 for dependence on an evolving product |
| **Security** | **8/10** | 4-tier credentials, sandbox, deny rules, withdrawals disabled. Loses 2 because a single compromised laptop is still a bad day |
| **Scalability** | **5/10** | Deliberately. This scales to ~15 strategies and ~1 TB, then needs rearchitecting. **That is the right trade for one person** |
| **Cost** | **10/10** | ₹0/month software. One ₹15,000 SSD |
| **Maintainability** | **8/10** | Few dependencies, no services, everything is a file. Loses 2 for the custom code you must own forever |
| **Mac suitability** | **9/10** | Native ARM64 throughout, no VM, launchd, Keychain. Loses 1 for 16 GB and current disk state |
| **OVERALL** | **7.8/10** | Honest, operable, and correctly sized. It is not the most powerful architecture — it is the most powerful one **you can actually run alone** |

---

## SECTION 40 — THE "DO THIS" CHECKLIST

Exact sequence. No ambiguity.

1. **Free up disk space to ≥40 GB.** Nothing works until this is done.
2. **Order a 2 TB Thunderbolt NVMe SSD.** (~₹15,000. Highest-leverage purchase available to you.)
3. **Email a chartered accountant** with VDA/crypto experience. Ask specifically: 30% VDA treatment,
   1% s.194S TDS mechanics on foreign exchanges, and the 1-Apr-2026 per-transaction reporting rules.
4. **Decide the forex question.** Default answer: **research only, no FX execution.** Write it down
   as `DEC-000001`.
5. `brew install git ripgrep jq gitleaks sqlite duckdb` and `brew install --cask obsidian`.
6. Create `~/Quant-desk`, `git init`, private GitHub repo, `uv init --python 3.12`.
7. Build the folder tree from §7.2.
8. Write root `CLAUDE.md` (§8.3, keep it under 200 lines) and `.claude/rules/*`.
9. Write `.claude/settings.json`: `permissions.deny` + `PreToolUse` hooks (§9.4). **Test that a
   denied read is actually denied.**
10. Enable the Claude Code Bash sandbox (`/sandbox`) with the network allowlist from §22.
11. **Build the disk-guard hook** (deny writes below 2 GB free).
12. **Build `registry.sqlite`** with the schema in §12.2 and the append-only trigger on `trial`.
13. **Build the `supercortex` CLI** with `exp register`, `exp run`, `data verify`, `health`.
14. **Build the anti-fabrication hook** (§16.4). Verify it blocks a made-up Sharpe ratio.
15. `uv add nautilus_trader …` and verify the ARM64 import works.
16. Ingest BTCUSDT 1m from `data.binance.vision`. Write the pandera schema + `DATA-000001` card.
17. **Write the India cost model** (§15.4). Unit-test it to ≥90%.
18. Run one trivial strategy end-to-end. Produce the four numbers: gross → net costs → net TDS → net tax.
19. **Write the golden backtest test.** Commit the expected `metrics.json`.
20. **Build the falsification engine** — all 11 tests (§16.1) with thresholds in git.
21. **Acceptance test: deliberately overfit a strategy and confirm the engine kills it.** If it does
    not, the engine is broken and nothing downstream can be trusted.
22. Build the lifecycle state machine + promotion gates (§13.2).
23. Ingest 10 symbols + funding + open interest. Verify every dataset.
24. Write ADR-001 … ADR-010 into `20-decisions/`.
25. Open the vault in Obsidian. Build one `.base` dashboard. Install **zero** community plugins.
26. **Only now:** write three subagents — Research, Quant, Falsification (§9.3).
27. Run one full research cycle with agents. Expect a FAIL. **Celebrate the FAIL.**
28. Add the remaining agents **only** as you identify specific manual work to delegate.
29. Build the risk engine + `06-risk/limits.yaml` + kill switch. Run the chaos tests.
30. Paper trade on Binance testnet for **90 days**. Reconcile every day.
31. Only after all of the above, and only with your CA's sign-off, consider live capital at ≤10% size.

---

## THE MOST IMPORTANT FINAL QUESTION

> *"If you personally had a Mac Mini, Claude Max, Obsidian, and wanted to build a serious crypto +
> forex AI-native quantitative research desk from scratch in 2026, what exact open-source stack
> would you choose and why?"*

**THE STACK:**

**NautilusTrader** on **Python 3.12** managed by **uv**, storing everything in **Parquet** through
Nautilus's own **ParquetDataCatalog**, queried by **DuckDB** and **Polars**, with **SQLite** as the
experiment registry, **statsmodels + arch + SciPy** for statistics, **pandera** for data contracts,
**marimo** for notebooks, **QuantStats** for reporting, **Markdown + Git + Obsidian** as the second
brain, **Claude Code subagents + hooks + the Seatbelt sandbox** as the agent layer, **launchd** as
the scheduler, **macOS Keychain** for secrets, **gitleaks** for scanning — running **natively on
macOS with no Docker and no database server** — plus five pieces of custom code: **the experiment
registry, the falsification engine, the India cost model, the strategy lifecycle state machine, and
the anti-fabrication validator.**

**Crypto on Binance and Bybit. Forex as research only, until FEMA counsel says otherwise.**

**Why this stack and not another:**

**One engine, one truth.** NautilusTrader is the only mature open-source engine where the strategy
that ran your backtest is *literally the same object* that trades live. Every other choice forces a
rewrite, and the rewrite is where backtests quietly stop describing reality.

**No servers, because you are one person.** Every database I rejected — ClickHouse, Timescale,
QuestDB, Postgres — is excellent and would be correct for a team. For you they are pagers. Parquet
files plus DuckDB give you better query performance at 4 GB than any of them, and the only thing you
back up is a folder.

**The transparency chain is unbroken.** Market data is a file you can open. Metrics are a JSON you
can read. Judgments are Markdown you can diff. Decisions are commits you can sign. There is no
opaque service anywhere in the chain, which means that when a strategy fails — and it will — you can
walk backwards through the exact reasoning that produced it. Add a vector database and you break that
chain in the one place it matters most: retrieval.

**The agent layer is an accelerator, never a dependency.** Claude Code subagents nest exactly three
levels — your CIO/Director/Worker hierarchy — with per-role tool permissions declared in Markdown
and enforced by hooks the model cannot talk its way past. But if the API went dark tomorrow,
`supercortex` would keep trading. **An LLM must never sit in the path of a live order**, and in this
design it structurally cannot.

**And the reason for the five custom pieces:** because the hard problem in systematic trading is not
finding strategies — agents make that trivially easy, which is exactly the danger. The hard problem
is *not fooling yourself*. Nothing off the shelf counts your trials honestly, models a 30% tax with
no loss offset, blocks a promotion on a Deflated Sharpe Ratio, or physically prevents an agent from
writing a number it did not compute. Those five components are your entire edge. Everything else in
this stack is plumbing that someone smarter than both of us already got right.

---

## SOURCES

**Verified via GitHub REST API, 2026-08-24** (licence SPDX, stars, last push, latest release):
nautechsystems/nautilus_trader · QuantConnect/Lean · mementum/backtrader · polakowo/vectorbt ·
stefan-jansen/zipline-reloaded · freqtrade/freqtrade · hummingbot/hummingbot · jesse-ai/jesse ·
microsoft/qlib · pmorissette/bt · ccxt/ccxt · bmoscon/cryptofeed · duckdb/duckdb · pola-rs/polars ·
apache/arrow · ClickHouse/ClickHouse · timescale/timescaledb · questdb/questdb · influxdata/influxdb ·
apache/iceberg · delta-io/delta · lancedb/lancedb · pandas-dev/pandas · statsmodels/statsmodels ·
bashtage/arch · scikit-learn/scikit-learn · optuna/optuna · mlflow/mlflow · aimhubio/aim ·
clearml/clearml · iterative/dvc · ranaroussi/quantstats · quantopian/empyrical · quantopian/pyfolio ·
quantopian/alphalens · robertmartin8/PyPortfolioOpt · dcajasn/Riskfolio-Lib · skfolio/skfolio ·
cvxpy/cvxpy · pymc-devs/pymc · shap/shap · esvhd/pypbo · hudson-and-thames/mlfinlab ·
sam31415/timeseriescv · PrefectHQ/prefect · dagster-io/dagster · apache/airflow · temporalio/temporal ·
celery/celery · ray-project/ray · dask/dask · langchain-ai/langgraph · microsoft/autogen ·
crewAIInc/crewAI · pydantic/pydantic-ai · modelcontextprotocol/python-sdk · prometheus/prometheus ·
grafana/grafana · grafana/loki · VictoriaMetrics/VictoriaMetrics · getsentry/sentry ·
open-telemetry/opentelemetry-python · astral-sh/uv · python-poetry/poetry · hashicorp/vault ·
openbao/openbao · Infisical/infisical · getsops/sops · marimo-team/marimo · unionai-oss/pandera ·
redis/redis · valkey-io/valkey · sktime/sktime · feast-dev/feast · great-expectations/great_expectations

**Licence files read directly from the repository:** vectorbt (Apache-2.0 **+ Commons Clause**),
cryptofeed (custom BSD-style), timescaledb (Apache-2.0 + **Timescale Licence**), arch (permissive
custom), sentry (**FSL-1.1-Apache-2.0**), celery (BSD-3-Clause), redis (**RSALv2/SSPLv1/AGPLv3**),
mlfinlab (**proprietary licensing agreement**).

**Official documentation:**
- https://code.claude.com/docs/en/sub-agents — frontmatter fields, depth-3 nesting, 20-concurrent limit, tool filters
- https://code.claude.com/docs/en/memory — CLAUDE.md precedence, 200-line guidance, `.claude/rules/` path scoping, auto memory
- https://code.claude.com/docs/en/hooks — full event list, `permissionDecision`, exit-code-2 semantics
- https://code.claude.com/docs/en/skills — SKILL.md format, progressive disclosure
- https://code.claude.com/docs/en/agent-teams — experimental status, no nesting, permission inheritance
- https://code.claude.com/docs/en/sandboxing — macOS Seatbelt sandbox, network allowlist
- https://nautilustrader.io/docs/latest/integrations/ — 19 stable adapters
- https://nautilustrader.io/docs/latest/getting_started/installation/ — Python 3.12–3.14, macOS ARM64
- https://nautilustrader.io/docs/latest/concepts/data/ — ParquetDataCatalog, DataFusion, funding-rate types
- https://nautilustrader.io/legal/open-source-licensing/ — LGPL-3.0-only
- https://data.binance.vision/ and github.com/binance/binance-public-data — free bulk archives
- https://orbstack.dev/pricing — Pro required for commercial use
- https://obsidian.md/help/teams/license — commercial licence guidance

**Regulatory context (secondary sources; NOT legal advice — verify with counsel):**
RBI Alert List of unauthorised forex platforms · FEMA 1999 penalties · SEBI-permitted currency pairs ·
LRS USD 250,000 limit and leveraged-product exclusion · Income Tax Act s.115BBH (30% VDA) and s.194S
(1% TDS) · Income Tax Act 2025 per-transaction VDA reporting effective 2026-04-01 · FIU-IND VDASP
registration status of Binance and Bybit.

---
*End of report. Nothing has been installed. Nothing has been built. Awaiting your approval.*
