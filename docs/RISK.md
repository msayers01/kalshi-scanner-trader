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
| `KILLED` | ❌ | only explicit **flatten** orders that are also risk-reducing | **all cancelled** (`DELETE /portfolio/events/orders`) | resume (operator, explicit) |

`KILLED` is persisted and survives restarts. On startup in `KILLED` the
engine stays killed and alerts.

## Pre-trade checks (the chain, evaluated in order)

Two chains run in order: `ALWAYS` (every order) and `ENTRY_ONLY` (marked ★).
An order is "risk-reducing" if it shrinks `|position|` in its market without
flipping the sign. Risk-reducing orders skip the ★ checks. Live orders that
already reduce the position count against it first: with +10 YES and a resting
10-lot sell, a second 10-lot sell is an *entry* (it would flip to NO if both
filled).

**Arb basket legs** (`basket_id` set) also skip `PriceBand`,
`DailyNewMarketLimit`, and `MaxOpenPositions`, because a hedged basket is one
trade, not N directional entries. The exposure checks still apply to every leg,
and `AutoTrader.run_arbs` pre-checks every leg before sending any.

**Re-quotes** pass `replaces=<client_order_id>`, so the order being amended
or replaced is not counted against position and exposure room twice.

| # | Check | Default | Notes |
|---|---|---|---|
| 1 | `EngineStateAllows` | — | KILLED blocks everything. PAUSED blocks non-reducing orders. |
| 2 | `LiveTradingArmed` | — | Live mode needs the config flag and the CLI flag. |
| 3 | `MarketTradable` | — | Status `active`, inside the open/close window, no exchange pause. |
| 4 | `DataFresh` | WS 5 s / poll 20 s | The book is younger than its staleness limit. |
| 5 | `PriceOnGrid` | — | The price is valid on the market's `price_ranges` and is not 0 or 1. |
| 6 | `FatFinger` | 0.10 | `|price − reference| ≤ max_price_deviation` when the price is *worse* for us than the reference. The reference is the fair value if the caller has one, else the live book mid (fresh books only), else the catalog mid. Manual orders are checked against the book mid. |
| 7 | ★ `PriceBand` | 0.03–0.97 | Entry prices outside the band are refused (lottery tickets and pennies-in-front-of-steamroller trades). |
| 8 | ★ `MinTimeToClose` | 120 s | No new entries this close to close. |
| 9 | `MaxOrderSize` | 50 contracts / $50 | Applies to each order. Strategies clip entries (`StrategyContext.clip_qty`) and exits (`plan_exit(max_qty=, max_usd=)`) to these caps, so a large position exits in slices instead of being rejected. |
| 10 | ★ `MaxPositionPerMarket` | 100 contracts / $50 | Includes resting orders on the same side (worst case). |
| 11 | ★ `MaxExposurePerEvent` | $100 | Correlated outcomes. |
| 12 | ★ `MaxExposurePerCategory` | $250 | Optional per-category overrides. |
| 13 | ★ `MaxTotalExposure` | $500 and 50% of equity | Exposure = worst-case loss = cost basis of positions + resting buy orders. |
| 14 | ★ `MaxOpenPositions` | 15 | |
| 15 | ★ `DailyNewMarketLimit` | 10 / trading day | **"Daily limit of markets"**: distinct markets entered per trading day (timezone `app.trading_day_tz`, default America/New_York). A slot is reserved when an entry order is sent and released if the order dies unfilled. |
| 16 | `OrderRate` | 60 / min | Protects the rate-limit budget and catches loops. Risk-reducing orders get 2× the cap, so exits still work when entries hit the limit. |
| 17 | ★ `CategoryAllowed` | config | `universe.categories` allow-list and `risk.blocked_series`. |
| 18 | ★ `BlackoutWindow` | config | No entries during configured windows, e.g. CPI/FOMC ± 5 min for crypto. |
| 19 | `SelfCross` | — | Never send an order that would cross our own resting order. Exchange STP is a backstop. |
| 20 | `SufficientBalance` | — | Cost ≤ available cash − `risk.cash_buffer`. |

Every rejection is logged (`risk_rejected`) with the check name and counted in
`/api/status`. A quote slot whose placement is rejected is not re-placed for
`OrderManager.reject_backoff_ms` (5 s), so a limit that is hit does not turn
into a reject every tick. The take cooldown (2 s per market) throttles repeat
entry takes but never cancels resting quotes.

## Breakers (evaluated on every portfolio update)

| Breaker | Default | Action |
|---|---|---|
| `daily_loss_limit_usd` (realized + unrealized since the day start) | $50 | PAUSE + alert |
| `max_drawdown_usd` (from intraday equity peak) | $100 | KILL + alert |
| `max_consecutive_losses` (closed round trips) | 5 | PAUSE + alert |
| `ws_down_cancel_after_s` (exchange WS down with resting orders) | 15 s | cancel all + PAUSE |
| `reject_storm` (exchange rejects) | 10 in 60 s | PAUSE |
| Critical task crash (market data, OMS, portfolio) | — | KILL |

**Auto-resume** (`engine.auto_resume`, off by default, on with any bankroll preset): a PAUSE made by the
system (any breaker above) is lifted at the start of the next trading day. Pauses made by an operator and
every KILL are never undone automatically.

## Bankroll presets (`kst/presets.py`)

`preset:` in config, `kst run --preset KEY`, or the web UI's Risk limits tab. Every tier scales the limits
by the same fractions of bankroll — 5 % per order and per market, 10 % per event, 25 % per category,
50 % total exposure (and ≤ 50 % of equity), daily loss 5 % → PAUSE, drawdown 10 % → KILL — with
hand-tuned entry throttles (markets/day, open positions). `starter` ($1,000) equals the defaults above.

Presets are hands-off: autotrade on, auto-resume on, and `llm.fallback: allow` (entries proceed on the
quant signal when the LLM is unreachable; with a reachable LLM the gate still decides). They never change
the trading mode. At startup a configured preset wins; switching presets discards limit edits saved for the
previous one; with none configured, the preset last applied in the UI is restored together with its edits.

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
3. If `flatten`, then for every position cancel its resting orders and send
   IOC exits at the touch, walking at most `risk.flatten_max_slippage`
   (default $0.05) from the touch. Exits are sliced to the per-order caps
   (`max_order_contracts`, `max_order_usd`), and `FatFinger` measures them
   against the touch, not the mid, so wide markets can still be flattened.
   Positions that cannot be exited inside the band are reported, not chased.
4. Audit and notify.

The flatten button requires a second confirmation in both the UI and Discord.
