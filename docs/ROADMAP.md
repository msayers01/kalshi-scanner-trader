# Roadmap

Ordered by expected profit impact per unit of effort. Keep the statuses
current.

## v0.1: base (this commit)

- [x] Architecture, docs, ADRs, CLAUDE.md
- [x] Core: fixed-point money, event bus, supervisor, config
- [x] Kalshi adapter: auth (RSA-PSS / Ed25519), token-bucket limiter, REST (V2 orders), WS client
- [x] Market data: catalog, YES-space order books, WS + REST-poll sources, recorder
- [x] Pricing: crypto digital/range with 60 s averaging + horizon-dependent Student-t, EWMA vol blended with event-ladder implied σ, weather ensemble dressing with intraday observations, calibration model (fit code only), Bayesian market combination
- [x] Scanner: structural-arb detection (exclusive/exhaustive baskets, `-T` strike ladders), model edge, DISAGREE/WIDE/CLOSING tags, ranking, focus set
- [x] Strategies: structural_arb, edge_taker, market_maker (A-S logit), Kelly sizing, EV exits, router
- [x] Risk: limit model, check chain, breakers, kill/pause/flatten
- [x] Execution: OMS with declarative reconciliation, PaperBroker, LiveBroker
- [x] Portfolio: positions, cash, equity, realized/unrealized, daily P&L ledger
- [x] LLM gate: compact packets, OpenAI-compatible + Anthropic providers, cache, budgets
- [x] Discord bot: slash commands, persistent control panel, notifier
- [x] Web API (REST + WS hub) and React UI
- [x] Tests (89, no network) + CI (ruff, mypy, pytest, web build)
- [x] Verified end-to-end in paper mode against live public Kalshi data (no keys): ~5k-market catalog, ~1.85k priced, ~110 ms scans, ~1 ms loop lag

**Not done in v0.1 (do not assume these exist):**

- No replay or backtester. `src/kst/backtest/` is an empty package, and there is no `kst replay` command. The recorder writes the input files.
- No `kst calibrate`. The favorite-longshot fit function exists (`pricing/calibration.py::fit_logistic_calibration`), but nothing fetches history or writes `data/calibration.json`. The prior stays at identity, which means no trades from it.
- No Alembic. Tables are created at startup.
- No fake-server test for the WS client. The LiveBroker has only been exercised against unit tests, not the demo exchange.
- Residual legs of partially filled arb baskets are not completed automatically.

## v0.2: validate edge before going live

- [ ] `kst calibrate`: fit the favorite-longshot calibration from `/historical/markets` + candlesticks; per-category fits; reliability diagrams
- [ ] `kst calibrate-weather`: fit per-station bias and spread inflation from past CLI reports versus archived ensembles
- [ ] **Replay backtester** (`kst replay`): drive `MarketDataService` from `data/recordings/*.jsonl.gz` with `ManualClock` + `PaperBroker`. Report P&L, Sharpe, hit rate, and fill-model sensitivity, and sweep `s_p_base`, `z`, Kelly `λ`, and `implied_weight`
- [ ] Paper-trading scorecard per strategy (expected vs realized edge at settlement, slippage, fill ratio, adverse selection). First live paper runs were slightly negative after spread and fees, so measure before tuning
- [ ] Demo-exchange run of the LiveBroker: orders, amends, IOC finalization, fills over WS, cancel-all, order group
- [ ] Residual-leg completion for partially filled arb baskets
- [ ] Alembic baseline + migration for every schema change

## v0.3: more edge sources

- [ ] Crypto options-implied vol from Deribit (DVOL / strike-interpolated IV) as a third anchor next to realized and event-ladder implied σ
- [ ] Scheduled-event volatility bumps (CPI, FOMC, NFP) from an economic calendar
- [ ] Economics markets: Cleveland Fed inflation nowcast, CME FedWatch-style rate probabilities, Kalshi `forecast_percentile_history`
- [ ] S&P/Nasdaq range markets: an index-futures proxy feed + overnight vol model
- [ ] Cross-event coherence: `KXBTCD` ("above K") vs `KXBTC` (ranges) at the same timestamp
- [ ] Multi-outcome Kelly for correlated positions within one event
- [ ] Incentive programs (`GET /incentive_programs`) folded into market-maker EV

## v0.4: operations

- [ ] Docker compose (kst + optional Ollama)
- [ ] Prometheus metrics endpoint
- [ ] Postgres option for long histories
- [ ] Sports in-game win-probability models (Kalshi `/live_data` game stats)
