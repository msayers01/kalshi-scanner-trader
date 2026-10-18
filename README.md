# kst — Kalshi Scanner & Quantitative Auto-Trader

A single-process tool for trading [Kalshi](https://kalshi.com) event contracts. It does five things:

- **Scanner.** Ranks every open market by after-fee edge, liquidity, and structural-arbitrage signals.
  Use it on its own for manual trading.
- **Auto-trader.** Runs quantitative strategies behind a 20-check risk engine with a kill switch:
  - structural arbitrage
  - model edge (crypto digitals, weather ensembles)
  - Avellaneda–Stoikov market making
- **LLM entry gate.** A local Qwen model (via Ollama or LM Studio) or Claude/ChatGPT decides *which
  markets to enter*. Calls are batched, cached, and token-budgeted.
- **Web UI.** Real-time over WebSocket: scanner, order book ladder, trade ticket, positions, risk limits,
  and kill switch.
- **Discord bot.** A phone control panel (Pause / Resume / KILL / Kill+Flatten / Status / P&L) plus fill,
  risk, and daily-P&L notifications.

> **It starts in PAPER mode with autotrade OFF.** Paper-trade first, read the P&L reports, and only then
> consider `demo`, and then `live`. No strategy here is guaranteed to make money. The models need
> calibration against your own paper and replay results (`docs/ROADMAP.md`).

## Quick start

```bash
# 1. Python side (needs uv: https://docs.astral.sh/uv/)
uv sync --all-extras
cp .env.example .env                    # add Kalshi API key (optional for paper), Discord token, …
uv run kst check                        # config, credentials, connectivity, LLM reachability

# 2. Web UI (Node 20+)
cd web && npm ci && npm run build && cd ..

# 3. Run (paper mode, scanner + manual trading)
uv run kst run                          # → http://127.0.0.1:8787
uv run kst run --autotrade              # let strategies trade (paper)
```

Without API keys, market data comes from public REST polling (about 3 s books). With keys, it streams
over WebSocket, and crypto is priced off the actual CF Benchmarks settlement index.

## Commands

| Command | What it does |
|---|---|
| `kst run [--mode paper\|demo\|live] [--autotrade] [--i-understand-live-trading]` | Full system |
| `kst scan` | Scanner + UI + Discord, no automatic orders |
| `kst check` | Validate config, keys, exchange status, API tier, LLM, Discord |
| `kst llm-packet --top 20` | Print a compact decision packet to paste into ChatGPT/Claude |
| `kst status` / `kst pause` / `kst resume` / `kst kill [--flatten]` | Control a running instance |
| `kst report --days 14` | Daily P&L history |
| `kst scorecard` | Lifetime paper execution scorecard as JSON (also in the UI Scorecard tab) |
| `kst replay "data/recordings/*.jsonl.gz" --sensitivity` | Offline replay, reports and maker-fill sensitivity |
| `kst calibrate --input data/market-samples.jsonl` | Candidate market-probability fit, holdout metrics and reliability plots |
| `kst calibrate-weather --input data/weather-samples.jsonl` | Candidate station bias/spread fit and validation plots |

## Documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md): components, patterns, data flow, safety design.
- [`CLAUDE.md`](CLAUDE.md): conventions and hard rules for contributors (human or AI).
- [`docs/STRATEGIES.md`](docs/STRATEGIES.md): the math behind every strategy and pricing model.
- [`docs/RISK.md`](docs/RISK.md): limits, breakers, kill and flatten.
- [`docs/LLM_GATE.md`](docs/LLM_GATE.md): how the LLM is used without burning tokens.
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md): setup (Kalshi keys, Discord bot, Ollama), going live, operations.
- [`docs/KALSHI_API_NOTES.md`](docs/KALSHI_API_NOTES.md): verified API facts (fixed-point, V2 orders, WS, fees, limits).
- [`docs/ROADMAP.md`](docs/ROADMAP.md): what's next.
- [`docs/REPLAY.md`](docs/REPLAY.md): offline backtesting, recording coverage, sweeps and report definitions.
- [`docs/CALIBRATION.md`](docs/CALIBRATION.md): probability/weather fits, input formats and candidate review.
- [`docs/EXECUTION_VALIDATION.md`](docs/EXECUTION_VALIDATION.md): automated execution tests, failure handling and pending demo acceptance checks.
- [`docs/RELIABILITY.md`](docs/RELIABILITY.md): durable kill checkpoints, paper restart recovery, persistence health and failure tests.
