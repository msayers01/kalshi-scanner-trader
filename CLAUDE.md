# CLAUDE.md: working on `kst` (Kalshi Scanner & Auto-Trader)

Read this first, then `ARCHITECTURE.md`. The math lives in
`docs/STRATEGIES.md`, risk rules in `docs/RISK.md`, the LLM gate in
`docs/LLM_GATE.md`, and **verified Kalshi API facts** in
`docs/KALSHI_API_NOTES.md`. Check those before you touch `src/kst/kalshi/`.

## What this is

This is a single-process Python 3.12 asyncio app. It does five things:

1. Scans Kalshi markets, as a standalone info tool and as the engine of the
   auto-trader.
2. Auto-trades with quantitative strategies (structural arb, model edge,
   market making) behind a risk engine and a kill switch.
3. Asks a local or cloud LLM, frugally, *which markets to enter*.
4. Reports P&L to the logs, the DB, and `data/pnl/`.
5. Exposes control through a React web UI and a Discord bot.

The #1 goal is **after-fee profit**, and the #2 goal is **not losing money to
bugs**.

## Commands

```bash
uv sync --all-extras              # install (creates .venv)
uv run kst --help                 # CLI
uv run kst check                  # validate config, keys, connectivity
uv run kst scan                   # scanner only (web UI + Discord, no auto orders)
uv run kst run                    # full system (paper mode by default)
uv run kst llm-packet --top 20    # print an LLM decision packet
uv run pytest -q                  # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy src
cd web && npm ci && npm run build # UI → web/dist (served by the backend at /)
cd web && npm run dev             # UI dev server on :5173, proxies to :8787
```

`make help` lists the same shortcuts.

## Hard rules (do not break)

1. **Money is integers.** `Price` = $0.0001 units, `Qty` = 0.01 contracts,
   `Micros` = $0.000001 (see `core/fixedpoint.py`). Never send a float to
   Kalshi, never accumulate P&L in floats, and always snap prices through
   `PriceGrid`.
2. **YES-price space everywhere.** `BookSide.BID` = buy YES, `ASK` = sell
   YES (= buy NO). Positions are signed (+YES / −NO). REST orderbook NO bids
   use NO-leg prices and must be converted. The WS subscribes with
   `use_yes_price: true`.
3. **Use V2 order endpoints** (`/portfolio/events/orders...`). The legacy
   `/portfolio/orders` POST/DELETE are deprecated.
4. **Dependency rule.** `domain/`, `pricing/`, `strategies/`, and
   `risk/checks.py` are pure. They must not import `kalshi`, `api`,
   `discord_bot`, `httpx`, `fastapi`, or `discord`. `tests/test_architecture.py`
   enforces this.
5. **Every order goes through `RiskEngine.check()` and then the OMS.** That
   includes manual UI and Discord orders, via `ControlService`. No component
   calls a broker directly except the OMS (and `ControlService.kill` →
   `broker.cancel_all`).
6. **Fail safe.** Paper is the default mode. Live needs `mode: live` **and**
   `--i-understand-live-trading`. `KILLED` is persisted. When in doubt,
   reject the order.
7. **No blocking calls on the event loop.** Use async clients, or
   `asyncio.to_thread` for CPU or disk-heavy work.
8. **Secrets** come only from `.env` or the environment. Never log keys or
   tokens, and never commit `config/local.yaml`, `.env`, `*.pem`, or `data/`.
9. If you change a formula, update `docs/STRATEGIES.md` and its test in the
   same change.

## Layout cheat-sheet

```
src/kst/core/         fixedpoint, types, events (all bus event dataclasses), bus, clock, supervisor, logging
src/kst/domain/       Market/Event/Series, PriceGrid, OrderBook, Order (+FSM), Position, FeeModel
src/kst/kalshi/       auth, ratelimit, rest (KalshiRest), ws (KalshiWs), parse (JSON→domain)
src/kst/marketdata/   catalog (universe), service (books + sources: ws | poll | replay), recorder
src/kst/feeds/        crypto (CF Benchmarks via Kalshi WS, Coinbase fallback), weather (Open-Meteo, NWS)
src/kst/pricing/      base (FairValue, combine_with_market), crypto, volatility, weather, calibration, resolver
src/kst/scanner/      features, signals (arb/edge/mm), service (ScanSnapshot)
src/kst/strategies/   base, sizing (Kelly), exits, structural_arb, edge_taker, market_maker, router
src/kst/risk/         limits (RiskLimits), checks (chain), engine (breakers, counters)
src/kst/execution/    broker (protocol), paper, live, oms (OrderManager)
src/kst/portfolio/    service (positions/equity/P&L), reports (daily summary + ledger)
src/kst/llm/          packet, providers, gate
src/kst/engine/       trader (AutoTrader loop), control (ControlService), state (EngineState store)
src/kst/persistence/  db, tables, repo (write-behind)
src/kst/api/          FastAPI app, routes, ws hub, schemas, security
src/kst/discord_bot/  bot, views (buttons), notifier, formatting
src/kst/app.py        composition root: read this to see how everything is wired
web/src/              React UI: store.ts (Zustand, WS-driven), components/
```

## Conventions

- Hot-path domain objects are `@dataclass(slots=True)`. Pydantic is for
  config and API DTOs only.
- Events are frozen dataclasses in `core/events.py`. Publish with
  `bus.publish(evt)`. Add new events there, never ad hoc dicts.
- Logging: `log = get_logger(__name__)`, then
  `log.info("order_placed", ticker=..., price=...)`. Use snake_case event
  names and key=value fields.
- Time: inject `Clock`. Never call `time.time()` or `datetime.now()` in
  domain or strategy code; it breaks replay.
- Tests: pure-math modules need closed-form or edge-case tests. Async tests
  use `pytest-asyncio` (auto mode). Mock HTTP with `respx`.
- Config: every new tunable goes in `config/default.yaml` *and*
  `src/kst/config.py` with a comment explaining its effect on P&L or risk.

## Gotchas

- `GET /portfolio/balance` has a separate 1 req/s bucket. Update the balance
  from fills, and poll rarely.
- `GET /events` excludes combo (multivariate) events. Use
  `mve_filter=exclude` on `/markets`.
- The WS needs auth even for public channels. Without keys, market data
  comes from REST polling (`marketdata.source: auto`).
- Crypto markets settle on a **60 s average** of the CF Benchmarks RTI, not
  on spot at T.
- The weather climate day follows *standard* time, so it runs 1 AM to 1 AM
  local time during DST.
- Kalshi's cancel-all can also cancel orders placed in the following minute.
  After a kill, do not re-quote immediately.
