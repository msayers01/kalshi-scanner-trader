# Offline replay backtester

`kst replay` feeds recorded inputs through the production market-data service, scanner,
strategy router, risk engine, OMS and PaperBroker. Time advances deterministically via
ManualClock; the exchange, Discord, UI and external model/feed runners are never started.
Credentials are stripped from the replay configuration, even when the operator's configuration
is live. Every variant starts with a fresh cash balance and an isolated `kst_replay.db`.
The running paper/live process and its databases are untouched.

## Run it

```powershell
# A small, explicitly synthetic example; this is a functionality check, not a profitability result.
uv run kst replay examples/replay-demo.jsonl --output data/replays/demo

# Replay one or more completed recordings. Quote globs so expansion works consistently across shells.
uv run kst replay "data/recordings/*.jsonl.gz" --output data/replays/baseline

# Compare maker queue assumptions (current multiplier, 2, 5).
uv run kst replay "data/recordings/*.jsonl.gz" --sensitivity

# Cartesian parameter grid; a maximum of 64 variants per invocation.
uv run kst replay "data/recordings/*.jsonl.gz" --sweep s_p_base=0.2,0.35 --sweep z=0,0.5 --sweep kelly=0.1,0.25 --sweep implied_weight=0,0.7

# Optional initial period that updates models/books but submits no strategy orders.
uv run kst replay "data/recordings/*.jsonl.gz" --warmup-seconds 180
```

Output defaults to a new directory under `data/replays/`. Existing output directories are
rejected; reruns never overwrite previous reports. A failed run can leave partial artifacts;
only a successful CLI exit and completed `comparison.json` identify a completed experiment.
Do not backtest recordings that are still being written. Source SHA-256 hashes are included
in the experiment manifest and rechecked before completion.

Supported sweep aliases and full paths:

| Alias | Configuration field | Range |
|---|---|---|
| `s_p_base` | `pricing.s_p_base` | > 0 |
| `z` | `strategies.edge_taker.z` | ≥ 0 |
| `kelly` | `strategies.edge_taker.kelly_fraction` | 0–1 |
| `implied_weight` | `pricing.crypto.implied_weight` | 0–1 |
| `queue_haircut` | `paper.maker_queue_haircut` | ≥ 0; values below 1 are optimistic |

Each axis can be specified once. `--sensitivity` supplies the queue axis only when no explicit
queue sweep was given. The baseline YAML/.env settings supply everything else; effective
non-secret strategy/risk/pricing settings are saved in each variant report.

## Collect recordings

Set this in `config/local.yaml`, preserving any other local overrides:

```yaml
marketdata:
  record: true
```

On the next normal app start, completed recordings accumulate under
`data/recordings/YYYY-MM-DD.jsonl.gz` (UTC dates). Stop that recording process gracefully before
replaying its current file so gzip trailers are complete. Existing sessions must be restarted
to pick up this configuration. Enabling recording does not enable autotrading.

New schema-2 recordings include point-in-time catalog membership, market definitions, event
relationships, series fee schedules, full books, deltas, public prints, ticker updates,
reference prices, startup volatility-seed closes, weather ensembles/observations, and lifecycle
settlements. Catalog snapshots are repeated on file rotation so new-day depth has metadata.
Volatility and weather state are not automatically bootstrapped at day boundaries: include
earlier recordings or allow sufficient model warmup when needed.

Schema 2 uses `t` for **receipt/availability time** and `source_t` for the underlying exchange
or observation timestamp. The clock never trades before information was available. At a shared
receipt time, all records are applied in line order before strategy timers run; the same print
cannot fill a quote that the subsequent strategy evaluation creates. Maker fills also reject
prints or through-books whose source timestamp predates the order's creation.

Legacy `.jsonl.gz` files and raw `mkt` metadata records are supported with coverage warnings.
Late legacy timestamps are clamped in append order, never sorted into the past. Missing market
metadata or an initial book snapshot is a hard error. No current API data is fetched to repair
old inputs, and missing reference/weather/seed inputs are reported. Unknown record kinds,
future-dated source/observation timestamps, corrupt JSON and truncated gzip fail with context.

## Outputs and definitions

- `comparison.json`: input hashes and all variants' parameters and performance metrics.
- `variant-NNN/report.json`: P&L, risk rejections/state, drawdown, hit rate, Sharpe, effective
  configuration, input coverage warnings, open positions/mark freshness and the paper scorecard.
- `variant-NNN/equity.csv`: timestamps and equity in integer micros, regularly sampled plus a
  final endpoint. The initial row is the starting cash balance before trading.
- `variant-NNN/fills.csv`: simulated fills with integer price, quantity and fee units.
- `variant-NNN/kst_replay.db`: isolated order/fill/settlement ledger and scorecard context.

P&L is final marked equity minus starting equity, after actual simulated fees. Open positions
are **not** assigned a fictional closing fill or settlement; their quantities, marks and freshness
are shown. Gross realized, fees and unrealized P&L are also reported separately. Hit rate is the
fraction of completed engine round trips with positive net P&L; breakeven is not a win.

Maximum drawdown is peak-to-trough marked equity in integer micros, observed at input groups
and strategy timer steps. It includes the starting equity as the initial peak.

Sharpe uses simple returns between consecutive **regular** equity samples, zero risk-free rate
and sample standard deviation. A partial final interval is excluded. Both the per-interval ratio
and its 365-day annualization are reported, along with the number of return samples. Fewer than
two returns or zero variance produces `null`, not an infinite ratio. Short runs do not support
reliable annualized conclusions. Configure `replay.equity_sample_s` (default 60) to change the
interval; scanner/trader/risk timers still advance independently at their configured cadences.

## Assumptions and limitations

The LLM gate is explicitly disabled for offline quantitative-strategy comparison; replay does
not reproduce LLM-assisted entry selection. Today's fitted probability calibration is also
disabled, avoiding an accidental fit on outcomes later than the recording. Quantitative models
need recorded inputs and sufficient historical samples. Parameter sweeps compare assumptions;
they do not perform an out-of-sample validation or select a proven profitable configuration.

Replay covers the recorded universe/focus books. Changing settings can request markets that were
never recorded; missing depth remains missing. Taker fills consume observed depth until a new
book event; market impact, exchange latency, and unobserved replenishment are not simulated.
Maker fills use the existing queue-ahead/trade-through model. Queue sensitivity changes visible
queue-ahead at placement; it does not perturb taker slippage or the book-through assumption.
If a run only takes liquidity, queue variants can legitimately produce identical results.

No historical data is bundled. `examples/replay-demo.jsonl` is explicitly synthetic, and reports
generated from it carry a synthetic-data flag and warning.
