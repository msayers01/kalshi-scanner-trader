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

- Calibration commands produce reviewable candidates; the prior remains identity until a reviewed file is selected.
- No Alembic. Tables are created at startup.
- LiveBroker now has intercepted-REST integration tests and real loopback WebSocket tests; a demo-exchange acceptance run is still pending (`docs/EXECUTION_VALIDATION.md`).
- Residual legs of partially filled arb baskets are not completed automatically.

## v0.2: validate edge before going live

- [x] `kst calibrate`: bounded historical/recent markets + pre-horizon candlesticks or offline samples; global/per-category fits, event-grouped purged holdout, reliability diagrams and candidate files (`docs/CALIBRATION.md`)
- [x] `kst calibrate-weather`: source-labeled final station reports paired with recorded pre-day ensembles or prepared samples; station bias/spread fits, chronological holdout and candidate YAML (`docs/CALIBRATION.md`)
- [x] **Replay backtester** (`kst replay`): offline `MarketDataService` + `ManualClock` + `PaperBroker`, P&L/Sharpe/hit rate reports, maker queue sensitivity, and grids over `s_p_base`, `z`, Kelly `λ`, and `implied_weight`. Recorder now captures point-in-time metadata, fees, weather, volatility seeds and settlements. See `docs/REPLAY.md` for coverage and limitations.
- [x] Paper-trading scorecard per strategy (expected vs realized net at settlement, decision-mid slippage, order fill rate, 60s markout). Available in the UI Scorecard tab and `kst scorecard`; see `docs/SCORECARD.md`. First live paper runs were slightly negative after spread and fees, so measure before tuning
- [ ] Demo-exchange run of the LiveBroker: orders, amends, IOC finalization, fills over WS, cancel-all, order group
- [x] Automated execution validation: OMS/risk + signed REST, real local WS, failure/race cases, fill deduplication, IOC, TTL, kill and group startup checks (`docs/EXECUTION_VALIDATION.md`)
- [x] Local reliability: serialized/cancellation-safe DB batches, durable kill intent, immediate critical-worker escalation, paper fill IDs/dedup across committed checkpoints, persistence health and lifecycle cleanup (`docs/RELIABILITY.md`)
- [ ] Durable fill/checkpoint reconciliation across restarts and eventually consistent balance/position snapshots; unknown-create history recovery
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
