# Kalshi API: Verified Facts (as of 2026-10-18)

Verified against https://docs.kalshi.com (OpenAPI v3.34.0) and live calls to
the public endpoints. **The Kalshi API changes often. Check the changelog
(https://docs.kalshi.com/changelog, RSS at `/changelog/rss.xml`) before you
rely on anything here, and update this file when you learn something new.**

## Hosts

| | REST | WebSocket |
|---|---|---|
| Prod (recommended) | `https://external-api.kalshi.com/trade-api/v2` | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` |
| Prod (legacy, still OK) | `https://api.elections.kalshi.com/trade-api/v2` | `wss://api.elections.kalshi.com/trade-api/ws/v2` |
| Demo | `https://external-api.demo.kalshi.co/trade-api/v2` | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` |

Market data REST endpoints (`/markets`, `/events`, `/series`, orderbooks,
trades, candlesticks) work **without authentication**. **WebSocket requires
auth for every channel.** Without keys, `kst` falls back to REST polling.

## Authentication

- Headers: `KALSHI-ACCESS-KEY` (key ID), `KALSHI-ACCESS-TIMESTAMP` (ms), and
  `KALSHI-ACCESS-SIGNATURE` (base64).
- Signed string: `timestamp_ms + METHOD + path`. The path starts at
  `/trade-api/...`, **without the query string**. The host is not included.
- Key types:
  - **Ed25519** (recommended): plain signature.
  - **RSA**: RSA-PSS, SHA-256, MGF1(SHA-256), salt length = digest length (32).
  - Detect the type from the parsed key.
- WS handshake: sign `timestamp + "GET" + "/trade-api/ws/v2"`.

## Fixed-point everything

- Prices: `*_dollars` strings, up to 4 dp (`"0.5600"`). Requests accept 2–4 dp;
  responses may emit up to 6 dp for costs and fees.
- Quantities: `*_fp` strings, 2 dp (`"10.00"`). **Fractional contracts exist**
  (minimum 0.01).
- Integer-cent fields are mostly gone. Do not rely on `yes_bid`/`yes_ask`
  ints. Exceptions: `GetBalanceResponse.balance` and `portfolio_value` are
  int cents. `Settlement.revenue` and `value` are int cents.
- **Price grid per market.** `price_ranges: [{start, end, step}]` is the
  source of truth. Snap to it; off-grid orders are rejected. Some structures
  have sub-cent ticks near the edges (e.g. `0.001` below $0.10).
  `price_level_structure` is only a label. `tick_size` was removed.

## Orderbook

- REST `GET /markets/{ticker}/orderbook?depth=N` returns
  `{"orderbook_fp": {"yes_dollars": [[price, qty], ...], "no_dollars": [...]}}`.
  It contains **bids only**, ascending by price, so the best bid is the last
  element. NO bids are in **NO-leg pricing**: a NO bid at 0.30 is a YES ask at
  0.70.
- REST `GET /markets/orderbooks?tickers=A&tickers=B` returns up to 100 books
  in one call, in the shape `{"orderbooks": [{"ticker", "orderbook_fp"}]}`.
- WS `orderbook_delta` sends an `orderbook_snapshot` (`yes_dollars_fp`,
  `no_dollars_fp`) and then `orderbook_delta` messages (`price_dollars`,
  `delta_fp`, `side`). Each message has `sid` and `seq`. **Check `seq`
  continuity** and re-snapshot on a gap (`update_subscription` with
  `get_snapshot`, or resubscribe).
- Subscribe with **`use_yes_price: true`**. Then NO-side levels arrive in
  YES-leg pricing, so a NO-side level at 0.70 is a YES ask at 0.70. The
  default will flip to `true` and the flag will later be removed. We set it
  explicitly, so the behaviour does not change underneath us.

## Orders (V2, use these)

The legacy `/portfolio/orders` mutation endpoints are deprecated. GET
`/portfolio/orders` is still the read endpoint.

| Op | Endpoint |
|---|---|
| Create | `POST /portfolio/events/orders` |
| Batch create | `POST /portfolio/events/orders/batched` |
| Cancel | `DELETE /portfolio/events/orders/{order_id}?market_ticker=...` |
| Batch cancel | `DELETE /portfolio/events/orders/batched` |
| **Cancel ALL** (kill switch) | `DELETE /portfolio/events/orders` (all shards; may also cancel orders placed in the following minute) |
| Amend (price/count/expiry) | `POST /portfolio/events/orders/{order_id}/amend` |
| Decrease | `POST /portfolio/events/orders/{order_id}/decrease` |

Create body fields:

- Required: `ticker`, `side` (`bid` = buy YES, `ask` = sell YES / buy NO),
  `count` (fp string), `price` (YES-leg dollars string), `time_in_force`
  (`fill_or_kill` | `good_till_canceled` | `immediate_or_cancel`), and
  `self_trade_prevention_type` (`taker_at_cross` | `maker`).
- Optional: `client_order_id`, `post_only`, `reduce_only` (IOC only),
  `cancel_order_on_pause`, `expiration_time` (unix seconds, with GTC),
  `order_group_id`, `subaccount`, `exchange_index` (omit to auto-route by
  ticker).

The response contains `order_id`, `fill_count`, `remaining_count`,
`average_fill_price`, `average_fee_paid`, and `ts_ms`. There are no market
orders in V2: use an IOC limit at the price you are willing to pay.

**Direction fields.** On Order, Fill, and Trade responses use `outcome_side`
(`yes`|`no`) or `book_side` (`bid`|`ask`); they carry the same bit. `action`
and `side` are deprecated. Public trades have `taker_outcome_side` and
`taker_book_side`.

## Portfolio

- `GET /portfolio/positions` returns `market_positions[]` with
  `position_fp` (**signed: + = YES, − = NO**), `market_exposure_dollars`,
  `realized_pnl_dollars`, `fees_paid_dollars`, and `total_traded_dollars`. It
  also returns `event_positions[]`.
- `GET /portfolio/balance` returns `balance` (int cents), `balance_dollars`,
  and `portfolio_value` (cents). It has **its own rate bucket: 1/s on
  Basic/Advanced**. Do not poll it; react to fills.
- `GET /portfolio/fills` returns `fill_id`, `order_id`, `ticker`,
  `outcome_side`, `book_side`, `count_fp`, `yes_price_dollars`, `is_taker`,
  `fee_cost` (dollars string), and `created_time`.
  It also carries `trade_id`, which is the identifier in WS fill messages. These
  may differ: LiveBroker deduplicates with an internal SHA-256 key of `order_id:trade_id`
  across both sources when available. Pairing with order ID preserves both fills
  if we are on both sides of a trade. See [REST fills](https://docs.kalshi.com/api-reference/portfolio/get-fills)
  and [WS fills](https://docs.kalshi.com/websockets/user-fills).
- `GET /portfolio/settlements` returns `market_result`, `yes_count_fp`,
  `no_count_fp`, costs, `revenue` (cents), and `fee_cost`.

## WebSocket channels

| Channel | Use |
|---|---|
| `orderbook_delta` | Books for the focus set (market tickers required). |
| `ticker` | Top of book, last price, volume, and OI for all markets (or a filter). Cheap universe coverage. |
| `trade` | Public trades (`yes_price_dollars`, `count_fp`, `taker_outcome_side`). |
| `fill` | Our fills (`yes_price_dollars`, `count_fp`, `fee_cost`, `is_taker`, `post_position_fp`, `book_side`). |
| `user_orders` | Our order state changes. |
| `market_positions` | Our positions. |
| `market_lifecycle_v2` | Open, close, determined, settled, and `price_level_structure_updated` events. |
| `order_group_updates` | Order-group triggers. |
| `cfbenchmarks_value` / `cfbenchmarks_value_5hz` | **The crypto settlement index** (BRTI, ETHUSD_RTI, …) with trailing 60 s averages. Params: `index_ids`. |

Commands: `subscribe`, `unsubscribe`, `update_subscription` (with
`add_markets`, `delete_markets`, or `get_snapshot`), and `list_subscriptions`.
Error code 25 means subscription buffer overflow: you read too slowly or
subscribed to too much.

## Rate limits (token buckets)

- The default cost is 10 tokens per request. `GET /account/endpoint_costs`
  lists exceptions. Cancels are cheaper (the docs cite 2).
- Budgets per second:

  | Tier | Read | Write |
  |---|---|---|
  | Basic | 200 | 100 |
  | Advanced | 300 | 300 |
  | Expert | 600 | 600 |
  | Premier | 1200 | 1200 |

  On Basic that means **about 20 reads/s and 10 order writes/s**.
- Basic/Advanced read buckets hold 3 s of budget. The Basic write bucket holds
  1 s.
- A 429 has no `Retry-After` header. Back off exponentially.
- Upgrade Basic → Advanced via `POST /account/api_usage_level/upgrade`.
- `GET /account/limits` returns your actual budgets. `kst` reads it at
  startup in live and demo mode.

**Unauthenticated public access** (observed 2026-10-18, no API key):

- Light requests (a single orderbook, a small `/markets` page) are fine at
  about 20/s.
- Heavy pages are throttled much harder: `GET /markets?limit=500–1000` and
  `GET /events?with_nested_markets=true`. Paging all open events with nested
  markets returned 429s within a few pages. The nested-events response is
  enormous because every market carries its full rules text.
- Open-market counts by close-time horizon: about 5k markets close within
  24 h, and about 33k within 72 h (about 70 MB per full refresh). Without keys
  the catalog therefore pages `/markets?status=open&mve_filter=exclude` with
  `min_close_ts`/`max_close_ts` (`universe.max_hours_to_close`, default 24),
  sleeps `universe.page_delay_s` between pages, and fetches event metadata
  only for events it has not seen (`GET /events/{ticker}`, chunked).
- `KalshiRest.request` retries GETs on 429 up to 5 times with
  `min(0.5·2ⁿ, 16)` s backoff, and counts them (`throttled`).

## Fees

- Per series: `fee_type` is one of `quadratic`, `quadratic_with_maker_fees`,
  `quadratic_with_combo_maker_fees`, or `flat`, plus `fee_multiplier`. In Oct
  2026, of 15 188 series, 15 025 were `quadratic`, 160 had maker fees, and 3
  had combo maker fees. Multipliers seen: 1, 0.5, and 0.
- Schedule PDF: taker `ceil(0.07 × C × P × (1−P))`; maker `0.0175 × …`, which
  is 0.25× taker, on maker-fee series. Combo maker multiplier is 0.5×.
  Plain `quadratic` series have **no maker fee**.
- A `fee_waiver_expiration_time` on a market means fees are waived until then.
- Fees are computed to $0.000001, and then a rounding fee aligns the balance
  to $0.0001 for direct members (a fee accumulator rebates overpayment).

## Market and event structure

- Market fields used: `ticker`, `event_ticker`, `status` (`active`, `closed`,
  `determined`, `finalized`, …; filter param `status=open`), `close_time`,
  `strike_type` (`greater`, `less`, `between`, `greater_or_equal`, …),
  `floor_strike`, `cap_strike`, `rules_primary`, `yes_bid_dollars`,
  `yes_ask_dollars` (+ `_size_fp`), `volume_24h_fp`, `open_interest_fp`,
  `price_ranges`, `exchange_index`, `result`, and
  `fee_waiver_expiration_time`.
- **Market objects have no `series_ticker`.** It is the prefix of
  `event_ticker` before the first `-` (`series_from_event_ticker`), or comes
  from the event object. `GET /events?with_nested_markets=true` (max 200 per
  page) also has it, but see the throttling note above.
- `GET /events` excludes multivariate (combo) events. Use
  `mve_filter=exclude` on `/markets` to avoid tens of thousands of combo
  markets.
- An event has `mutually_exclusive`, `category`, `series_ticker`, and
  `strike_date`. Example: `KXBTC-26OCT1723` (hourly BTC ranges) has 188
  markets. The lowest is `less`, the middle ones are `between` with $100
  buckets, and the top is `greater`, so it is mutually exclusive and
  exhaustive.
- `GET /series` returns all ~15k series in one response, with category,
  `fee_type`, `fee_multiplier`, and frequency.

## Calibration history collection

Public calibration acquisition reads both recent `GET /markets?status=settled&series_ticker=...`
and archived `GET /historical/markets?series_ticker=...`, deduplicating ticker overlaps. Historical
listing's series/event/ticker filters are mutually exclusive, so no unsupported settlement-time
filter is sent there. API documentation identifies `settlement_ts` on archived market objects.
The tool applies final-result and as-of checks locally.

Recent candles use `/series/{series_ticker}/markets/{ticker}/candlesticks`; archived candles use
`/historical/markets/{ticker}/candlesticks`. Both take Unix-second `start_ts`, `end_ts`, and
`period_interval`; calibration uses 1 minute. Archived YES bid/ask OHLC fields use dollar strings.
Only candles whose end time is at/before the configured horizon are used. 404s near an archive
handoff retry the other candle endpoint. See [historical storage](https://docs.kalshi.com/getting_started/historical_data),
[market filters](https://docs.kalshi.com/api-reference/historical/get-historical-markets), and
[archived candles](https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks).

## Domain-specific

- Crypto (`KXBTCD` above/below hourly and daily, `KXBTC` ranges, `KXETH*`,
  `KXSOL*`, `*15M` 15-minute series): settles on the **simple average of 60
  one-second CF Benchmarks RTI values before the close**.
- Weather `KXHIGH*`: integer °F daily maximum at a station for the climate
  day. The rules text names the station (e.g. "CLINYC") and the source (now
  "The Weather Company"). Strike semantics: `greater 61` ⇒ ≥ 62, `less 54` ⇒
  ≤ 53, `between 60–61` ⇒ 60 or 61.
- `GET /live_data/weather/{city}` returns Kalshi's minute-resolution city
  temperature index (for the hourly temperature markets).
