---
id: ADR-011
type: adr
title: OpenAlgo as the Indian broker gateway
status: ACCEPTED (deferred to Phase 9)
created: 2026-08-24
supersedes_part_of: ADR-008
tags: [execution, india, forex, licence]
---

# ADR-011 — OpenAlgo as the Indian broker gateway

## Context

`ARCHITECTURE-RESEARCH-REPORT.md` §18.2 claimed that the legal FX route for an Indian
resident — exchange-traded currency derivatives on NSE/BSE through a SEBI broker — had
**"No adapter exists — you'd write one, ~2–4 weeks."**

**That claim was wrong.** [OpenAlgo](https://github.com/marketcalls/openalgo) exists.

Verified 2026-08-24 via GitHub API and docs.openalgo.in:

| Property | Value |
|---|---|
| Licence (server) | **AGPL-3.0** |
| Licence (Python client SDK) | **MIT** — `marketcalls/openalgo-python-library` v2.0.0 |
| Stars / forks | 2,507 / 1,191 |
| Last push | 2026-08-24 (same day) |
| Open issues | 336 |
| Created | 2024-02-19 |
| Language | Python 3.12+ (matches our pin), Flask + React 19 |
| Broker plugins | 34–36, incl. Zerodha, Dhan, AngelOne, Fyers, Upstox, 5paisa, AliceBlue |
| Exchanges | NSE, BSE, NFO, BFO, **CDS**, **BCD**, MCX, NCDEX, NCO, crypto |
| Interfaces | REST `http://127.0.0.1:5000/api/v1`, WebSocket `ws://127.0.0.1:8765` |
| Paper trading | "Analyzer Mode" — ₹1 crore sandbox capital |
| Storage | SQLite + DuckDB ("Historify") |

**`CDS` and `BCD` are the currency-derivative segments.** That is precisely the legal FX
path, and it is reachable through one normalised API rather than one adapter per broker.

## Decision

**Adopt OpenAlgo as the broker gateway for Indian exchange-traded instruments — at Phase 9,
not before.** Write a single Nautilus `ExecutionClient` + `DataClient` against OpenAlgo's
REST/WebSocket contract, rather than one adapter per SEBI broker.

**Do not adopt it for Phase 1–8.** We have no execution problem for months, and crypto
research runs on `data.binance.vision`, which OpenAlgo does not replace.

## Licence analysis — the part that needs care

AGPL-3.0 is the strongest copyleft in common use. Its §13 network clause triggers when you
**modify** the program and let **users interact with it over a network**.

Our intended use is the safest possible shape:

- run it **unmodified**, at a **pinned release**
- as a **separate process**, on **localhost**, with **one user (the operator)**
- talking to it over a **documented HTTP/WebSocket API**
- using the **MIT-licensed** client SDK, not the AGPL server code

Separate programs communicating over a network protocol are the classic arm's-length
arrangement; our strategy code does not become a derivative work. Nothing is conveyed to
anyone, and there are no remote users to offer source to, so no AGPL obligation triggers.

**Obligations WOULD trigger if we:** fork or patch OpenAlgo, vendor it into this repo,
expose it to other users over a network, or distribute it inside any product.

**Therefore, binding rules:**
1. Never vendor OpenAlgo into this repository. It is a sibling service, installed separately.
2. Never patch it. Needed behaviour goes upstream as a PR or into our own adapter.
3. If Supercortex ever becomes a product or a hosted service, **get counsel before shipping.**

`UNVERIFIED:` this is a technical licence-risk assessment, not legal advice.

## Consequences

**Good**
- The FX question in §0.2 gains a real, legal, technically-cheap path.
- We do not write broker connectivity — §6 says never to, and this is why.
- Analyzer Mode gives an Indian-market paper-trading surface we would otherwise lack.
- It reaches **Delta Exchange** (FIU-IND registered, INR-settled BTC/ETH futures, perps and
  European options) — a crypto venue with Indian KYC and automatic tax reporting.

**Costs and risks**
- It is a **server**, which cuts against the "no servers" principle in §21. Accepted: broker
  session management is genuinely server-shaped, and the alternative is writing it ourselves.
- Large surface — Flask + React + 34 plugins + 336 open issues — and it will hold **T3 broker
  credentials**. It becomes the most sensitive process on the machine.
- **Security consequence, and this one is easy to miss:** our rule is that agents never reach
  T3 credentials. OpenAlgo puts those behind `127.0.0.1:5000`. A network allowlist that only
  considers *external* hosts would leave that wide open. **The agent sandbox must explicitly
  deny localhost:5000 and localhost:8765.**

## Open question, deliberately not answered yet

**Delta Exchange vs Binance as the crypto execution venue.**

| | Binance | Delta Exchange India |
|---|---|---|
| Legal clarity for an Indian resident | FIU-registered, but INR on/off-ramp is your problem | INR settlement end to end, Indian KYC, auto tax reporting |
| Liquidity / spreads | Deepest in crypto | Materially thinner |
| Free historical data | Excellent (`data.binance.vision`) | Sparse |
| Nautilus adapter | Native, stable | None — would go via OpenAlgo |

This cannot be decided from a table. Researching on Binance data and executing on Delta is a
**venue-mismatch bug** — different depth, different spreads, different funding. The decision
needs a measurement: *does Delta's book support our target size at a slippage the cost model
survives?* That is a Phase 8 question. Recorded here so it is not silently forgotten.

## Report corrections

- §18.2 table, path (A): "No adapter exists (~2–4 weeks)" → **"OpenAlgo, one adapter for 34
  brokers, AGPL-3.0 server + MIT client, Phase 9."**
- §3.4: add OpenAlgo as **ADOPT (Phase 9, execution only)**.
- §19: add `127.0.0.1:5000` and `127.0.0.1:8765` to the agent network **deny** list.
