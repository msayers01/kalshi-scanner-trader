# Architecture — Kalshi Scanner & Auto-Trader (`kst`)

> Status: v0.1 base. This document describes the target architecture and the
> current implementation. Where the code does not yet implement something, the
> section says **(roadmap)**. Keep this file current when you change structure.

---

## 1. Goals and non-goals

### Goals (in priority order)

1. **Profit.** Every component is judged by whether it raises risk-adjusted,
   after-fee P&L. That rules out trading noise: a strategy trades only when its
   expected value, net of fees and model uncertainty, is positive.
2. **Capital safety.** A kill switch, pause, hard risk limits, exchange-side
   safety nets, and fail-safe defaults: paper mode by default, a persisted kill
   state, and staleness guards. Losing money to a bug is worse than missing a
   trade.
3. **Scanner usable on its own.** The scanner is a standalone information tool
   for manual trading, and the same engine drives the auto-trader.
4. **Low latency from engine to screen.** The web UI and Discord show engine
   state in near real time, with push updates and no polling.
5. **Token-frugal LLM assistance.** An LLM, local (Qwen via Ollama or LM
   Studio) or cloud (Claude or OpenAI), decides only *which markets to enter*.
   It sees compact, cached, batched decision packets under a hard daily budget.
6. **Auditability.** Every order, fill, LLM decision, limit change, and
   kill or pause is persisted. P&L summaries are logged daily and on demand.
7. **Maintainable by future engineers and AI agents.** The codebase has clear
   module boundaries, typed interfaces, pure and testable math, and docs next
   to the code.

### Non-goals (for now)

- Multi-user or multi-tenant operation. This is a single-operator tool.
- Sub-millisecond latency, colocation, or FIX. REST and WebSocket are enough at
  our time scales (100 ms to seconds).
- Perps or margin products. Only Kalshi event contracts are in scope.
- Running as a hosted service. It runs on the operator's PC or a small VPS.

---

## 2. System context

```
                ┌──────────────────────── operator ───────────────────────┐
                │  Browser (PC)            Discord app (phone)            │
                └──────┬──────────────────────────┬───────────────────────┘
                       │ HTTP + WebSocket         │ Discord gateway
                       ▼                          ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                         kst  (single Python process)                     │
│                                                                          │
│   Web API ─┐   Discord bot ─┐                                            │
│            ▼                ▼                                            │
│        ControlService  (the ONE command entry point: pause/kill/limits/  │
│            │            manual orders)                                   │
│            ▼                                                             │
│   ┌──────────── AutoTrader ─────────────┐                                │
│   │ Scanner → LLM Gate → Strategy Router│→ Risk Engine → OMS → Broker ───┼──► Kalshi REST
│   └─────────────────────────────────────┘        ▲          │            │
│   MarketData (books, tickers, trades) ◄──────────┼──────────┼────────────┼──◄ Kalshi WS
│   Reference feeds (CF Benchmarks/Coinbase, Open-Meteo, NWS)               │
│   Portfolio & P&L ◄── fills/settlements ──────────────────────┘            │
│   Persistence (SQLite) · Recorder (JSONL.gz) · Structured logs            │
└──────────────────────────────────────────────────────────────────────────┘
                       │                          │
                       ▼                          ▼
          Local LLM (Ollama / LM Studio)    Cloud LLM (Anthropic / OpenAI), optional
```

---

## 3. Architectural style and patterns

| Concern | Pattern | Why |
|---|---|---|
| Overall | **Modular monolith, hexagonal (ports and adapters)** | One process gives in-memory latency, trivial deployment, and a kill switch that never crosses a network hop. Domain logic (`domain/`, `pricing/`, `strategies/`, `risk/`) has no imports from adapters (`kalshi/`, `api/`, `discord_bot/`, `llm/providers`). |
| Intra-process communication | **Event-driven pub/sub** (`core/bus.py`) | Components are decoupled. The UI, Discord, the persistence writer, and the recorder are observers and never call into trading code. |
| Hot-path processing | **Conflation / dirty-set loop** | Book deltas only mark markets dirty. The trader evaluates the latest state at a fixed cadence, so it never falls behind a burst of messages. This is the standard design in trading systems. |
| Strategies, fair-value models, LLM providers, feeds | **Strategy pattern + registry** | You add a strategy or model by writing one class and registering it. Selection is data-driven (series and category rules in config). |
| Order generation | **Declarative target orders** | Strategies return the quotes they *want* resting. The OMS diffs desired against live orders and places, amends, or cancels. This is idempotent and self-heals after reconnects or partial failures. |
| Pre-trade risk | **Chain of responsibility** | Each check is a small pure function, so checks are easy to add, test, and report ("rejected by `MaxExposurePerEvent`"). |
| Orders and engine state | **Explicit state machines** | Illegal transitions raise errors. `RUNNING → PAUSED → KILLED` is persisted, so a restart after a kill stays killed. |
| Commands from UI and Discord | **Command service (CQRS-lite)** | Writes go through `ControlService`, which validates, audits, and executes. Reads come from in-memory projections. The web UI and Discord share exactly the same semantics. |
| Exchange vs paper (vs replay) | **Adapter + factory** | The `Broker` protocol has `LiveBroker` and `PaperBroker`. Market-data sources are WS, REST polling, or offline recorded inputs. Replay drives the production scanner/strategies/risk/OMS with `PaperBroker` + `ManualClock`; see `docs/REPLAY.md` for assumptions. |
| Persistence | **Repository + async write-behind queue** | Immutable queued snapshots and serialized atomic batches; explicit kill checkpoints await durable commit. Disk I/O uses the async DB adapter. |
| Long-running tasks | **Supervisor** | Crashed tasks restart with backoff. Critical worker failures immediately invoke KILL before retry; DB writer is critical. |
| Wiring | **Composition root** (`app.py`), manual dependency injection | No DI framework magic. One file shows how the system is assembled. |

---

## 4. Technology choices

| Layer | Choice | Rationale |
|---|---|---|
| Language | **Python 3.12+**, fully type-hinted, `asyncio` | It has the best quant ecosystem (numpy, scipy), mature Discord, FastAPI, and LLM SDKs, and the operator's local-model tooling is Python-friendly. Our latency needs are milliseconds, which asyncio handles easily. |
| Packaging | **uv** + `pyproject.toml` (hatchling) | It is fast, reproducible (`uv.lock`), and one tool covers venv, deps, and running. |
| HTTP client | **httpx** (async, HTTP/2 capable) | It is async, supports timeouts and connection pooling, and is easy to mock (`respx`). |
| WebSocket client | **websockets** | Kalshi's docs use it. It has built-in ping/pong keepalive. |
| Crypto (API signing) | **cryptography** | Supports RSA-PSS (SHA-256, MGF1, digest-length salt) and Ed25519, the two key types Kalshi accepts. |
| Data models | **pydantic v2** (config, API DTOs) + **dataclasses** (hot-path domain objects) | Pydantic validates at the edges. Slotted dataclasses avoid validation overhead in the loop. |
| Config | **pydantic-settings** + YAML (`config/default.yaml` + `config/local.yaml`) + `.env` | Typed, layered, and secrets stay out of git. |
| Math | **numpy**, **scipy.stats** | Normal and Student-t CDFs, ensemble statistics. |
| Web backend | **FastAPI** + **uvicorn** | Async-native, gives WebSocket and REST in one app, auto OpenAPI docs, and runs in the same event loop as the engine. |
| Web frontend | **React 19 + TypeScript + Vite + Zustand**, plain CSS | Small and fast, with one WebSocket feeding a single store. No component library, to keep the UI lean ("only the necessities"). |
| Discord | **discord.py 2.x** (slash commands + persistent button views) | Mature, asyncio-native, and runs in the same loop. |
| LLM | **OpenAI-compatible HTTP** via httpx (covers Ollama, LM Studio, vLLM, llama.cpp server, OpenAI) + the official **`anthropic` SDK** | One generic adapter covers every local runtime. The Claude adapter uses the SDK for prompt caching, JSON-schema structured output, and low effort. Budgets and timeouts are enforced by the gate, not the providers. |
| Persistence | **SQLite (WAL)** via **SQLAlchemy 2.0 async + aiosqlite** | Zero-ops for a single user, with separate DB files per mode (`paper`, `demo`, `live`) so P&L never mixes. Tables are created at startup in v0.1; add Alembic before the first schema change on real data (`docs/RUNBOOK.md`). Postgres is a URL change away. |
| Logging | **structlog** (JSON to file, pretty console) | Machine-parseable logs. The P&L ledger also goes to `data/pnl/`. |
| CLI | **Typer** | `kst run`, `kst scan`, `kst llm-packet`, `kst check`, `kst status/pause/resume/kill`, `kst report`. |
| Tests | **pytest**, **pytest-asyncio**, **respx**, **hypothesis** (property tests for money and book math) | |
| Lint and type checks | **ruff** (lint + format), **mypy** (`disallow_untyped_defs` everywhere except `api` and `discord_bot`; pydantic plugin) | |
| CI | GitHub Actions (`.github/workflows/ci.yml`): ruff, mypy, pytest, web typecheck and build | |

---

## 5. Numeric representation (critical)

Kalshi (as of Oct 2026) uses **fixed-point strings**: prices are `*_dollars`
with up to 4 decimal places, and quantities are `*_fp` with 2 decimal places
(fractional contracts allowed). Each market has its own **price grid**
(`price_ranges`), and sub-cent ticks exist. See `docs/KALSHI_API_NOTES.md`.

Internally, money and quantity are always **integers**:

| Type | Unit | Example |
|---|---|---|
| `Price` | $0.0001 (1 = 0.01¢) | `"0.5600"` → `5600`; $1.00 = `10_000` |
| `Qty` | 0.01 contract | `"10.00"` → `1000` |
| `Micros` | $0.000001 | `Price × Qty` = `Micros` **exactly** |

Probabilities and model math use `float`. Conversion happens only at the
boundaries: `fixedpoint.py` for parsing, `PriceGrid.snap_*` for price
decisions. Never put a float into an order payload.

The **single-book convention**: the system thinks in **YES-price space**. A
`BID` means buy YES, and an `ASK` means sell YES, which is the same as buying NO
at `1 − price`. This matches Kalshi's V2 order API (`side: bid|ask`, price
always quoted on the YES leg). Positions are signed: `+q` = q YES contracts,
`−q` = q NO contracts.

---

## 6. Component view

```
src/kst/
├── core/          infrastructure primitives: fixedpoint, types, events, bus, clock, supervisor, logging
├── domain/        pure domain: Market/Event/Series, PriceGrid, OrderBook, Order (+state machine), Position, FeeModel,
│                  views (CatalogView/BookView read-only protocols the pure layers depend on)
├── kalshi/        ADAPTER: auth signer, token-bucket rate limiter, async REST client, WS client, JSON→domain parsing
├── marketdata/    MarketCatalog (universe), MarketDataService (WS | REST-poll sources → books), Recorder (JSONL.gz)
├── feeds/         ADAPTERS: crypto spot/index (Kalshi CF Benchmarks channel, Coinbase fallback), weather (Open-Meteo, NWS)
├── pricing/       pure fair-value math: digital/range pricing w/ averaging window, EWMA vol, weather ensemble,
│                  price calibration (favorite-longshot), Bayesian combination with market price, model resolver
├── scanner/       signals (structural arb, taker edge), service (chunked async scan, tags, ranking → ScanSnapshot)
├── strategies/    Strategy ABC, router, Kelly sizing, EV-based exits, structural_arb, edge_taker, market_maker
├── risk/          RiskLimits (runtime-editable), pre-trade check chain, RiskEngine (breakers, daily counters)
├── execution/     Broker protocol, PaperBroker, LiveBroker, OrderManager (declarative reconciliation)
├── portfolio/     PortfolioService (positions, cash, equity, realized/unrealized), daily P&L reports + ledger, FIFO paper execution scorecard (docs/SCORECARD.md)
├── llm/           decision-packet builder, providers (OpenAI-compatible, Anthropic), LLMGate (cache, budget, batching)
├── engine/        AutoTrader (orchestration loop), ControlService (commands), EngineState store
├── persistence/   SQLAlchemy tables, async DB, write-behind repository
├── api/           app (FastAPI routes, bearer auth, static UI), hub (WebSocket topic coalescing), serialize
├── discord_bot/   bot (slash commands, persistent control-panel view, notifier), formatting (embeds)
├── backtest/      offline recording reader, replay driver, performance metrics, parameter grids
├── calibration/   probability/weather candidate jobs, historical acquisition, archive pairing, diagnostics
├── config.py      typed settings tree (YAML + env)
├── app.py         composition root: build_app(settings) → Application (start/stop)
└── cli.py         Typer CLI
web/               React UI (Vite). `npm run build` → web/dist, served by FastAPI at "/"
config/            default.yaml (committed), local.yaml (git-ignored overrides)
docs/              STRATEGIES.md, RISK.md, LLM_GATE.md, KALSHI_API_NOTES.md, RUNBOOK.md, ROADMAP.md, adr/
```

### Dependency rule

```
api, discord_bot, cli ──► engine ──► strategies, risk, scanner, llm.gate, portfolio, execution.oms
                                          │
                                          ▼
                       pricing, domain, core   (pure; no I/O, no adapter imports)
adapters (kalshi, feeds, llm.providers, persistence, execution.live/paper) implement ports used by engine/services
```

`domain`, `pricing`, `strategies`, `risk.checks`, `risk.limits`, and
`scanner.signals` must not import `kalshi`, `api`, `discord_bot`, `persistence`,
`execution`, `httpx`, `fastapi`, `discord`, `websockets`, or `sqlalchemy`.
`tests/test_architecture.py` enforces this, and also that only `execution/`,
`engine/control.py`, and `app.py` place orders.

---

## 7. Runtime data flow

### 7.1 Market data → decision → order (hot path)

```mermaid
sequenceDiagram
    participant WS as Kalshi WS / REST poll
    participant MD as MarketDataService
    participant SC as Scanner
    participant G as LLM Gate
    participant T as AutoTrader
    participant S as Strategy
    participant R as RiskEngine
    participant O as OMS
    participant B as Broker
    WS->>MD: orderbook_snapshot / delta / ticker / trade
    MD->>MD: apply to OrderBook (seq-checked), mark dirty
    MD-->>SC: BookUpdated (bus)
    loop every scanner.interval (≈1 s)
        SC->>SC: features + fair values + signals → ScanSnapshot
        SC-->>T: ScanUpdated (bus) & focus-set → MD (orderbook subscriptions)
    end
    T->>G: candidates needing a verdict (non-blocking)
    G-->>T: cached verdicts (background batches to LLM)
    loop every engine.tick (≈250 ms) for dirty markets
        T->>S: evaluate(ctx: market, book, fair value, position, limits)
        S-->>T: Decision{takes[], quotes[]}
        T->>R: pre-trade checks (chain)
        R-->>T: approved / rejected(reason)
        T->>O: submit takes; reconcile quotes
        O->>B: place / amend / cancel
        B-->>O: order updates, fills (bus)
    end
```

### 7.2 Fills → portfolio → notifications

`Broker` → `OrderUpdated` / `FillReceived` events → `OMS` (order state) →
`PortfolioService` (position, cash, realized P&L) → `PortfolioUpdated` → web
hub (≤ 1 Hz), Discord notifier (batched), and persistence (write-behind).
`RiskEngine` subscribes to `PortfolioUpdated` to evaluate the daily-loss,
drawdown, and consecutive-loss breakers, which call `ControlService.pause/kill`.

### 7.3 Commands (UI or Discord → engine)

```
Button/slash command or HTTP POST /api/control/kill
   → ControlService.kill(actor="discord:<user_id>", flatten=False)
       1. EngineState := KILLED (persisted immediately)
       2. Broker.cancel_all()          (Kalshi: DELETE /portfolio/events/orders)
       3. optional flatten: IOC exits within slippage band
       4. audit_log row + EngineStateChanged event → UI + Discord within ~100 ms
```

---

## 8. Concurrency model

- **One asyncio event loop** runs the engine, the API server (uvicorn embedded
  via `uvicorn.Server.serve()`), and the Discord bot (`bot.start()`), each as a
  supervised task.
- **Single-writer principle.** Domain state (books, orders, positions) is
  mutated only by coroutines on the loop. There are no threads, so there are no
  locks on the hot path. CPU-heavy work, such as large ensemble downloads and
  parsing, goes through `asyncio.to_thread` or runs outside the hot path.
- **Bus delivery.** `publish()` calls synchronous handlers inline, and they
  must be O(1)-ish. Async consumers (web hub, Discord, DB writer) get **bounded
  queues** with a per-topic policy. Critical events (fills, orders, engine
  state) are never dropped. UI-only events conflate to the latest value.
- **Backpressure.** The WebSocket hub coalesces per topic per client and
  flushes at ≤ 10 Hz. Slow browser clients never slow the engine.
- **Scanner queries.** By default every client gets the same top-N scan rows.
  A client that sends `{"op": "scan_query"}` (column filters and sort from the
  scanner table) gets matches across *every* scanned market instead, built at
  most once per distinct query per flush (`scanner/query.py`).

---

## 9. Modes

| Mode | Market data | Orders | DB file | Notes |
|---|---|---|---|---|
| `paper` (default) | live public data (WS if keys, else REST polling) | `PaperBroker` simulated against live books and trades | `data/kst_paper.db` | Safe default. Validate strategies here first. |
| `demo` | Kalshi demo env | `LiveBroker` → demo API | `data/kst_demo.db` | Demo keys. Exercises the real order lifecycle. |
| `live` | Kalshi prod | `LiveBroker` → prod API | `data/kst_live.db` | Requires `mode: live` in config **and** the `--i-understand-live-trading` CLI flag. |
| `replay` | recorded JSONL(.gz) | `PaperBroker` + `ManualClock` | `data/replays/<run>/variant-NNN/kst_replay.db` | `kst replay`; isolated runs, no network feeds or exchange calls, LLM and fitted calibration disabled. |

The **scanner-only** run (`kst scan` or `engine.autotrade: false`) starts
everything except order submission. The UI trade ticket still works for manual
orders in paper, demo, or live mode.

---

## 10. Safety architecture (defense in depth)

1. **Fail-safe defaults.** Paper mode, autotrade off on first run, an explicit
   live flag, and conservative default limits.
2. **Engine state machine.** `RUNNING` (entries + exits), `PAUSED` (no new
   entries, exits and risk-reducing orders still allowed), `KILLED` (cancel all,
   no orders at all except an explicit flatten). The state is persisted, and
   `KILLED` survives restarts until the operator explicitly resumes it.
3. **Pre-trade check chain.** About 20 checks (see `docs/RISK.md`): the daily
   new-market limit, exposure per market, event, category, and total, order
   size, fat-finger bands, stale data, time-to-close, rate of orders, and more.
4. **Breakers.** Daily loss limit, intraday drawdown, consecutive losses, and
   order-reject storms trigger an automatic PAUSE or KILL.
5. **Exchange-side nets (live).** Every resting order carries
   `cancel_order_on_pause=true` and an `expiration_time` TTL (refreshed by the
   OMS), so a crashed process cannot leave stale quotes forever. An optional
   **Kalshi order group** with a rolling 15 s contract limit auto-cancels
   everything on a runaway-fill burst.
6. **Dead-man's switch.** If the exchange WebSocket is down longer than
   `risk.ws_down_cancel_after_s` while orders are resting, the system cancels
   all orders via REST.
7. **Reconciliation.** In live mode, positions, open orders, and balance are
   periodically reconciled with the exchange. The exchange is the source of
   truth, and mismatches are logged and adopted.
8. **Authorization.** The web API binds to `127.0.0.1` by default. Control
   endpoints require a bearer token whenever the bind is non-local. Discord
   commands check an allow-list of user IDs.

---

## 11. Persistence model

SQLite with one file per mode, WAL and `synchronous=FULL`. Repository batches,
kill checkpoints and paper restart deduplication are described in `docs/RELIABILITY.md`.
Tables (`persistence/tables.py`):

| Table | Content |
|---|---|
| `orders` | every order and its latest state, with strategy attribution and `client_order_id` |
| `fills` | every fill: price, qty, fee, taker/maker, strategy |
| `settlements` | settled positions and payouts |
| `equity_snapshots` | equity, cash, realized/unrealized, exposure, once a minute |
| `daily_pnl` | one row per trading day: realized, fees, trades, markets entered, win rate, per-strategy JSON |
| `llm_decisions` | provider, model, ticker, verdict, confidence, reason, tokens, packet hash |
| `audit_log` | actor, action, payload: kill, pause, limit edits, manual orders |
| `kv_state` | engine state, runtime risk-limit overrides, daily counters |

**Files.** `data/pnl/<day>_<mode>.json` holds the daily P&L ledger.
`data/logs/kst.jsonl` holds structured logs. `data/recordings/` holds market
data recordings for replay.

---

## 12. Strategy layer (summary; math in `docs/STRATEGIES.md`)

The decision framework is shared by all strategies:

```
q̂   = combine(model_probability, market_probability)       # Bayesian, in logit space
EV  = q̂ − price − fee(price)            (buy YES)          # per contract, in $
EV  = price − q̂ − fee(price)            (sell YES / buy NO)
trade iff EV > max(min_edge, z · σ(q̂))   and risk checks pass
size = fractional_kelly(q̂, cost) × bankroll, capped by limits
exit iff liquidation value ≥ hold value − margin            # EV-based, not a naive stop-loss
```

| Strategy | Markets | Edge source |
|---|---|---|
| `structural_arb` | events with mutually-exclusive outcomes; strike ladders | Coherence violations: Σ asks < $1, Σ bids > $1, and non-monotone ladders. Net of fees. Near risk-free. |
| `edge_taker` | markets with a quantitative fair-value model (crypto above/below and ranges, weather highs) | Model probability vs executable price |
| `market_maker` | liquid markets, maker-fee-free series preferred | Avellaneda–Stoikov quoting in logit space around q̂ with inventory skew and toxicity filters |
| (calibrated prior) | any market, used when no specific model exists | Empirical favorite-longshot calibration of Kalshi prices (Bürgi, Deng & Whelan 2025). Used by `market_maker` in the extremes band. Disabled until fitted. |

The router (`strategies/router.py`) picks one strategy per market:
`market_maker` if the series is listed in `strategies.market_maker.series`,
else `edge_taker` if a fair-value model covers the market, else `market_maker`
if the calibrated prior applies, else nothing. Structural arb runs separately
over the scanner's event-level opportunities. The LLM verdict never routes; it
only gates new entries (section 13).

---

## 13. LLM gate (summary; details in `docs/LLM_GATE.md`)

- **Scope.** The gate decides only ENTER or SKIP for *new* entries (plus an
  optional side bias). It never picks prices, sizes, or exits.
- **Token frugality.**
  - It is asked only when a strategy already wants to *enter* a market
    (`llm.scope: all`, the default; `unmodeled_only` skips modeled markets).
    Exits, re-quotes, and `llm.bypass_strategies` (default: `structural_arb`)
    never wait on it.
  - Calls are batched (N markets per call) in a compact pipe-delimited table
    with numeric row IDs.
  - The JSON reply has a ~12-word reason cap.
  - The system prompt is static and cache-friendly.
  - Verdicts are cached per (ticker, state bucket) with a TTL.
  - Hard daily call and token budgets apply, with a fallback policy.
- **Manual use.** `kst llm-packet` and the UI "Copy LLM packet" button produce
  the same packet to paste into ChatGPT or Claude by hand.

---

## 14. Observability

- Structured logs carry `component`, `ticker`, `strategy`, `order_id`, and `latency_ms`.
- `/api/health` reports per-component health: feed connected, last message
  age, loop lag, DB queue depth, and LLM budget left.
- The UI header shows the connection dot, market-data age, and loop lag.
- P&L is reported daily (Discord + `data/pnl/` + log) and on demand
  (`/pnl` in Discord, the UI, and `GET /api/pnl`).
- Equity is snapshotted once a minute while the engine runs. `GET /api/equity?range=1D|1W|1M|3M|1Y|ALL`
  serves it downsampled (`portfolio/history.py`) for the UI's portfolio chart.

---

## 15. Extension points

| To add… | Do this |
|---|---|
| A fair-value model | Implement `pricing.base.FairValueModel` and register it in `pricing/resolver.py` with series or category rules. Add tests with closed-form checks. |
| A strategy | Subclass `strategies.base.Strategy`, register it in `strategies/router.py`, add a config block, and document the math in `docs/STRATEGIES.md`. |
| A risk check | Add a function in `risk/checks.py`, append it to `ALWAYS` (every order) or `ENTRY_ONLY` (orders that open exposure), and add a limit field to `RiskLimits`. |
| An LLM provider | Implement `llm.providers.LLMProvider` (`complete(system, user, max_tokens) -> LLMResult`). |
| A reference data feed | Implement it in `feeds/` and publish `ReferencePriceUpdated` (or a domain-specific event). |
| A UI panel | Add a component under `web/src/components/` and subscribe to a hub topic in `web/src/store.ts`. |

---

## 16. Testing strategy

- **Pure math** (fixed-point, price grid, order book, fees, pricing models,
  Kelly, exits, risk checks, position accounting) is unit tested with
  closed-form and edge cases, plus property tests where invariants exist (book
  never crossed after a snapshot, `Price × Qty` exact).
- **Adapters** use `respx`-mocked REST, recorded JSON fixtures
  (`tests/fixtures/`), and signature verification of the auth signer against a
  public key. The WS client has real loopback-server tests for subscriptions,
  gaps, reconnect and shutdown (`tests/test_kalshi_ws.py`). LiveBroker validation
  and pending demo evidence are described in `docs/EXECUTION_VALIDATION.md`.
- **Integration.** The `Harness` fixture (`tests/conftest.py`) wires a full
  paper engine with no network: catalog, books, risk, OMS, `PaperBroker`,
  portfolio, and ControlService (`tests/test_trading.py`). FastAPI routes are
  tested via ASGI transport (`tests/test_api.py`).
- **Architecture test.** Enforces the dependency rule in section 6.
- **Live smoke test.** `uv run kst run --autotrade` in paper mode against real
  public data. Watch `risk_rejected` counts, order churn, and loop lag.

---

## 17. Known limitations and open decisions

- **Fee coefficients.** Taker 0.07, maker = 0.25× taker on maker-fee series
  (`fee_type` and `fee_multiplier` come from the Series API). Verify against
  kalshi.com/fee-schedule. They are configurable in `fees:`.
- **`flat` fee type.** The schedule for it is not modeled precisely and is
  treated conservatively.
- **Settlement-source nuances** (BRTI 60 s average, NWS/Weather Company highs)
  are modeled. Exotic or custom strikes are skipped by the models.
- **Unauthenticated catalog.** Without API keys, heavy `/markets` pages are
  throttled, so the universe is limited to markets closing within
  `universe.max_hours_to_close` (24 h by default). See `docs/KALSHI_API_NOTES.md`.
- **Replay coverage.** Offline backtesting is available, but requires recorded point-in-time
  metadata and model inputs. Missing history cannot be reconstructed from today's exchange state.
  Model parameters remain uncalibrated defaults until validated. See `docs/REPLAY.md`.
- **Sports, politics, and economics** have no quant model in v0.1. They are
  tradable only via the calibrated prior + LLM gate (maker-only). See
  `docs/ROADMAP.md` for planned models: economic nowcasts and sports win
  probability.
