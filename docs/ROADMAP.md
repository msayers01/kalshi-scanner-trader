# Roadmap

Ordered by expected profit impact per unit of effort. Keep the statuses
current.

## v0.1: base (this commit)

- [x] Architecture, docs, ADRs, CLAUDE.md
- [x] Core: fixed-point money, event bus, supervisor, config
- [x] Kalshi adapter: auth (RSA-PSS / Ed25519), token-bucket limiter, REST (V2 orders), WS client
- [x] Market data: catalog, YES-space order books, WS + REST-poll sources, recorder
- [x] Pricing: crypto digital/range with 60 s averaging + Student-t, EWMA vol, weather ensemble dressing, calibration model, Bayesian market combination
- [x] Scanner: features, structural-arb detection, model edge, ranking, focus set
- [x] Strategies: structural_arb, edge_taker, market_maker (A-S logit), Kelly sizing, EV exits, router
- [x] Risk: limit model, check chain, breakers, kill/pause/flatten
- [x] Execution: OMS with declarative reconciliation, PaperBroker, LiveBroker
- [x] Portfolio: positions, cash, equity, realized/unrealized, daily P&L ledger
- [x] LLM gate: compact packets, OpenAI-compatible + Anthropic providers, cache, budgets
- [x] Discord bot: slash commands, persistent control panel, notifier
- [x] Web API (REST + WS hub) and React UI
- [x] Tests + CI

## v0.2: validate edge before going live

- [ ] `kst calibrate`: fit the favorite-longshot calibration from `/historical/markets` + candlesticks; per-category fits; reliability diagrams
- [ ] `kst calibrate-weather`: fit per-station bias and spread inflation from past CLI reports versus archived ensembles
- [ ] Replay backtester report: P&L, Sharpe, hit rate, and fill-model sensitivity; sweep `s_p_base`, `z`, and Kelly `λ`
- [ ] Paper-trading scorecard per strategy (expected vs realized edge, slippage, fill ratio, adverse selection)
- [ ] Alembic migration for every schema change (baseline exists)

## v0.3: more edge sources

- [ ] Crypto implied vol from Deribit options (DVOL / strike-interpolated IV) blended with realized vol
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
