# Risk Management

Risk lives in `src/kst/risk/`.

- `limits.py` holds `RiskLimits`, a pydantic model. Defaults come from
  `config/*.yaml → risk.limits`. Runtime edits from the web UI or Discord are
  persisted in `kv_state` and audited.
- `checks.py` holds the pre-trade check chain. Each check is a pure function
  `(OrderProposal, RiskContext) -> CheckResult`.
- `engine.py` holds `RiskEngine`: daily counters, breakers, and the bridge to
  `ControlService` for automatic pause or kill.

## Engine states

| State | New entries | Exits / risk-reducing orders | Resting orders | How to leave it |
|---|---|---|---|---|
| `RUNNING` | ✅ | ✅ | kept | pause / kill |
| `PAUSED` | ❌ | ✅ | entry quotes cancelled, exit quotes kept | resume |
| `KILLED` | ❌ | only explicit **flatten** | **all cancelled** (`DELETE /portfolio/events/orders`) | resume (operator, explicit) |

`KILLED` is persisted and survives restarts. On startup in `KILLED` the
engine stays killed and alerts.

## Pre-trade checks (the chain, evaluated in order)

An order is "risk-reducing" if it shrinks `|position|` in its market without
flipping the sign. Risk-reducing orders skip the checks marked ★.

| # | Check | Default | Notes |
|---|---|---|---|
| 1 | `EngineStateAllows` | — | KILLED blocks everything. PAUSED blocks non-reducing orders. |
| 2 | `LiveTradingArmed` | — | Live mode needs the config flag and the CLI flag. |
| 3 | `MarketTradable` | — | Status `active`, inside the open/close window, no exchange pause. |
| 4 | `DataFresh` | WS 5 s / poll 20 s | The book is younger than its staleness limit. |
| 5 | `PriceOnGrid` | — | The price is valid on the market's `price_ranges` and is not 0 or 1. |
| 6 | `FatFinger` | 0.10 | `|price − reference| ≤ max_price_deviation`, where reference is the fair value, else the mid. |
| 7 | ★ `PriceBand` | 0.03–0.97 | Entry prices outside the band are refused (lottery tickets and pennies-in-front-of-steamroller trades). |
| 8 | ★ `MinTimeToClose` | 120 s | No new entries this close to close. |
| 9 | `MaxOrderSize` | 50 contracts / $50 | Applies to each order. |
| 10 | ★ `MaxPositionPerMarket` | 100 contracts / $50 | Includes resting orders on the same side (worst case). |
| 11 | ★ `MaxExposurePerEvent` | $100 | Correlated outcomes. |
| 12 | ★ `MaxExposurePerCategory` | $250 | Optional per-category overrides. |
| 13 | ★ `MaxTotalExposure` | $500 and 50% of equity | Exposure = worst-case loss = cost basis of positions + resting buy orders. |
| 14 | ★ `MaxOpenPositions` | 15 | |
| 15 | ★ `DailyNewMarketLimit` | 10 / trading day | **"Daily limit of markets"**: distinct markets entered per trading day (timezone `app.trading_day_tz`, default America/New_York). A slot is reserved when an entry order is sent and released if the order dies unfilled. |
| 16 | `OrderRate` | 60 / min | Protects the rate-limit budget and catches loops. |
| 17 | ★ `CategoryAllowed` | config | `universe.categories` allow-list and `risk.blocked_series`. |
| 18 | ★ `BlackoutWindow` | config | No entries during configured windows, e.g. CPI/FOMC ± 5 min for crypto. |
| 19 | `SelfCross` | — | Never send an order that would cross our own resting order. Exchange STP is a backstop. |
| 20 | `SufficientBalance` | — | Cost ≤ available cash − `risk.cash_buffer`. |

Every rejection is logged with the check name. It is counted in
`/api/health`, and if rejections storm (more than N in M seconds), the system
alerts on Discord.

## Breakers (evaluated on every portfolio update)

| Breaker | Default | Action |
|---|---|---|
| `daily_loss_limit_usd` (realized + unrealized since the day start) | $50 | PAUSE + alert |
| `max_drawdown_usd` (from intraday equity peak) | $100 | KILL + alert |
| `max_consecutive_losses` (closed round trips) | 5 | PAUSE + alert |
| `ws_down_cancel_after_s` (exchange WS down with resting orders) | 15 s | cancel all + PAUSE |
| `reject_storm` (exchange rejects) | 10 in 60 s | PAUSE |
| Critical task crash (market data, OMS, portfolio) | — | KILL |

## Exchange-side safety nets (live and demo)

- Every resting order has `cancel_order_on_pause: true`, so exchange pauses
  cancel it.
- Every resting order has `expiration_time = now + order_ttl_s` (default 600
  s). The OMS re-amends quotes it still wants before expiry, so if the process
  dies, quotes self-destruct.
- Optionally (`risk.order_group.enabled`), all orders join a Kalshi **order
  group** with `contracts_limit` over the exchange's rolling 15 s window.
  Breaching it auto-cancels the group's orders on the exchange side.
- Self-trade prevention is `taker_at_cross`.

## Kill and flatten

`ControlService.kill(flatten: bool)`:

1. Persist `KILLED`.
2. `cancel_all` (exchange-wide).
3. If `flatten`, then for every position send an IOC exit at the touch,
   walking at most `risk.flatten_max_slippage` (default $0.05) from the
   touch. Positions that cannot be exited inside the band are reported, not
   chased.
4. Audit and notify.

The flatten button requires a second confirmation in both the UI and Discord.
